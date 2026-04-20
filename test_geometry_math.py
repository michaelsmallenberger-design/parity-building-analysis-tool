from geometry import get_building_footprint, latlon_to_pixel
from utils import get_satellite_image_mapbox
from PIL import Image, ImageDraw
from shapely.geometry import MultiPolygon
import os as _os

lat = 40.74844
lon = -73.98565
ZOOM = 19
IMG_W, IMG_H = 768, 768

_here = _os.path.dirname(_os.path.abspath(__file__))

# Fetch satellite image
print("Fetching satellite image...")
ok = get_satellite_image_mapbox(lat, lon, _os.path.join(_here, "test_esb.jpg"))
if not ok:
    print("ERROR: Failed to fetch satellite image")
    exit(1)

# Fetch building footprint
print("Fetching building footprint...")
footprint = get_building_footprint(lat, lon)
if not footprint:
    print("ERROR: No building footprint returned")
    exit(1)

polygon = footprint['polygon']
poly_type = type(polygon).__name__

# Handle MultiPolygon — use the largest sub-polygon
if isinstance(polygon, MultiPolygon):
    polygon = max(polygon.geoms, key=lambda p: p.area)
    print("MultiPolygon detected — using largest sub-polygon")

exterior_coords = list(polygon.exterior.coords)
num_vertices = len(exterior_coords)

# Convert polygon vertices to pixel coordinates
pixel_coords = [
    latlon_to_pixel(v_lat, v_lon, lat, lon, ZOOM, IMG_W, IMG_H)
    for v_lon, v_lat in exterior_coords  # shapely stores as (lon, lat)
]

# Bounding box in lat/lon (minx=lon_min, miny=lat_min, maxx=lon_max, maxy=lat_max)
bounds = polygon.bounds

print(f"OSM type: {footprint.get('osm_type')}")
print(f"OSM ID: {footprint.get('osm_id')}")
print(f"OSM tags: {str(footprint.get('tags')).encode('ascii', errors='replace').decode('ascii')}")
print(f"Polygon type: {poly_type}")
print(f"Polygon bounds (lat/lon): {bounds}")
print(f"Polygon area (sq degrees): {polygon.area}")
print(f"Polygon vertices: {num_vertices}")
print(f"contains_point: {footprint['contains_point']}")
print(f"ALL pixel coords:")
for i, px in enumerate(pixel_coords):
    print(f"  [{i}] {px}")

# Draw on image (math_test.jpg — unchanged)
img = Image.open(_os.path.join(_here, "test_esb.jpg")).convert("RGB")
draw = ImageDraw.Draw(img)
draw.line(pixel_coords + [pixel_coords[0]], fill=(255, 0, 0), width=4)
cx, cy = 384, 384
r = 8
draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(0, 255, 0), width=3)
img.save(_os.path.join(_here, "math_test.jpg"))
print("Saved math_test.jpg")

# Draw v2 image with blue bounding box added
img2 = Image.open(_os.path.join(_here, "test_esb.jpg")).convert("RGB")
draw2 = ImageDraw.Draw(img2)

# Red polygon
draw2.line(pixel_coords + [pixel_coords[0]], fill=(255, 0, 0), width=4)

# Green center dot
draw2.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(0, 255, 0), width=3)

# Blue bounding box: bounds = (lon_min, lat_min, lon_max, lat_max)
lon_min, lat_min, lon_max, lat_max = bounds
bb_tl = latlon_to_pixel(lat_max, lon_min, lat, lon, ZOOM, IMG_W, IMG_H)  # top-left
bb_br = latlon_to_pixel(lat_min, lon_max, lat, lon, ZOOM, IMG_W, IMG_H)  # bottom-right
draw2.rectangle([bb_tl, bb_br], outline=(0, 0, 255), width=3)

img2.save(_os.path.join(_here, "math_test_v2.jpg"))
print("Saved math_test_v2.jpg")
