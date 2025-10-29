"""
Local storage version of task processor (replaces tasks_serverless.py).
Processes address lists using local filesystem instead of GCS.
"""
import os
import re
import tempfile
import logging
import pandas as pd
from typing import Callable, Dict, Any, Optional
from utils import geocode_address_mapbox, get_satellite_image_mapbox, run_prediction
from html_report import generate_html_report
from zip_bundler import create_results_bundle, extract_image_paths_from_results

log = logging.getLogger("tasks")

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
    # Verify API keys are configured
    import os
    if not os.getenv('MAPBOX_API_KEY'):
        error_msg = "MAPBOX_API_KEY environment variable is not set. Cannot process addresses."
        log.error(error_msg)
        return {"error": error_msg}

    web_results, csv_rows = [], []

    # Load CSV with comprehensive fallback handling
    log.info(f"Reading CSV file: {uploaded_filepath}")
    df = None

    # Try multiple encoding and delimiter combinations
    encodings = ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252', 'iso-8859-1']
    delimiters = [',', ';', '\t']

    for encoding in encodings:
        if df is not None:
            break

        for delimiter in delimiters:
            try:
                # Try to read with this combination
                test_df = pd.read_csv(
                    uploaded_filepath,
                    encoding=encoding,
                    sep=delimiter,
                    skipinitialspace=True,   # Remove leading whitespace
                    skip_blank_lines=True,   # Skip empty rows
                    on_bad_lines='warn',     # Warn but don't fail on malformed rows
                    engine='python'          # More flexible parser for edge cases
                )

                # Validate: must have at least 1 column and 1 row
                if len(test_df.columns) >= 1 and len(test_df) > 0:
                    df = test_df
                    log.info(f"CSV loaded successfully with encoding='{encoding}', delimiter='{repr(delimiter)}'")
                    break

            except Exception as e:
                # Continue trying other combinations
                continue

    # If all attempts failed
    if df is None:
        error_msg = f"Failed to read CSV file. Tried encodings: {encodings}, delimiters: [comma, semicolon, tab]. Please ensure the file is a valid CSV."
        log.error(error_msg)
        return {"error": error_msg}

    # Strip whitespace from column names
    df.columns = df.columns.str.strip()

    # Remove completely empty rows
    df = df.dropna(how='all')

    total = len(df)
    log.info(f"CSV loaded successfully: {total} rows, {len(df.columns)} columns")
    log.info(f"CSV columns: {list(df.columns)}")

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
        error_msg = f"CSV must contain an address column. Supported column names: 'Address', 'Property Address', 'Street Address', 'Building Address' (case-insensitive). Found columns: {list(df.columns)}"
        log.error(error_msg)
        return {"error": error_msg}

    log.info(f"Using address column: '{address_col}'")

    # Rename to standard 'Address' for consistent processing
    if address_col != 'Address':
        df = df.rename(columns={address_col: 'Address'})

    # Check if DataFrame has any valid rows
    if total == 0:
        error_msg = "CSV file is empty (no rows to process)"
        log.error(error_msg)
        return {"error": error_msg}

    log.info(f"Starting processing loop for {total} addresses")
    done = 0
    for i, row in df.iterrows():
        if should_cancel():
            raise Exception("Job cancelled by user.")

        done += 1
        progress_cb(done, total, None)

        # Check if address is empty or null
        if pd.isna(row['Address']) or str(row['Address']).strip() == '':
            log.warning(f"Row {i}: Empty address, skipping")
            csv_rows.append({
                'Address': '(Empty)',
                'Detected': 'No',
                'Confidence': 'N/A',
                'Original_Image': '',
                'Detection_Result': '',
                'Notes': 'Empty address in CSV'
            })
            web_results.append({
                "address": "(Empty)",
                "confidence_score": None,
                "result_image_url": None,
                "original_image_url": None,
                "error": "Empty Address"
            })
            continue

        # Build address string
        parts = [str(row['Address']).strip()]
        if 'Boro_Area' in df.columns and pd.notna(row.get('Boro_Area')):
            parts.append(str(row['Boro_Area']).strip())
        parts.append('NY')  # keep NYC context
        if 'Zip' in df.columns and pd.notna(row.get('Zip')):
            z = row['Zip']
            parts.append(str(int(z)) if isinstance(z, float) else str(z))
        full_address = ", ".join(parts)

        # Geocode
        log.info(f"Row {i+1}/{total}: Geocoding '{full_address}'")
        coords = geocode_address_mapbox(full_address)
        if not coords:
            log.warning(f"Row {i+1}/{total}: Geocoding failed for '{full_address}'")
            csv_rows.append({
                'Address': full_address,
                'Detected': 'No',
                'Confidence': 'Geocoding Failed',
                'Original_Image': '',
                'Detection_Result': '',
                'Notes': 'Address could not be geocoded'
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
        log.info(f"Row {i+1}/{total}: Geocoded to ({lat:.6f}, {lon:.6f})")

        # Download satellite image to temp
        clean_addr = re.sub(r'[\\/*?:"<>| ,]', '_', str(row['Address'])[:50])
        original_local = os.path.join(tempfile.gettempdir(), f"{job_id}_{i}_{clean_addr}_original.jpg")

        log.info(f"Row {i+1}/{total}: Downloading satellite image")
        ok = get_satellite_image_mapbox(lat, lon, original_local)
        if not ok:
            log.warning(f"Row {i+1}/{total}: Image download failed")
            csv_rows.append({
                'Address': full_address,
                'Detected': 'No',
                'Confidence': 'Image Download Failed',
                'Original_Image': '',
                'Detection_Result': '',
                'Notes': 'Satellite image could not be downloaded'
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
        log.info(f"Row {i+1}/{total}: Running YOLO prediction")
        result_url, confidence = run_prediction(original_local)
        log.info(f"Row {i+1}/{total}: Prediction complete (confidence: {confidence if confidence else 'N/A'})")

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

        # Build CSV row with image URLs
        if confidence:
            detected = 'Yes'
            conf_display = f"{(confidence * 100):.1f}%"
            if confidence >= 0.70:
                notes = 'High confidence detection'
            elif confidence >= 0.40:
                notes = 'Review recommended - medium confidence'
            else:
                notes = 'Low confidence detection'
        else:
            detected = 'No'
            conf_display = 'N/A'
            notes = 'No cooling tower detected'

        csv_rows.append({
            'Address': full_address,
            'Detected': detected,
            'Confidence': conf_display,
            'Original_Image': original_url,
            'Detection_Result': result_image_url,
            'Notes': notes
        })

        # Clean up temp files to save memory
        try:
            if os.path.exists(original_local):
                os.remove(original_local)
            if result_local and os.path.exists(result_local):
                os.remove(result_local)
        except Exception as e:
            log.warning(f"Could not clean up temp file: {e}")

        # Garbage collection every 50 addresses for large batches
        if done % 50 == 0 and total > 100:
            import gc
            gc.collect()
            log.info(f"Memory cleanup at {done}/{total} addresses")

        # Write partial results so frontend can display progress
        if write_partial_result:
            write_partial_result({"web_results": web_results})

    # Log summary statistics
    successful = sum(1 for r in web_results if not r.get('error'))
    failed = sum(1 for r in web_results if r.get('error'))
    detections = sum(1 for r in web_results if r.get('confidence_score'))

    log.info(f"Processing complete: {total} total addresses")
    log.info(f"  ✓ Successful: {successful}")
    log.info(f"  ✗ Failed: {failed}")
    log.info(f"  📡 Cooling towers detected: {detections}")

    # Build results CSV with warning header
    results_df = pd.DataFrame(csv_rows)
    csv_local = os.path.join(tempfile.gettempdir(), f"results_{job_id}.csv")

    # Write CSV with warning comment at the top
    with open(csv_local, 'w', encoding='utf-8', newline='') as f:
        # Add warning header (will appear as first row in Excel/Google Sheets)
        f.write('⚠️ IMPORTANT: Image URLs expire when app is updated (typically 1-2 weeks). Download ZIP bundle for permanent backup.\n')
        f.write('\n')  # Blank line for readability
        # Write actual data
        results_df.to_csv(f, index=False)

    csv_blob = f"results/{job_id}/results_{job_id}.csv"
    upload_file(csv_local, csv_blob)
    csv_url = make_signed_url(csv_blob)

    # Generate HTML report with embedded images (skip for large batches to save memory)
    skip_html = total > 200
    html_local = os.path.join(tempfile.gettempdir(), f"Report_{job_id}.html")
    html_url = None

    if skip_html:
        log.info(f"Skipping HTML generation for large batch ({total} addresses) to conserve memory")
    else:
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
            log.warning(f"Failed to generate HTML report: {e}")
            html_url = None

    # Create ZIP bundle with everything
    zip_local = os.path.join(tempfile.gettempdir(), f"results_{job_id}.zip")

    try:
        # Define blob_to_local if not already defined
        if skip_html:
            from storage_helpers import get_file_path
            def blob_to_local(blob_path):
                return get_file_path(blob_path)

        # Extract all image paths
        image_paths = extract_image_paths_from_results(web_results, blob_to_local)

        # Create the bundle (pass None for html_path if skipped)
        create_results_bundle(
            html_path=html_local if not skip_html and os.path.exists(html_local) else None,
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
        log.warning(f"Failed to create ZIP bundle: {e}")
        zip_url = None

    return {
        "web_results": web_results,
        "csv_url": csv_url,
        "html_url": html_url,
        "zip_url": zip_url
    }
