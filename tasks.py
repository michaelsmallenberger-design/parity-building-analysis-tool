import os
import pandas as pd
import re
from rq import get_current_job
from utils import geocode_address_mapbox, get_satellite_image_mapbox, run_prediction

def process_address_list(uploaded_filepath: str):
    job = get_current_job()
    job_id = job.get_id()
    web_results = []
    csv_data = []
    created_files = []

    try:
        df = pd.read_csv(uploaded_filepath)
        total_addresses = len(df)
    except Exception as e:
        return {"error": f"Error reading CSV: {e}"}

    # "Address" is the only required column
    if 'Address' not in df.columns:
        return {"error": "CSV must contain an 'Address' column."}

    try:
        for i, row in df.iterrows():
            job.refresh()
            if job.is_canceled:
                raise Exception("Job cancelled by user.")

            job.meta['progress'] = i + 1
            job.meta['total'] = total_addresses
            job.save_meta()

            # --- DYNAMICALLY BUILD THE ADDRESS STRING ---
            address_parts = [row['Address']]
            if 'Boro_Area' in df.columns and pd.notna(row.get('Boro_Area')):
                address_parts.append(str(row['Boro_Area']))
            
            address_parts.append('NY') # Always add NY for context
            
            if 'Zip' in df.columns and pd.notna(row.get('Zip')):
                # Convert zip to string and remove .0 if it's a float
                address_parts.append(str(int(row['Zip'])) if isinstance(row['Zip'], float) else str(row['Zip']))
            
            full_address = ", ".join(address_parts)
            # ---------------------------------------------
            
            coords = geocode_address_mapbox(full_address)
            if not coords:
                csv_data.append({'Address': full_address, 'Cooling Tower Detected': 'No', 'Confidence Score': 'Geocoding Failed'})
                continue 

            lat, lon = coords
            
            clean_address = re.sub(r'[\\/*?:"<>| ,]', '_', row['Address'][:50])
            original_image_filename = f"{job_id}_{i}_{clean_address}_original.jpg"
            original_image_filepath = os.path.join('static/uploads', original_image_filename)
            created_files.append(original_image_filepath)
            
            success = get_satellite_image_mapbox(lat, lon, original_image_filepath)
            if not success:
                csv_data.append({'Address': full_address, 'Cooling Tower Detected': 'No', 'Confidence Score': 'Image Download Failed'})
                continue

            result_url, confidence = run_prediction(original_image_filepath)
            
            result_image_filepath = os.path.join('static', result_url)
            created_files.append(result_image_filepath)

            web_results.append({
                "address": full_address, "confidence_score": confidence,
                "result_image_url": result_url,
                "original_image_url": os.path.join('uploads', original_image_filename).replace('\\', '/')
            })
            
            if confidence:
                csv_data.append({'Address': full_address, 'Cooling Tower Detected': 'Yes', 'Confidence Score': f"{(confidence * 100):.1f}%"})
            else:
                csv_data.append({'Address': full_address, 'Cooling Tower Detected': 'No', 'Confidence Score': 'N/A'})

        results_df = pd.DataFrame(csv_data)
        csv_filename = f"results_{job_id}.csv"
        csv_filepath = os.path.join('static/results', csv_filename)
        created_files.append(csv_filepath)
        results_df.to_csv(csv_filepath, index=False)
        
        return {
            "web_results": web_results,
            "csv_path": os.path.join('results', csv_filename).replace('\\', '/')
        }
    finally:
        job.refresh()
        if job.is_canceled:
            for f_path in created_files:
                try:
                    wsl_path = f_path.replace('C:\\', '/mnt/c/').replace('\\', '/')
                    if os.path.exists(wsl_path):
                        os.remove(wsl_path)
                except OSError:
                    pass