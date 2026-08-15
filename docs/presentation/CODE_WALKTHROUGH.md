# Parity Cooling-Tower Analyzer — Code Walkthrough (for the Alex deck)

A step-by-step explanation of what the tool is, how the pipeline works, and how
the pieces fit together. Written to be turned into slides — each numbered section
is roughly one slide's worth of content, with a "Slide takeaway" line you can lift.

---

## 0. What it is (one sentence)

**Given a list of building addresses, it finds which buildings have rooftop
cooling towers** — by combining computer vision on satellite imagery with two
independent AI vision models that cross-check each detection, then emails a
verdict-per-building report.

> **Slide takeaway:** "Upload a list of addresses → get back, for each one, a
> yes/no on rooftop cooling towers with the evidence image and the AI reasoning."

**Why it matters:** cooling towers are a regulated asset (NYC requires
registration — Legionella risk). Manually checking rooftops across thousands of
buildings is slow; this automates the find-and-verify step and flags the
uncertain ones for a human instead of guessing.

---

## 1. The pipeline in one picture

For **each address**, the system runs an 8-stage pipeline:

```
Address
  │
  1. GEOCODE        address text ──► (latitude, longitude)        [Google, Mapbox fallback]
  2. VALIDATE       confirm the address is real & precise          [Google Address Validation]
  3. FOOTPRINT      find the building's outline (polygon)          [OpenStreetMap → NYC → Microsoft]
  4. IMAGERY        download satellite tile centered on the roof   [Google Static Maps / Mapbox]
  5. DETECT (CV)    YOLO model ensemble finds candidate towers     [2× custom-trained YOLO]
  6. FILTER         keep only detections ON this building's roof   [geometry / point-in-polygon]
  7. VERIFY (AI)    Gemini judges the evidence                     [Grok only after technical failure]
  8. REPORT         verdict + annotated image + reasoning          [audit-card HTML]
```

> **Slide takeaway:** "Eight stages, but the headline is: locate the building →
> look at its roof → CV proposes → bounded AI verifies → report."

This is exactly what the demo run logged, per address. The rest of this doc is
each stage in plain English.

---

## 2. Stage 1 — Geocode (address → map coordinates)

**Module:** `utils.py` (`geocode_address`, provider-selectable)

- Turns `"140 West End Ave, New York, NY"` into a precise latitude/longitude.
- **Primary:** Google Maps geocoding. **Fallback:** Mapbox, then Nominatim (OSM).
- It tries cleaned variations of the address (strips parentheses, corporate
  suffixes, building names) and keeps the highest-confidence hit.
- Returns the point **and** a `location_type` (e.g. `GEOMETRIC_CENTER`,
  `ROOFTOP`) used to judge how trustworthy the point is.

> **Slide takeaway:** "Multi-provider geocoding with cleanup + fallbacks — messy
> sales-list addresses still resolve."

**Why two providers:** a single geocoder mis-locates ~some % of messy addresses.
Falling back catches those instead of silently analyzing the wrong rooftop.

---

## 3. Stage 2 — Address Validation (is this a real, precise address?)

**Module:** `utils.py` (Google Address Validation API)

- Confirms the address exists and how *granular* the match is
  (`PREMISE` = an exact building, vs. a vague street-level guess).
- This is a **safety gate**: if we're about to skip the expensive CV/AI work for
  a building (e.g. it's already in a registry), we only trust that skip when the
  address is confirmed precise. A bad geocode on a skipped row would confirm the
  wrong building with nothing to catch it.

> **Slide takeaway:** "Before trusting any shortcut, we verify the address is a
> real, building-level match — no silent wrong-building confirmations."

---

## 4. Stage 3 — Building Footprint (draw the building's outline)

**Module:** `geometry.py` (`get_building_footprint`) + `nyc_opendata.py`, `ms_footprints.py`

- Looks up the **polygon** outline of the building from OpenStreetMap (Overpass
  API): "the building that contains this point" (or nearest within ~50 m).
- **Fallback chain** when OSM has no footprint: NYC planimetric data → Microsoft
  Building Footprints. (OSM is incomplete for new construction / rural areas.)
- The footprint is the key to accuracy: it lets us (a) center the image on the
  **roof**, not the street pin, and (b) later reject cooling towers that belong
  to the *neighbor's* roof.

> **Slide takeaway:** "We don't just look at a point — we get the building's
> actual roof outline, so we analyze the right rooftop and ignore neighbors."

If no footprint is found anywhere, the row is marked `footprint_missing` and sent
for manual review (we never guess).

---

## 5. Stage 4 — Satellite Imagery (get the picture)

**Module:** `utils.py` (`get_satellite_image`, imagery-provider selectable)

- Downloads a high-resolution satellite tile **centered on the footprint
  centroid** (the middle of the roof), not the geocoded street point — critical
  for large buildings/complexes.
- **Two tiles per address:** a *detail* tile (zoom 19, ~tight on the roof) and a
  *wide* tile (zoom 17, ~900 m across) so ground-mounted units in adjacent
  yards/pads are also in frame.
- **Provider logic:** Google Static Maps by default; **dense urban cores
  (19 NYC/metro CBDs) switch to Mapbox**, because Google's 3-D oblique imagery
  hides flat rooftops downtown. This is the `DENSE_CORE_BBOXES` / `_in_nyc` gate.

> **Slide takeaway:** "Roof-centered imagery, two zoom levels, and we switch
> imagery providers downtown where 3-D views would hide the roof."

---

## 6. Stage 5 — Detection: the YOLO computer-vision model

**Module:** `utils.py` (`_get_model`, lazy-loaded) + `tasks_local.py`

- A **custom-trained YOLO model** (YOLO26m, ~44 MB, trained specifically on
  cooling towers) scans the tile and proposes bounding boxes around anything that
  looks like a cooling tower, each with a confidence score.
- We actually run an **ensemble of two model versions** (`MODEL_PATHS`) on **both
  zoom tiles**, then merge the proposals. More viewpoints = fewer misses.
- Detections from different zooms are merged in **geographic space** (~10 m
  threshold), because the same tower appears at different pixel coordinates across
  zoom levels (`geo_dedupe_detections`).
- Runs at confidence threshold `YOLO_CONF=0.18` — deliberately **low recall-first**:
  it's fine to over-propose here, because Stage 7 (the AI verifiers) filters out
  false positives. Better to show the AI a maybe than to miss a real one.

> **Slide takeaway:** "A cooling-tower-specific vision model (an ensemble of two)
> proposes candidates across two zoom levels. Tuned for recall — catch everything,
> let the AI cull."

**From the demo log (140 West End Ave):** `[z19] ensemble union 3 → dedupe 3 →
kept 3` — three candidate boxes found and confirmed on-roof.

---

## 7. Stage 6 — Footprint Filter (is the detection on THIS roof?)

**Module:** `geometry.py` (`footprint_filter_pipeline`)

- Each YOLO box is converted to a lat/lon and tested against the building polygon:
  **inside / on the boundary / outside.**
- Detections on a neighbor's roof are dropped (`neighbor_only`). In **dense
  downtown cores** the gate is stricter — "roof-only scan," drop anything not
  clearly on the building (because rooftops are packed tightly together).

> **Slide takeaway:** "Every candidate is geometrically checked against the
> building's outline — a tower on the building next door doesn't count."

**From the demo log:** `Footprint filter: 3 kept, 0 rejected` for the confirmed
building; `0 kept` for the negative (nothing on that roof).

---

## 8. Stage 7 — Verification: Gemini-first with a bounded fallback

**Module:** `vlm.py` (`verify_address`) — the heart of the accuracy story

- The annotated tile (+ the opposite-zoom tile as context) is sent to **Gemini**
  (`gemini-3.6-flash`) for the normal structured verdict, confidence, and written
  reasoning.
- A valid uncertain Gemini result is kept as `needs_review`; uncertainty does not
  spend on another provider.
- **Grok** (`grok-4.3`) is allowed one emergency request only after Gemini exhausts
  its retries because of a provider, transport, timeout, or structured-output
  failure. It is an availability fallback, not a second vote.
- The reviewer is given **few-shot reference images** (`reference_images/positive`
  and `/negative`) so they know what a real cooling tower vs. a lookalike (vents,
  RTUs, water tanks) looks like.

> **Slide takeaway:** "Gemini handles normal rooftop review. If its service fails
> technically, one bounded Grok fallback keeps the row moving; genuine visual
> uncertainty still goes to a human instead of being guessed."

---

## 9. Stage 8 — The Report (the deliverable)

**Module:** `report_audit.py` (`build_audit_report`)

- Produces a **self-contained HTML audit report**: one **card per building**, in
  sections grouped by verdict (positives first, "needs review" in the middle,
  negatives last).
- Each card shows: the address, the verdict + confidence, the
  **annotated satellite image** (red building outline, color-coded detection
  boxes), Gemini's reasoning, and Grok's reasoning only when the emergency
  fallback actually ran.
- Built **email-safe** (table layout, inline styles) so it renders correctly in
  Gmail/Outlook, not just a browser.
- Images are embedded inline (base64), so the report is one file / one email with
  nothing to host.

> **Slide takeaway:** "The output is a single emailable report — every building as
> a card with the evidence image and both AIs' reasoning, sorted by verdict."

---

## 10. Two ways to drive it (the "front doors")

**Module:** `app_railway.py` (Flask)

1. **Browser UI (interactive):** upload a CSV → a background worker processes it →
   results page + downloadable report. Backed by a SQLite job queue + worker
   thread (`worker.py`, `job_queue.py`, `tasks_local.py`).
2. **Stateless API (automation):** `api_analyze.py` exposes
   `POST /api/run` — send a list of addresses, get the finished HTML report back.
   No queue, no storage, images inline. **This is the endpoint n8n calls.**
   Guarded by an `X-API-Key` secret so it can't be hit anonymously (it spends
   AI-API money per address).

> **Slide takeaway:** "Same engine, two doors: a web upload for people, and a
> one-call API for automation."

---

## 11. The automation: n8n "sheet in, email out"

**Folder:** `n8n/` (`parity_cooling_tower.workflow.json`, `README.md`)

```
Webhook → Read Google Sheet → Normalize each address (Grok LLM)
        → Collect into one list → POST /api/run (the analyzer)
        → Email the HTML report (Gmail) → Post confirmation (Slack)
```

- **n8n only orchestrates** — it can't run the ML itself (its Python sandbox has
  no PyTorch/YOLO), so it calls the analyzer's `/api/run` endpoint.
- An LLM **normalization** step (Grok) cleans messy sheet rows into a consistent
  `number street, city, STATE ZIP` format before geocoding.
- One bad address doesn't abort the batch — its failure is encoded in that
  building's card.

> **Slide takeaway:** "End-to-end automation: someone drops addresses in a Google
> Sheet, and a finished report lands in their inbox — no one touches the tool."

---

## 12. Tech stack & where things run

| Layer | Technology |
|---|---|
| Computer vision | Ultralytics YOLO (2× custom cooling-tower models, ~44 MB each) |
| AI verification | Google Gemini normally; one xAI Grok request only after a technical Gemini failure |
| Geocoding / imagery | Google Maps + Mapbox (+ Nominatim/OSM fallback) |
| Building footprints | OpenStreetMap Overpass → NYC planimetric → Microsoft |
| Geometry | Shapely (point-in-polygon, Web-Mercator math) |
| Web app | Python Flask, single process + background worker thread (SQLite queue) |
| Packaging | Docker (`Dockerfile.railway`, CPU-only PyTorch) |
| Orchestration | n8n Cloud |

> **Slide takeaway:** "Python + Flask + a custom YOLO model + bounded AI
> verification, packaged in Docker, orchestrated by n8n."

---

## 13. Accuracy, cost, and honest limits (good for the "what's next" slide)

- **Accuracy approach:** recall-first CV (catch everything) + Gemini
  precision review + a `needs_review` bucket so uncertain
  cases go to a human rather than being guessed. The system is designed to
  **not silently be wrong.**
- **Footprint coverage:** depends on OSM/NYC/Microsoft data; genuinely missing
  buildings are flagged `footprint_missing` for manual check.
- **Cost:** Gemini is the normal per-address AI expense. Grok adds cost only on
  exceptional technical fallback calls. Hosting is separate and flat.
- **Large batches (>~200):** the report embeds every image, so very large reports
  get heavy for email — split the sheet or write to Drive (a known next-step).

> **Slide takeaway:** "Built to flag uncertainty rather than fake confidence.
> Costs scale per-address; the main scaling work left is huge-batch reporting."

---

## 14. Deployment status & the ask for Alex

- **Status:** fully built and demo-proven locally (this report), and now deployed
  live on Render (Standard instance, 2 GB).
- **Where it landed:** production runs on a **Parity-owned Render account** (not an
  intern account that expires), Standard tier — which also lifts the memory limit the
  trial capped (2 GB). Builds straight from the GitHub repo + Docker image.
- **Handoff is clean:** the code already builds from `Dockerfile.railway`; the only
  setup is creating the host account and setting the API keys (Google/Mapbox/
  Gemini/xAI + the API secret).

> **Slide takeaway:** "It's ready to ship. The one decision is who owns the
> hosting — a ~$5/mo Parity account or our existing cloud — and then it's live."
