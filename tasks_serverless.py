import os, re, tempfile, pandas as pd
from typing import Callable, Dict, Any, Optional
# Reuse your existing utilities (Google→Mapbox geocoding, Mapbox static image, YOLO prediction)
from utils import geocode_address_mapbox, get_satellite_image_mapbox, run_prediction

def process_address_list(
    uploaded_filepath: str,
    job_id: str,
    progress_cb: Callable[[int, int, Optional[str]], None],
    should_cancel: Callable[[], bool],
    upload_file: Callable[[str, str], str],       # (local_path, dest_blob) -> gcs_blob_path
    make_signed_url: Callable[[str], str],        # (gcs_blob_path) -> signed_url
) -> Dict[str, Any]:
    """
    Serverless-friendly version of your RQ task.
    - Reads CSV from uploaded_filepath
    - For each row: geocode (Google→Mapbox), fetches satellite image, runs YOLO, uploads artifacts to GCS
    - Uses progress_cb and should_cancel for status/cancel
    - Returns {'web_results': [...], 'csv_url': <signed url>}
    """
    web_results, csv_rows = [], []

    # Load CSV
    try:
        df = pd.read_csv(uploaded_filepath)
        total = len(df)
    except Exception as e:
        return {"error": f"Error reading CSV: {e}"}

    if 'Address' not in df.columns:
        return {"error": "CSV must contain an 'Address' column."}

    done = 0
    for i, row in df.iterrows():
        if should_cancel():
            raise Exception("Job cancelled by user.")

        done += 1
        progress_cb(done, total, None)

        # Build address string like your original logic
        parts = [row['Address']]
        if 'Boro_Area' in df.columns and pd.notna(row.get('Boro_Area')):
            parts.append(str(row['Boro_Area']))
        parts.append('NY')  # keep NYC context
        if 'Zip' in df.columns and pd.notna(row.get('Zip')):
            z = row['Zip']
            parts.append(str(int(z)) if isinstance(z, float) else str(z))
        full_address = ", ".join(parts)

        # Geocode
        coords = geocode_address_mapbox(full_address)
        if not coords:
            csv_rows.append({'Address': full_address, 'Cooling Tower Detected': 'No', 'Confidence Score': 'Geocoding Failed'})
            continue
        lat, lon = coords

        # Download satellite image to temp
        clean_addr = re.sub(r'[\\/*?:"<>| ,]', '_', str(row['Address'])[:50])
        original_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_original.jpg")
        ok = get_satellite_image_mapbox(lat, lon, original_local)
        if not ok:
            csv_rows.append({'Address': full_address, 'Cooling Tower Detected': 'No', 'Confidence Score': 'Image Download Failed'})
            continue

        # Run prediction (may create a local result image or write to static/results)
        result_url, confidence = run_prediction(original_local)

        # Upload original
        original_gcs = upload_file(original_local, f"uploads/{job_id}/{os.path.basename(original_local)}")
        original_signed = make_signed_url(original_gcs)

        # Try to locate a real local file for the result image
        result_local = None
        if result_url and os.path.exists(result_url):
            result_local = result_url
        elif isinstance(result_url, str) and result_url.startswith('results/'):
            # utils.run_prediction returns web path 'results/<file>'; actual file is at 'static/results/<file>'
            maybe_local = os.path.join(os.getcwd(), 'static', result_url.replace('\\', '/'))
            if os.path.exists(maybe_local):
                result_local = maybe_local

        if result_local and os.path.exists(result_local):
            result_gcs = upload_file(result_local, f"results/{job_id}/{os.path.basename(result_local)}")
            result_signed = make_signed_url(result_gcs)
        else:
            # Fallback: if we can't find a separate result image, show the original
            result_signed = original_signed

        web_results.append({
            "address": full_address,
            "confidence_score": confidence,
            "result_image_url": result_signed,
            "original_image_url": original_signed
        })

        if confidence:
            csv_rows.append({'Address': full_address, 'Cooling Tower Detected': 'Yes', 'Confidence Score': f"{(confidence * 100):.1f}%"})
        else:
            csv_rows.append({'Address': full_address, 'Cooling Tower Detected': 'No', 'Confidence Score': 'N/A'})

    # Build results CSV locally, upload, return signed URL
    results_df = pd.DataFrame(csv_rows)
    csv_local = os.path.join(tempfile.gettempdir(), f"results_{job_id}.csv")
    results_df.to_csv(csv_local, index=False)
    csv_blob = upload_file(csv_local, f"results/{job_id}/results_{job_id}.csv")
    csv_signed = make_signed_url(csv_blob)

    return {"web_results": web_results, "csv_url": csv_signed}
