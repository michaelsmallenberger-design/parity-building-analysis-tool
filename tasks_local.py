"""
Local storage version of task processor (replaces tasks_serverless.py).
Processes address lists using local filesystem instead of GCS.
"""
import os
import re
import tempfile
import pandas as pd
from typing import Callable, Dict, Any, Optional
from utils import geocode_address_mapbox, get_satellite_image_mapbox, run_prediction
from html_report import generate_html_report
from zip_bundler import create_results_bundle, extract_image_paths_from_results

def process_address_list(
    uploaded_filepath: str,
    job_id: str,
    progress_cb: Callable[[int, int, Optional[str]], None],
    should_cancel: Callable[[], bool],
    upload_file: Callable[[str, str], str],       # (local_path, dest_blob) -> blob_path
    make_signed_url: Callable[[str], str],        # (blob_path) -> url
    write_partial_result: Callable[[Dict[str, Any]], None] = None,  # Optional callback for streaming results
) -> Dict[str, Any]:
    """
    Process a CSV of addresses with YOLO model predictions.

    This is the local storage version - works with filesystem instead of GCS.
    """
    web_results, csv_rows = [], []

    # Load CSV
    try:
        df = pd.read_csv(uploaded_filepath)
        total = len(df)
    except Exception as e:
        return {"error": f"Error reading CSV: {e}"}

    # Auto-detect address column (support common variations)
    address_col = None
    address_variants = [
        'Address', 'address', 'ADDRESS',
        'Property Address', 'property address', 'PROPERTY ADDRESS',
        'PropertyAddress', 'propertyaddress', 'PROPERTYADDRESS',
        'Street Address', 'street address', 'STREET ADDRESS',
        'StreetAddress', 'streetaddress', 'STREETADDRESS',
        'Building Address', 'building address', 'BUILDING ADDRESS',
        'BuildingAddress', 'buildingaddress', 'BUILDINGADDRESS',
        'Property_Address', 'property_address', 'PROPERTY_ADDRESS',
        'Street_Address', 'street_address', 'STREET_ADDRESS',
        'Building_Address', 'building_address', 'BUILDING_ADDRESS'
    ]

    for variant in address_variants:
        if variant in df.columns:
            address_col = variant
            break

    if not address_col:
        return {"error": f"CSV must contain an address column. Supported column names: 'Address', 'Property Address', 'Street Address', 'Building Address' (case-insensitive)."}

    # Rename to standard 'Address' for consistent processing
    if address_col != 'Address':
        df = df.rename(columns={address_col: 'Address'})

    done = 0
    for i, row in df.iterrows():
        if should_cancel():
            raise Exception("Job cancelled by user.")

        done += 1
        progress_cb(done, total, None)

        # Build address string
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
            csv_rows.append({
                'Address': full_address,
                'Cooling Tower Detected': 'No',
                'Confidence Score': 'Geocoding Failed'
            })
            web_results.append({
                "address": full_address,
                "confidence_score": None,
                "result_image_url": None,
                "original_image_url": None,
                "error": "Geocoding Failed"
            })
            # Write partial result for failed address
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        lat, lon = coords

        # Download satellite image to temp
        clean_addr = re.sub(r'[\\/*?:"<>| ,]', '_', str(row['Address'])[:50])
        original_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_original.jpg")

        ok = get_satellite_image_mapbox(lat, lon, original_local)
        if not ok:
            csv_rows.append({
                'Address': full_address,
                'Cooling Tower Detected': 'No',
                'Confidence Score': 'Image Download Failed'
            })
            web_results.append({
                "address": full_address,
                "confidence_score": None,
                "result_image_url": None,
                "original_image_url": None,
                "error": "Image Download Failed"
            })
            # Write partial result for failed address
            if write_partial_result:
                write_partial_result({"web_results": web_results})
            continue

        # Run prediction (creates result image in static/results/)
        result_url, confidence = run_prediction(original_local)

        # Upload original image
        original_blob = f"uploads/{job_id}/{os.path.basename(original_local)}"
        upload_file(original_local, original_blob)
        original_url = make_signed_url(original_blob)

        # Handle result image
        result_local = None
        if result_url and os.path.exists(result_url):
            result_local = result_url
        elif isinstance(result_url, str) and result_url.startswith('results/'):
            # utils.run_prediction returns web path 'results/<file>'
            # Actual file is at 'static/results/<file>'
            maybe_local = os.path.join(os.getcwd(), 'static', result_url.replace('\\', '/'))
            if os.path.exists(maybe_local):
                result_local = maybe_local

        if result_local and os.path.exists(result_local):
            result_blob = f"results/{job_id}/{os.path.basename(result_local)}"
            upload_file(result_local, result_blob)
            result_image_url = make_signed_url(result_blob)
        else:
            # Fallback: use original if no result image
            result_image_url = original_url

        web_results.append({
            "address": full_address,
            "confidence_score": confidence,
            "result_image_url": result_image_url,
            "original_image_url": original_url
        })

        if confidence:
            csv_rows.append({
                'Address': full_address,
                'Cooling Tower Detected': 'Yes',
                'Confidence Score': f"{(confidence * 100):.1f}%"
            })
        else:
            csv_rows.append({
                'Address': full_address,
                'Cooling Tower Detected': 'No',
                'Confidence Score': 'N/A'
            })

        # Write partial results so frontend can display progress
        if write_partial_result:
            write_partial_result({"web_results": web_results})

    # Build results CSV
    results_df = pd.DataFrame(csv_rows)
    csv_local = os.path.join(tempfile.gettempdir(), f"results_{job_id}.csv")
    results_df.to_csv(csv_local, index=False)

    csv_blob = f"results/{job_id}/results_{job_id}.csv"
    upload_file(csv_local, csv_blob)
    csv_url = make_signed_url(csv_blob)

    # Generate HTML report with embedded images
    html_local = os.path.join(tempfile.gettempdir(), f"Report_{job_id}.html")

    # We need a function to get local paths from blob paths for image encoding
    # This lambda will be passed to the HTML generator
    def blob_to_local(blob_path):
        # For local storage, blob paths are relative to storage base
        # The make_signed_url returns /files/<blob_path>
        # We need to reconstruct the actual local path
        from storage_helpers import get_file_path
        return get_file_path(blob_path)

    try:
        generate_html_report(
            web_results=web_results,
            job_id=job_id,
            output_path=html_local,
            get_local_path_func=blob_to_local
        )

        # Upload HTML report
        html_blob = f"results/{job_id}/Report_{job_id}.html"
        upload_file(html_local, html_blob)
        html_url = make_signed_url(html_blob)
    except Exception as e:
        print(f"Warning: Failed to generate HTML report: {e}")
        html_url = None

    # Create ZIP bundle with everything
    zip_local = os.path.join(tempfile.gettempdir(), f"results_{job_id}.zip")

    try:
        # Extract all image paths
        image_paths = extract_image_paths_from_results(web_results, blob_to_local)

        # Create the bundle
        create_results_bundle(
            html_path=html_local,
            csv_path=csv_local,
            image_paths=image_paths,
            output_zip_path=zip_local,
            job_id=job_id
        )

        # Upload ZIP bundle
        zip_blob = f"results/{job_id}/results_{job_id}.zip"
        upload_file(zip_local, zip_blob)
        zip_url = make_signed_url(zip_blob)
    except Exception as e:
        print(f"Warning: Failed to create ZIP bundle: {e}")
        zip_url = None

    return {
        "web_results": web_results,
        "csv_url": csv_url,
        "html_url": html_url,
        "zip_url": zip_url
    }
