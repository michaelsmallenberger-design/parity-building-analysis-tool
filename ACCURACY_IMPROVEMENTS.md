# Accuracy Improvement Tracking

## Current Issue: False Positives from Neighboring Buildings

**Problem**: The tool detects cooling towers anywhere in the satellite image, not just on the target building's rooftop. This causes false positives when neighboring buildings have cooling towers but the target address does not.

**Root Cause**:
- Mapbox satellite images are 768x768px at zoom 19
- At this zoom level, multiple buildings are visible in the frame
- YOLO detects cooling towers on ANY building in the image
- We report a detection even if the tower is on a neighboring building, not the target address

**Example False Positive Scenario**:
```
Target: 123 Main St (small building, no cooling tower)
Image includes: 125 Main St (large building with cooling tower)
Result: ✅ Cooling Tower Detected (INCORRECT - tower is on neighboring building)
```

## Proposed Solutions

### Option 1: Center-Crop Analysis (RECOMMENDED)
**Approach**: Crop the image to focus only on the center building before YOLO inference

**Implementation**:
- After downloading satellite image, crop to center 40-50% of frame
- Run YOLO only on the cropped region
- Add visual indicator on annotated image showing analyzed area

**Pros**:
- Simple and reliable
- Significantly reduces false positives
- Maintains original image for user reference

**Cons**:
- May miss towers if geocoding is slightly off-center
- Reduces context for visual inspection

**Parameters to tune**:
- `CENTER_CROP_PERCENT` (default: 40%) - How much of center to analyze
- Could make this configurable per-job

### Option 2: Distance-Based Filtering
**Approach**: Run YOLO on full image but filter detections by distance from center

**Implementation**:
- Run YOLO on full 768x768 image
- Calculate distance from each detection's bounding box center to image center
- Only count detections within radius threshold (e.g., 150 pixels from center)
- Visualize the "target zone" as a circle overlay

**Pros**:
- Keeps full image context
- More forgiving if geocoding is slightly off
- Can visualize which detections were counted vs ignored

**Cons**:
- More complex logic
- Need to tune radius threshold
- Still relies on geocoding accuracy

**Parameters to tune**:
- `MAX_DETECTION_RADIUS_PX` (default: 150px) - Max distance from center to count

### Option 3: Higher Zoom Level
**Approach**: Increase zoom from 19 to 20 or 21 to show fewer buildings

**Pros**:
- Very simple - just change one parameter
- Gets closer to target building

**Cons**:
- May lose useful context
- Some buildings might not fit in frame
- Doesn't fundamentally solve the problem

### Option 4: Multi-Scale Analysis
**Approach**: Fetch images at multiple zoom levels, compare results

**Implementation**:
- Fetch zoom 19 (wide) and zoom 20 (close)
- If detection at both levels → likely on target building
- If detection only at wide zoom → likely on neighbor

**Pros**:
- Most accurate approach
- Can distinguish between target and neighbors

**Cons**:
- 2x API calls and processing time
- 2x cost
- More complex logic

## Recommended Implementation Plan

**Phase 1** (Quick Win - 30 minutes):
1. Implement center-crop approach (Option 1)
2. Add config variable `CENTER_ANALYSIS_ENABLED` (default: True)
3. Add visual indicator showing analyzed region
4. Make crop percentage configurable via env var

**Phase 2** (Optional Enhancement - 1 hour):
1. Add distance-based filtering as alternative mode
2. Allow users to choose mode via UI dropdown
3. Display which mode was used in results

**Phase 3** (Advanced - 2+ hours):
1. Collect data on false positive rate with center-crop
2. Experiment with multi-scale analysis if needed
3. Train model specifically on center-cropped images

## Testing Strategy

**Test Cases**:
1. Building with tower in center → Should detect (True Positive)
2. Building without tower, neighbor has tower → Should NOT detect (avoiding False Positive)
3. Building with tower slightly off-center → Should still detect (avoid False Negative)
4. Large building complex with multiple towers → Should detect

**Metrics to Track**:
- False Positive Rate (detections on wrong building)
- False Negative Rate (missing actual towers)
- User-reported accuracy feedback

## Status

- [ ] Issue documented
- [ ] Solution chosen
- [ ] Implementation started
- [ ] Testing completed
- [ ] Deployed to production
- [ ] Accuracy metrics collected

## Notes

- Original issue raised by user on 2025-10-28
- Current zoom level: 19
- Current image size: 768x768px
- YOLO confidence threshold: 0.25 (configurable via YOLO_CONF env var)
