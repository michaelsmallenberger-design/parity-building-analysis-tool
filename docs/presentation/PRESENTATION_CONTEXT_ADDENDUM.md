# Presentation Context — ADDENDUM (newer features `CLAUDE.md` didn't cover)

> Fold these into the deck. The first draft (`PRESENTATION_CONTEXT.md`) was built
> partly from `CLAUDE.md`, which lags the current code. These items are confirmed
> against the live code + recent commits and are **material to the story** —
> especially the NYC registry integration, which is both a feature and a
> business-value point.

---

## A. ⭐ NYC cooling-tower registry integration (the biggest miss)

**Commits:** `6bf1667` (NYC OpenData registry lookup — DOHMH + planimetric),
`5d7863c` (registry-first gate), `8c0358e` (gate on fully-qualified address),
`925e1a0` (count registry hits as positives). Code: `nyc_opendata.py`,
`tasks_local.py` `lookup_nyc_registry()`.

**What it does:** before spending any CV/AI compute, for a fully-qualified NYC
address the pipeline looks the building up — **by BIN (Building Identification
Number)** — in the **official NYC cooling-tower registry** (NYC OpenData / DOHMH,
cross-referenced with NYC planimetric data). 

- **Registry hit →** verdict **`registry_confirmed`**, with a citation (source +
  BIN), and the pipeline **skips stages 4–7 entirely** (no imagery, no YOLO, no
  Gemini/Grok). It's an authoritative, ground-truth confirmation at **zero
  per-address AI cost**.
- **No registry hit →** the full imagery + CV + dual-AI pipeline runs as normal.

**Why this matters for the deck (two big points):**
1. **Cost + speed:** every building already in the registry is confirmed for free and
   instantly — you only pay AI spend on the unknowns. On a NYC list this can knock out
   a large share of rows before any model runs.
2. **Compliance lead generation (the killer angle):** the interesting case is the
   **inverse** — when the CV + dual-AI pipeline **detects a cooling tower that is NOT
   in the registry**. That's a strong signal of a **potentially unregistered cooling
   tower** — i.e., a compliance gap / enforcement or outreach lead. The tool isn't just
   "find cooling towers"; it's **"reconcile what's on the roofs against the official
   registry."** That framing is likely the core value to Parity.

**Verdict literal to add everywhere:** `registry_confirmed` (counts as a positive in
summaries; it's the top of the verdict order).

---

## B. Construction activity = a sales lead signal

Code: `tasks_local._build_notes()`. The dual-AI step also reports **construction**.
When a building is **negative for a cooling tower BUT construction is visible**, the
report appends: *"No cooling tower, but construction visible — investigate via
CoStar."*

**Why it matters:** a building under construction is a **future install / pipeline
lead** for the sales team. So the tool surfaces two kinds of leads: (1) unregistered
existing towers, and (2) construction sites likely to add equipment. Worth a bullet
on the "how the sales team uses it" slide.

---

## C. Concurrency + resilience (the "is it production-grade?" slide)

Recent hardening commits (`de4ddc8`, `1559238`, `33695e8`, `73315cc`):
- **Address-level concurrency:** processes **~5 addresses in parallel** (configurable),
  with a **transient-retry queue** and a YOLO inference lock — so a 50-address sheet
  isn't 50× one address in wall-clock time.
- **Throttle-aware retries:** distinguishes "API throttled, retry" from "genuinely no
  building here," and retries the former with backoff.
- **Overpass mirror failover:** automatically fails over to backup OpenStreetMap
  endpoints (with a sticky last-known-good) so footprint lookups survive an endpoint
  outage.
- **Thread-safe geocoder rate-limiting** for safe concurrent runs.

**Deck point:** it's not a fragile script — it's built for batch runs with retries,
failovers, and parallelism.

---

## D. Geocoder-agreement confidence flag

Commit `b0091d5`. The pipeline emits a **geocode confidence** signal based on whether
the independent geocoders **agree** (and by how many meters they diverge —
`geocode_confidence`, `geocode_divergence_m` in the output). A large divergence flags
"we may have located the wrong building," which feeds the trust/needs-review story.

**Deck point:** reinforce §3's "never silently wrong" — even the *geocoding* step
carries a confidence flag, not just the final verdict.

---

## E. One consolidated AI call (accuracy + cost detail)

Commit `b32e6ee` ("1-call VLM"). The current design uses a **single `verify_address`
call per building** that judges all candidate boxes at once (replacing the older
per-box `verify_detection` + whole-tile `verify_rooftop` calls). Each of the two
models is still called once (in parallel) — so it's **two AI calls per building**, not
N-per-detection. This keeps cost bounded and predictable regardless of how many YOLO
boxes a roof has.

**Correction to the cost slide:** "~$0.10–$0.30 per address" is **per address**
(one Gemini + one Grok call), independent of detection count — and **$0 for
registry-confirmed NYC buildings** (they skip the AI entirely).

---

## F. Residential / area gating (fewer false positives)

`AREA_GATE_ENABLED` (commit `0f3db5e`) + `is_house` flag (`b943a01`). The system gates
out implausibly small / residential buildings (a configurable minimum commercial
footprint), and the AI separately flags a building it judges to be **a house**
(`is_house`). Both reduce false positives on residential structures that aren't real
targets.

---

## G. Net corrections to the first draft

- **Add a Stage 0/3.5 "registry check"** to the pipeline diagram and a dedicated
  slide — it's the most distinctive feature and the clearest cost/compliance story.
- **Reframe the value prop** from "detect cooling towers" to **"reconcile rooftop
  reality against the official registry — surfacing unregistered towers and
  construction leads."**
- **Cost slide:** note registry-confirmed rows are free; AI cost is per-address (2
  calls), not per-detection.
- **Production-readiness slide:** add concurrency, retries, provider failover.
- **Data sources:** NYC OpenData / DOHMH registry + NYC planimetric are not just
  footprint fallbacks — the **registry** is a first-class data source driving verdicts.

---

## I. ⭐ SCOPE CORRECTION — it's a GLOBAL tool (registry is a NYC-only accelerator)

The first draft over-anchored on NYC. Correct framing:

- **The detection pipeline is GLOBAL.** Geocode → footprint → 2× satellite imagery →
  YOLO → dual-VLM works on any address, anywhere. This is the product.
- **The NYC cooling-tower registry is an optional fast-path that only exists for NYC.**
  Where a building is in the registry, it's a **`.gov`-verified tower**, so we trust it
  and **skip all compute** (imagery/YOLO/VLM) → pure cost savings.
- **Anything not in the registry — including every non-NYC address — goes to the full
  VLM detection pipeline.** The registry never narrows scope; it only short-circuits the
  NYC rows the government has already verified.

**Deck framing:** "A global rooftop/ground cooling-tower detector. In NYC we also
reconcile against the city's official registry — free-confirming what's already
registered and flagging detected-but-unregistered towers as compliance leads."

---

## J. The two-zoom design = rooftop AND ground-mounted coverage

Confirmed in `tasks_local.py`. Each address fetches **two satellite tiles**:
- **Detail tile (zoom 19)** — tight on the roof → rooftop cooling towers.
- **Wide tile (zoom 17, ~920 m across)** — catches **ground-mounted cooling equipment
  in adjacent yards / pads / lots** that fall outside the tight rooftop frame.

Both YOLO models run on **both** tiles; detections are de-duplicated across zooms in
**geographic space** (same tower at different pixel positions isn't double-counted).

**Nuance (important for a global tool):** in **dense urban cores the wide tile is
suppressed** (roof-only) because downtown, off-building detections are neighbors'
equipment = false positives. So: **rooftop detection everywhere; ground-mounted
detection (via the wide zoom) on suburban / industrial / campus sites** — which is
exactly where ground-mounted cooling towers are common. This is a coverage strength,
not a gap: dense cities → rooftop; everywhere else → rooftop + ground.

---

## K. Deeper technical findings (from reading utils.py / vlm.py / nyc_opendata.py)

**Google is the DEFAULT provider, not Mapbox.** `GEOCODER_PROVIDER` and
`IMAGERY_PROVIDER` both default to **`google`**. Google Maps does geocoding +
Static Maps imagery + Address Validation. **Mapbox is now the *specialist*:** the
geocode fallback AND the imagery provider for **dense urban cores** (where Google's
3-D view hides roofs). Nominatim (OSM) is the last-resort geocode + the
confidence-cross-check. (First draft implied Google/Mapbox co-primary — correct it:
Google default, Mapbox for fallback + downtown.)

**Google watermark masking (a real engineering fix).** Google bakes a logo/credit
strip into Static Maps tiles; YOLO was detecting that text as equipment. The code
paints over the bottom strip (`GOOGLE_WATERMARK_PX`) before YOLO runs — a concrete
false-positive fix worth a "we hardened the CV inputs" mention.

**The dual-VLM call is ONE smart pass, not per-box.** `verify_address` sends each
model a single marked-up tile (target footprint drawn in **red**, YOLO candidates
**numbered**) + reference images, and each model **both verifies the boxes AND
independently scans for towers YOLO missed** — in one call. So it's exactly **2 AI
calls per building** (1 Gemini + 1 Grok, in parallel), regardless of how many boxes.

**The exact consensus rule** (`_combine_verdicts`): bucket each model's verdict into
positive / negative / abstain. **Both must land in the same non-abstain bucket AND
both clear the confidence threshold (default 0.70)** → confirmed, with final
confidence = the *lower* of the two (conservative). Any disagreement or either model
under threshold → **`needs_review` (confidence 0)**. `construction` and `is_house`
only stick if **BOTH** models say so (AND logic) — deliberately conservative.

**`image_unusable` → automatic imagery retry.** If *either* model says it can't see
the target roof, the result is flagged and the pipeline can re-fetch imagery (e.g.
switch provider). A real robustness loop worth noting.

**Negative reference images.** Both models get few-shot **positive** examples (real
towers, yellow-boxed) AND **negative** examples (rooftop air handlers, exhaust fans,
skylights, satellite dishes, water tanks) as **exclusion anchors** — this is a key
false-positive-reduction mechanism (the classic "water tank vs cooling tower"
confusion is handled explicitly).

**Footprint fallback chain, precisely:** OSM Overpass (primary) → **NYC planimetric**
(`x748-37q7`, which does double duty as both a footprint source AND a registry
source) → **Microsoft Building Footprints**. No footprint anywhere → `footprint_missing`.

**Registry matching is rigorous (not fuzzy).** A registry confirm requires: point in
NYC bbox + the OSM footprint that *contains* the point carries a **BIN** tag + a DOHMH
(Local Law 77) **or** NYC planimetric row within 50 m with a **matching BIN**. A
nearest-building (non-containing) match is explicitly NOT trusted (could be a
neighbor's BIN). A hit means "cooling-tower infrastructure exists or existed";
active/decommissioned status is shown in the citation but never changes the verdict.

**Wide-tile zoom is 18 by default** (not 17). Correct any "~920 m / z17" figure to
"a wider, lower-zoom tile (z18) for ground-mounted equipment + parcel context."

**Address Validation is built + enabled but low-value.** `validate_address_google`
runs (Google Address Validation API), but the code's own note says it adds ~nil value
on clean address lists — treat it as a minor gate, not a headline feature.

---

## H. Still verify before stating as fact on a slide

- Exact **accuracy %** — not locked; your notes had validation at the N=20 sample
  stage with a larger run pending. Don't put a hard number on a slide unless confirmed.
- Exact **per-call API pricing** (Google/Mapbox/Gemini/Grok) — use ranges; confirm
  against current rate cards.
- Whether the **registry lookup is NYC-only today** (it is, via NYC OpenData) — so the
  registry-reconciliation value currently applies to **NYC**; other markets get the
  CV+AI detection but not the registry cross-check (yet). Be precise about that scope.
