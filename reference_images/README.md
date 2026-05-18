# Reference Images for VLM Few-Shot Visual Grounding

These images are loaded by `vlm.py` and sent with every Gemini API call as
few-shot examples. They teach the model what cooling towers look like from
overhead satellite imagery (positives) and what common lookalikes look like
(negatives), so it can verify YOLO detections more accurately.

## Loading behavior

- `vlm.py` loads up to 5 images per folder, sorted alphabetically.
- Accepted file extensions: `.jpg`, `.jpeg`, `.png`.
- Missing folders are handled silently (no error).
- Numeric filename prefixes (`01_`, `02_`, etc.) control load order.

## Positive examples (cooling towers)

All sourced from the Roboflow training set (`mikes-workspace-ugvwl/parity-cooling-tower`).
Yellow boxes are the original labels — they help Gemini focus on the equipment.

| File | Variety |
|---|---|
| `01_large_multicell_bank.png` | Large multi-cell bank (8 cells) on apartment building |
| `02_two_cell_dark_roof.png` | Two-cell tower with visible radial fans, dark roof |
| `03_small_twocell_white_roof.png` | Small two-cell tower on white roof |
| `04_side_by_side_different_buildings.png` | Two single cells on different adjacent buildings (footprint filter test case) |
| `05_two_singlecells_same_building.png` | Two single cells on the same building, side-by-side |

## Negative examples (lookalikes — NOT YET POPULATED)

Pull 3-5 from the `hard_negatives` folder in Roboflow. Suggested coverage:
- Rectangular RTU / air handler (boxy, no fan)
- Round exhaust fan or vent (smaller than a cooling tower)
- Satellite dish (round with mounting hardware)
- Skylight grid (crosshatch pattern)
- Wooden water tank (cylinder, common on NYC rooftops)
