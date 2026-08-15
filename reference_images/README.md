# Reference Images for VLM Few-Shot Visual Grounding

These images are loaded by `vlm.py` and sent with VLM verification calls as
few-shot examples. They teach the model what cooling towers look like from
overhead satellite imagery and what common rooftop false positives look like.

## Loading behavior

- `vlm.py` loads up to 5 images per folder, sorted alphabetically.
- Accepted file extensions: `.jpg`, `.jpeg`, `.png`.
- Missing folders are handled silently (no error).
- Numeric filename prefixes (`01_`, `02_`, etc.) control load order.
- The existing 5 positive and 3 negative assets are tightly cropped around the
  annotated equipment while retaining enough rooftop context for comparison.
- Gemini receives these reference-only images at medium media resolution to
  reduce token use; live building, detail, and context imagery keeps its normal
  resolution.

## Positive examples (cooling towers)

All sourced from the Roboflow training set
(`mikes-workspace-ugvwl/parity-cooling-tower`). Yellow boxes are the original
labels; they help Gemini focus on the equipment.

| File | Variety |
|---|---|
| `01_large_multicell_bank.png` | Large multi-cell bank (8 cells) on apartment building |
| `02_two_cell_dark_roof.png` | Two-cell tower with visible radial fans, dark roof |
| `03_small_twocell_white_roof.png` | Small two-cell tower on white roof |
| `04_side_by_side_different_buildings.png` | Two single cells on different adjacent buildings (footprint filter test case) |
| `05_two_singlecells_same_building.png` | Two single cells on the same building, side-by-side |

## Negative examples (human-reviewed RTU false positives)

The three anonymous JPEGs in `negative/` came from production rows that a human
reviewer explicitly marked as false positives for future reference. They are
de-identified, equipment-only contact sheets: surrounding buildings, streets,
and location context were removed before publication. They show RTUs, rooftop
condensers, exhaust fans, or air handlers that YOLO proposed as cooling towers.

YOLO draws every candidate box green regardless of what equipment it contains.
Green is a neutral locator, never evidence that a unit is an RTU, a cooling
tower, positive, or negative. These examples are negative because a human
reviewer rejected the equipment inside the boxes. A crop may retain a fragment
of the red target-building outline; neither annotation color is an equipment
feature.

Only add a production image here after a human reviewer has explicitly
classified it as a false positive. Keep filenames anonymous and do not include
customer addresses or other source-sheet contents.

| File | Variety |
|---|---|
| `01_human_reviewed_rtu_false_positive.jpg` | Three compact rooftop units and small fan grids |
| `02_human_reviewed_rtu_false_positive.jpg` | Two RTU / condenser false positives |
| `03_human_reviewed_rtu_false_positive.jpg` | RTU / exhaust-fan false positive |
