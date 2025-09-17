import os
import requests
import re
from urllib.parse import quote
from ultralytics import YOLO
from config import MAPBOX_API_KEY, GOOGLE_API_KEY # Import keys from config.py

# Load your model
MODEL = YOLO("models/rooftop_model.pt")

def geocode_with_google(address: str):
    """
    Uses Google to find a specific street address for a place name.
    Returns a clean street address string or the original address on failure.
    """
    if not GOOGLE_API_KEY:
        return address 

    params = {
        'address': f"{address}, New York, NY",
        'key': GOOGLE_API_KEY,
    }
    try:
        response = requests.get("https://maps.googleapis.com/maps/api/geocode/json", params=params)
        if response.status_code == 200:
            data = response.json()
            if data['status'] == 'OK' and data['results']:
                return data['results'][0]['formatted_address']
    except requests.exceptions.RequestException:
        pass
    return address

def geocode_address_mapbox(address: str):
    """
    Geocodes a text address, first attempting to clean it with Google,
    then using Mapbox constrained to an NYC bounding box.
    """
    if not MAPBOX_API_KEY:
        raise ValueError("Mapbox API key not found in config.py")
    
    clean_address = geocode_with_google(address)

    # --- ADD THIS LINE FOR DEBUGGING ---
    print(f"Original Input: '{address}' -> Google Result: '{clean_address}'")

    NYC_BBOX = "-74.25909,40.477398,-73.70018,40.917577"
    encoded_address = quote(clean_address)
    
    geocode_url = (
        f"https://api.mapbox.com/search/geocode/v6/forward?q={encoded_address}"
        f"&access_token={MAPBOX_API_KEY}&limit=1&bbox={NYC_BBOX}"
    )
    try:
        response = requests.get(geocode_url)
        if response.status_code == 200:
            data = response.json()
            if data['features']:
                coords = data['features'][0]['geometry']['coordinates']
                return (coords[1], coords[0]) # Return as (lat, lon)
        return None
    except requests.exceptions.RequestException:
        return None

def get_satellite_image_mapbox(lat: float, lon: float, filepath: str):
    """Downloads a satellite image from Mapbox."""
    if not MAPBOX_API_KEY:
        raise ValueError("Mapbox API key not found in config.py")
    
    image_url = (
        f"https://api.mapbox.com/styles/v1/mapbox/satellite-v9/static/"
        f"{lon},{lat},19/640x640"
        f"?attribution=false&logo=false&access_token={MAPBOX_API_KEY}"
    )
    try:
        img_response = requests.get(image_url)
        if img_response.status_code == 200:
            with open(filepath, 'wb') as f:
                f.write(img_response.content)
            return True
        return False
    except requests.exceptions.RequestException:
        return False

def run_prediction(image_path: str):
    """Runs YOLO prediction on an image."""
    results = MODEL(image_path)
    
    highest_confidence = None
    detections = results[0].boxes
    
    if detections.shape[0] > 0:
        highest_confidence = max(detections.conf.tolist())
    
    base_name = os.path.basename(image_path)
    result_filename = f"result_{base_name}"
    result_filepath = os.path.join('static/results', result_filename)
    results[0].save(filename=result_filepath)
    
    result_image_url = f"results/{result_filename}"
    return result_image_url, highest_confidence