# Parity Cooling-Tower Analyzer — Full Presentation Context

> **Purpose of this document:** a complete, self-contained briefing so another
> Claude (or person) can build a polished slide deck without needing the codebase.
> It covers *what* the tool does, *how* it works, *why* each piece is designed the
> way it is, *every external service/API* it depends on, *how people use it*, the
> *costs*, the *deployment story*, and a *ready-to-build slide outline* at the end.
> Audience for the deck: **Alex** (decision-maker at Parity) — semi-technical, cares
> about reliability, cost, and what it takes to put this into production.

---

## 1. The problem it solves

**Cooling towers are a regulated, safety-critical rooftop asset.** They're the
leading environmental source of Legionella (Legionnaires' disease), so
jurisdictions — **New York City most aggressively** — require building owners to
**register every cooling tower** with the health department and maintain them.

The business problem: **figuring out which buildings actually have cooling towers,
at scale.** Doing it manually means a person squinting at satellite imagery one
rooftop at a time across thousands of addresses — slow, inconsistent, expensive.

**What the tool does:** given a list of building addresses (a sales sheet, a
prospect list, a registry cross-check), it automatically determines, per building,
whether there's a rooftop cooling tower — and returns the evidence image plus the
reasoning, in a report you can email.

**Who uses the output:** a sales/operations team at Parity that needs to identify
buildings with cooling towers (e.g., for outreach, compliance services, or market
sizing). The report is written so a **non-technical sales rep** can read each
verdict and act on it.

---

## 2. How it works — the end-to-end pipeline

For **each address**, the system runs an 8-stage pipeline. (These stage names map
1:1 to what the live system logs when it runs — they're real, not idealized.)

```
ADDRESS
  1. GEOCODE        address text  ──►  latitude / longitude         (Google → Mapbox → OSM)
  2. VALIDATE       confirm it's a real, building-level address      (Google Address Validation)
  3. FOOTPRINT      get the building's roof outline (a polygon)      (OpenStreetMap → NYC → Microsoft)
  4. IMAGERY        download satellite tile centered on the roof     (Google Static Maps / Mapbox)
  5. DETECT         YOLO vision model proposes cooling-tower boxes   (2× custom-trained YOLO)
  6. FILTER         keep only detections ON this building's roof     (Shapely point-in-polygon)
  7. VERIFY         Gemini judges the evidence                       (Grok only after technical failure)
  8. REPORT         verdict + annotated image + reasoning            (email-safe HTML + Excel)
```

### Stage-by-stage detail

**1. Geocode** — Convert the address string to map coordinates. Tries cleaned
variants (strips parentheses, building names, corporate suffixes). **Primary
Google**, falls back to **Mapbox**, then **Nominatim (OpenStreetMap)**. Returns the
point plus a precision tag (e.g. `ROOFTOP` vs `GEOMETRIC_CENTER`).

**2. Address Validation** — Google confirms the address is real and how *granular*
the match is (`PREMISE` = exact building). This is a **safety gate** so the system
never silently analyzes the wrong building when it takes a shortcut.

**3. Building footprint** — Look up the building's actual outline polygon from
**OpenStreetMap (Overpass API)**: "the building containing this point." Fallback
chain when OSM is missing the building: **NYC planimetric data → Microsoft Building
Footprints**. The footprint is what makes the rest accurate (center the image on the
*roof*, and later reject a neighbor's cooling tower). No footprint anywhere →
flagged `footprint_missing` for manual review (never guessed).

**4. Satellite imagery** — Download a high-res tile **centered on the roof centroid**
(not the street pin). Fetches **two zoom levels** (detail z19 + wide z17) so
ground-mounted units in adjacent pads are also captured. **Google Static Maps by
default; switches to Mapbox in dense urban cores** (19 NYC/metro downtowns), because
Google's 3-D oblique downtown imagery hides flat rooftops.

**5. Detection (computer vision)** — A **custom-trained YOLO model** (YOLO26m, ~44 MB,
trained specifically on cooling towers) scans the tiles and draws bounding boxes
around candidates with confidence scores. Runs an **ensemble of two model versions**
on **both zoom tiles**, merging results in geographic space. Deliberately tuned
**recall-first** (low threshold, 0.18) — over-propose now, let the AI cull later.

**6. Footprint filter** — Each detection is geometrically tested against the building
polygon: inside / boundary / outside. Detections on a neighbor's roof are dropped.
In dense downtowns the gate is stricter ("roof-only").

**7. Verification (the accuracy core)** — The annotated tile (+ the other zoom as
context) is sent to **Google Gemini** for the normal structured verdict, confidence,
and written reasoning, with **few-shot reference images** of real vs. fake cooling
towers (vents, water tanks, RTUs). Valid uncertainty becomes `needs_review`. **xAI
Grok is a one-request emergency fallback only after a technical Gemini failure**;
it is not called for an ordinary uncertain roof.

**8. Report** — A self-contained, **email-safe HTML report**: one card per building,
grouped by verdict, each showing the annotated image (red footprint outline + boxes),
a **Google Maps / Earth / Bing link**, and the actual reviewer path: Gemini
reasoning normally, with Grok shown only when the emergency fallback ran. Plus an
**Excel spreadsheet** of the tabular results.

---

## 3. Why it works — the design rationale (the "why" slides)

These are the points that make the tool *trustworthy*, which is what Alex cares about.

- **Footprint-centered analysis, not pin-centered.** Geocoders return a street
  point; large buildings/complexes are big. Centering imagery on the *roof outline*
  and filtering detections against it is why a neighbor's cooling tower doesn't
  produce a false positive.

- **Recall-first CV + precision-first AI.** One model doing both jobs has to balance
  missing real towers vs. crying wolf. We split it: the **YOLO** stage is tuned to
  *catch everything* (it's fine to over-propose), and the **Gemini review** stage is tuned
  to *only confirm when sure*. Each stage does the job it's good at.

- **Uncertainty and outages are handled differently.** A visually ambiguous roof is
  routed to a human as `needs_review`; it is not sent to a second model to manufacture
  confidence. A technical Gemini outage can use one bounded Grok request so a provider
  failure does not silently drop the building.

- **It's designed to never be silently wrong.** Every failure mode has an explicit
  bucket: no footprint → `footprint_missing`; visual uncertainty → `needs_review`;
  API timeout → flagged retryable. Nothing gets a fake "confident" answer.

- **Provider fallbacks everywhere.** Geocoding (Google→Mapbox→OSM) and footprints
  (OSM→NYC→Microsoft) each have fallbacks, so one provider's gap doesn't sink a row.

- **Reasoning a salesperson can read.** The AI prompts explicitly ask for 2–5 plain
  sentences referencing what's actually on the roof — not jargon. The output is a
  sales tool, not a research dump.

---

## 4. External services & APIs (the dependency slides)

This is the full list of everything the system talks to. **"Key needed?"** = whether
you must obtain/set an API key.

| Service | What it does in the pipeline | Key / Auth | Cost model | Required? |
|---|---|---|---|---|
| **Google Maps Platform** (Geocoding + Static Maps + Address Validation APIs) | Stage 1 geocode, Stage 4 imagery, Stage 2 validation | `GOOGLE_MAPS_API_KEY` | Pay-per-call; Google gives a recurring monthly free credit that covers light use | **Yes** (default provider) |
| **Mapbox** (Geocoding + Static Images) | Geocode fallback; **dense-urban imagery** | `MAPBOX_API_KEY` | Free tier (~50k loads/mo) then per-call | **Yes** (used downtown + fallback) |
| **Google Gemini** (Generative Language / AI Studio API) | Normal Stage 7 AI verification | `GEMINI_API_KEY` | Pay-per-call (token-based); `gemini-3.6-flash` is the default | **Yes** |
| **xAI Grok** (OpenAI-compatible API) | One emergency verification request after a technical Gemini failure | `XAI_API_KEY` | Pay-per-call (token-based) | Optional |
| **OpenStreetMap Overpass API** | Stage 3 building footprints (primary) | none | Free, public, rate-limited | Yes (no key) |
| **Nominatim (OSM)** | Geocode last-resort fallback | none | Free, 1 req/sec | No key |
| **NYC planimetric / OpenData** | Footprint fallback in NYC | none | Free public data | No key |
| **Microsoft Building Footprints** | Footprint fallback (national) | none | Free open dataset | No key |
| **Render** | Hosts the app (the analyzer API + web UI) | account | $25/mo (Standard, 2 GB) — live | **Yes** (deployed) |
| **n8n Cloud** | Orchestration (sheet → analyze → email) | account + node credentials | Free trial / paid tiers | Optional (only for the automation path) |
| **Google Sheets API** | n8n reads the address list | OAuth (in n8n) | Free | Only for n8n path |
| **Gmail API** | n8n / script sends the report email | OAuth or App Password | Free | Only for emailing |
| **Slack** | Optional "analysis complete" ping | OAuth/token (in n8n) | Free | Optional |

**Internal (not an external API): the YOLO model.** The cooling-tower detector is a
**custom-trained model we own** (two 44 MB weight files), bundled into the app — not
a third-party service. This is a differentiator: the core CV is proprietary.

### The 5 environment variables the deployed app needs
- `GOOGLE_MAPS_API_KEY` — geocode + imagery + validation
- `MAPBOX_API_KEY` — dense-urban imagery + fallback
- `GEMINI_API_KEY` — Gemini verification
- `XAI_API_KEY` — optional Grok emergency fallback
- `ANALYZE_API_KEY` — a shared secret guarding the `/api/*` endpoints so they can't
  be called anonymously (they cost money per call)

---

## 5. Architecture — how the pieces fit

**A single Python (Flask) application** that bundles the ML, packaged in Docker, with
**two ways to drive the same pipeline**:

1. **Browser UI (for people):** upload a CSV of addresses → a background worker
   processes them → results page + downloadable report. Uses a small SQLite job
   queue + worker thread; stores files locally.

2. **Stateless API (for automation):** `POST /api/run` — send a list of addresses,
   get the finished HTML report back in one call. No queue, no storage, images
   embedded inline. Guarded by the `X-API-Key` secret. **This is what n8n calls.**
   - `GET  /api/health` → liveness check (no auth)
   - `POST /api/run`    → list of addresses → HTML report (the n8n endpoint)
   - `POST /api/analyze`→ one address → structured JSON (images as data URIs)
   - `POST /api/report` → assemble a report from collected results

**Why a stateless API exists:** the automation tool (n8n) **cannot run the ML
itself** — its scripting sandbox has no PyTorch/YOLO — so it offloads the heavy work
to this endpoint and just orchestrates around it.

---

## 6. How people actually use it (the workflow slides)

### Path A — n8n automation ("sheet in, email out") — the headline workflow
```
Webhook → Read Google Sheet → Normalize addresses (LLM) → Collect into a list
        → POST /api/run (Render analyzer) → Format email → Email the report (Gmail)
        → [optional] Slack "complete"
```
A user drops addresses into a Google Sheet, the workflow fires, and a finished
report lands in an inbox minutes later. **No one touches the tool itself.** The LLM
"normalize" step cleans messy sheet formats into consistent addresses before
geocoding. One bad address doesn't abort the batch — its failure shows on that
building's card.

### Path B — Browser upload (manual)
A person uploads a CSV in the web UI and downloads the report. Good for one-off lists
and for people who don't want to deal with sheets/automation.

### Path C — Direct API
Any system can `POST /api/run` with a list of addresses and an API key and get the
report. This is the integration point if Parity ever wires it into another internal
tool.

### The deliverable in all cases
- **HTML report** — per-building cards, verdict-grouped, annotated images, AI
  reasoning, maps links. Renders in Gmail/Outlook (built email-safe).
- **Excel spreadsheet** — sortable/filterable tabular results (address, verdict,
  confidence, each model's verdict + reasoning, detections, notes).

---

## 7. Cost (the money slide)

- **AI verification:** Gemini is the normal per-address variable expense. Grok
  adds spend only on exceptional technical fallback calls. Measure provider
  usage and retries before promising a fixed per-address budget.
- **Geocoding / imagery:** small per-call (cents), largely covered by Google's
  recurring free monthly credit + Mapbox's free tier at this volume.
- **Hosting (Render):** **$25/month** flat — Standard instance (2 GB RAM, 1 CPU),
  always-on, no per-request billing.
- **Footprints / OSM / Microsoft / NYC data:** **free.**
- **n8n Cloud:** its own subscription if used for automation (free trial available).

**Headline:** hosting is a flat monthly expense; the main variable cost is
per-address Gemini usage, with rare Grok fallback spend.

---

## 8. Accuracy, confidence & honest limits (the credibility slide)

- **Confidence model:** the system reports Gemini's confidence and, critically,
  **flags uncertainty (`needs_review`) instead of guessing.** That's the trust story.
- **Gemini review over recall-first CV** is the precision mechanism (see §3).
- **Known limits (be upfront — it reads as credible):**
  - *Footprint coverage:* depends on OSM/NYC/Microsoft data; genuinely missing
    buildings are flagged, not guessed.
  - *Imagery recency:* satellite tiles are as current as the provider's imagery; very
    new construction may lag.
  - *Large email reports:* embedding every image makes 100s-of-rows reports heavy for
    email — the roadmap is to host images / write to Drive for big batches.
  - *Cost scales with volume* (per-address AI spend).
- **Validation done to date:** spot-checks against NYC registry/planimetric data have
  shown strong agreement on sampled sets; a larger precision validation (N=100–200) is
  the natural next measurement. *(Frame as "validated on samples, scaling the
  measurement," not "proven at scale.")*

---

## 9. Tech stack (the architecture slide)

| Layer | Technology |
|---|---|
| Computer vision | Ultralytics YOLO — 2× custom cooling-tower models (~44 MB each) |
| AI verification | Google Gemini normally; one xAI Grok request only after a technical Gemini failure |
| Geocoding / imagery | Google Maps + Mapbox (+ Nominatim/OSM fallback) |
| Building footprints | OSM Overpass → NYC planimetric → Microsoft |
| Geometry | Shapely (point-in-polygon, Web-Mercator math) |
| App | Python + Flask, background worker thread, SQLite queue |
| Packaging | Docker (CPU-only PyTorch image) |
| Hosting | Render (Standard, 2 GB) |
| Automation | n8n Cloud (Google Sheets, Gmail, Slack, HTTP) |

---

## 10. Deployment status & the ask for Alex (the closing slide)

- **Status:** fully built, and **demo-proven end-to-end locally** (working report with
  real NYC buildings: 2 confirmed cooling towers + 1 correctly flagged for review).
  Now deployed live on a Parity-owned Render Standard instance (2 GB RAM, $25/mo).
- **Where it landed:** a **Parity-owned Render account** (not an intern/trial account
  that expires), on the Standard tier — which also lifts the memory limit the trial
  capped (2 GB RAM).
- **Why it's a clean handoff:** the app already builds from a Docker file; standing it
  up is (1) create the host account, (2) set the 5 API keys, (3) point it at the repo.
  The GitHub repo + container are ready.
- **Immediate next step requested:** a **live n8n test run** (sheet → emailed report)
  once the Parity-owned host is up — which is a ~$5 + 30-minute task.

---

## 11. SUGGESTED SLIDE OUTLINE (hand this structure to the deck builder)

A ~13-slide deck. Each bullet = the slide's job.

1. **Title** — "Automated Cooling-Tower Detection for Building Portfolios" + Parity branding.
2. **The problem** — cooling towers are regulated (Legionella); finding them across
   thousands of buildings by hand is slow/inconsistent. (§1)
3. **What it does (one line + the deliverable)** — addresses in → per-building
   yes/no with evidence → emailed report + Excel. Show a screenshot of a report card.
4. **Live demo / sample output** — the actual report: 2 confirmed + 1 needs-review,
   with the annotated images and the AI reasoning.
5. **How it works — the pipeline** — the 8-stage diagram (§2). One slide, visual.
6. **Deep dive: bounded AI verification** — Gemini normally; visual uncertainty →
   human review; one Grok request only for a technical Gemini failure.
7. **Why it's trustworthy** — recall-first CV + precision AI, footprint-centered,
   never-silently-wrong, fallbacks. (§3)
8. **The automation (n8n)** — sheet → emailed report flow diagram. (§6 Path A)
9. **External services & APIs** — the dependency table; note the YOLO model is
   *ours*, the rest are commodity APIs with fallbacks. (§4)
10. **Cost** — ~$0.10–0.30/address AI spend + ~$5/mo hosting; everything else free/
    cheap. (§7)
11. **Accuracy & limits (honest)** — confidence + needs-review, known limits,
    validation status. (§8)
12. **Tech stack** — the table; "Python + custom YOLO + Gemini with a bounded Grok fallback in Docker." (§9)
13. **Status & the ask** — built + demo-proven; needs a Parity-owned host (~$5/mo) +
    a live n8n test run; clean handoff. (§10)

---

## 12. Anticipated questions from Alex (prep the speaker)

- **"How accurate is it?"** → Uncertain cases are flagged for human review, not
  guessed. Validated on NYC samples; scaling the measurement.
- **"What does it cost to run?"** → Flat Render hosting plus per-address Gemini
  usage; Grok adds cost only after exceptional technical failures. Measure a
  representative batch before quoting a guaranteed per-address figure.
- **"What happens when it's not sure?"** → It says `needs_review` and shows the
  reasoning — a human decides. It does not fake confidence.
- **"What do we depend on / what's the risk?"** → Commodity APIs (Google, Mapbox,
  Gemini, and optional Grok fallback); the core detector is our own model. Main
  dependency risk is API pricing/availability, mitigated by fallbacks.
- **"What's left to go live?"** → Hosting is done — live on Render (Standard, 2 GB,
  $25/mo). Remaining: wire the n8n automation and run the end-to-end test.
- **"Can it scale to thousands?"** → Yes; cost scales linearly. The one engineering
  item for huge batches is report delivery (host images / split big reports).
- **"Who owns/controls it?"** → Should be a Parity account, not an individual's — that
  transfer is part of the ask.

---

## 13. Key facts cheat-sheet (for accurate slides)

- Detector: **custom-trained YOLO (YOLO26m)**, ensemble of 2, ~44 MB each, **ours**.
- AI review: **Google Gemini** (`gemini-3.6-flash`) normally; **xAI Grok**
  (`grok-4.3`) only as a one-request technical fallback.
- Geocode/imagery: **Google Maps** primary, **Mapbox** for dense urban + fallback.
- Footprints: **OSM → NYC planimetric → Microsoft**.
- Verdicts: confirmed / likely / **needs_review** / not_detected / no_cooling_tower /
  footprint_missing.
- Deliverables: **email-safe HTML report** + **Excel** spreadsheet.
- Two front doors: **web upload** + **stateless API** (`/api/run` — the n8n endpoint).
- Automation: **n8n** (Webhook → Sheet → LLM normalize → Render → format → email).
- Hosting: **Render** (Standard, 2 GB), Docker, $25/mo. **5 env vars** (4 API keys + `ANALYZE_API_KEY`).
- Cost: **~$0.10–0.30 / address** (AI) + $25/mo hosting; OSM/MS/NYC data free.
- Status: built + locally demo-proven; needs a Parity-owned host + live n8n test.
