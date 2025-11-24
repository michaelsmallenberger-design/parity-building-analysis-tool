# Building Analysis Tool

AI-powered cooling tower detection from satellite imagery for HVAC service prioritization.

[![Built with Claude Code](https://img.shields.io/badge/Built%20with-Claude%20Code-7C3AED)](https://claude.ai/code)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Overview

Automatically detects cooling towers on building rooftops by analyzing satellite imagery. Built for Parity Inc. to streamline identification of buildings suitable for HVAC optimization services.

**Key Features:**
- 🎯 ~80% detection accuracy
- ⚡ 1-2 seconds per address
- 💰 $5/month hosting cost
- 📊 CSV input/output with clickable image URLs
- 🤖 **Built entirely with Claude Code** (AI-generated codebase)

## Quick Start

### Deploy Your Own Instance

```bash
# 1. Clone repository
git clone https://github.com/mahkuhse/parity-building-analysis-tool.git
cd parity-building-analysis-tool

# 2. Get Mapbox API key (free tier: 50k requests/month)
# Sign up at https://mapbox.com

# 3. Deploy to Railway (first month free, then $5/month)
# Visit https://railway.app
# - New Project → Deploy from GitHub repo
# - Set environment variables:
#   MAPBOX_API_KEY=<your-token>
#   PORT=8080

# 4. Upload CSV with addresses and get results
```

**Setup time:** 30-45 minutes

## Usage

### Input Format

CSV file with required `Address` column:

```csv
Address,Boro_Area,Zip
"123 Main St","Queens",11101
"456 Broadway","Manhattan",10013
```

### Results

- **Detection confidence:** High (≥70%), Needs Review (40-70%), No Detection (<40%)
- **Outputs:** Annotated satellite images with bounding boxes, CSV with clickable URLs
- **Copy-paste workflow:** Results paste directly into Google Sheets

## Technology Stack

- **Framework:** Flask (Python 3.8+)
- **ML Model:** YOLOv8 (Ultralytics)
- **Geocoding:** Mapbox + OpenStreetMap/Nominatim fallback
- **Imagery:** Mapbox Static Images API (768x768px @ zoom 19)
- **Hosting:** Railway.app
- **Database:** SQLite (ephemeral job queue)

## Built with Claude Code

**This entire project was developed using Claude Code, Anthropic's AI development tool.**

All application code (~2,000 lines), architecture decisions, accuracy optimizations, and deployment configurations were generated through natural language conversations with Claude Code. The only manual work was training the YOLO model on ~200 labeled images.

**Future development can continue using Claude Code** - no traditional programming required for maintenance, debugging, or feature additions.

Learn more: https://claude.ai/code

## Architecture

```
User Upload → Flask App → SQLite Queue → Background Worker
                                              ↓
                                         Geocode Address
                                              ↓
                                    Fetch Satellite Image
                                              ↓
                                         YOLO Detection
                                              ↓
                                    Annotate & Store Results
```

## Model Performance

**Training:**
- Dataset: ~200 rooftop images (NYC buildings)
- Framework: YOLOv8 nano model
- Training: 50-100 epochs
- Resources:
  - [YOLOv8 Tutorial Video](https://www.youtube.com/watch?v=r0RspiLG260)
  - [Train YOLO Models Guide](https://www.ejtech.io/learn/train-yolo-models)

**Accuracy:**
- Overall: ~80%
- Main errors: False positives from neighboring buildings (70% of errors)
- Geocoding errors: 20% of errors
- False negatives: 10% of errors

## Accuracy Improvements

Evolution from 70% → 80% through experimentation:

**Implemented:**
- ✅ Center-crop analysis (45% crop optimal)
- ✅ Confidence threshold tuning (0.40 optimal)
- ✅ Multi-scale analysis (optional, 2x slower)
- ✅ Distance-based filtering (optional)

**Tried but didn't work:**
- ❌ Higher zoom levels (20+) - images too blurry
- ❌ Circular crop masks - minimal gain
- ❌ Aggressive cropping (20%) - too many false negatives

**Next steps for improvement:**
1. Building footprint masking (NYC OpenData) - biggest potential impact
2. Enable distance filtering + multi-scale
3. Expand training dataset to 500-1000 images

See [HANDOVER.md](HANDOVER.md) for detailed experiments and recommendations.

## Cost

**Monthly operating cost: $5**

- Railway: $5/month Hobby plan (first month free with $5 credit)
- Mapbox: Free tier (50k requests/month)
- Current usage: ~500 addresses/month (~$0.50 compute)

Scales to 5,000 addresses/month at flat $5/month.

## Documentation

- **[HANDOVER.md](HANDOVER.md)** - Comprehensive quickstart guide for team takeover
- **[CLAUDE.md](CLAUDE.md)** - Technical documentation for developers
- **[RAILWAY_DEPLOYMENT.md](RAILWAY_DEPLOYMENT.md)** - Deployment guide
- **[ACCURACY_IMPROVEMENTS.md](ACCURACY_IMPROVEMENTS.md)** - Accuracy tuning details

## Configuration

Environment variables (Railway dashboard → Variables):

**Required:**
- `MAPBOX_API_KEY` - Mapbox API token
- `PORT` - Server port (default: 8080)

**Optional (accuracy tuning):**
- `YOLO_CONF` - Confidence threshold (default: 0.40)
- `MULTI_SCALE_ANALYSIS` - Dual-pass detection (default: false)
- `DISTANCE_FILTER_ENABLED` - Filter by distance from center (default: false)
- `MAPBOX_ZOOM` - Satellite zoom level (default: 19)

## File Structure

```
├── app_railway.py          # Flask application
├── worker.py               # Background job processor
├── job_queue.py            # SQLite queue management
├── utils.py                # Geocoding, imagery, YOLO
├── tasks_local.py          # Processing pipeline
├── models/
│   └── rooftop_model.pt    # Trained YOLOv8 model (19 MB)
├── templates/              # HTML interface
├── Dockerfile.railway      # Container config
└── requirements_railway.txt # Dependencies
```

## Contributing

This project is in maintenance mode. For modifications:

**Option 1: Use Claude Code (Recommended)**
- Continue development through conversational AI
- No programming experience required

**Option 2: Traditional Development**
- Python 3.8+ required
- See [CLAUDE.md](CLAUDE.md) for technical details

## License

MIT License

## Attribution

- **Imagery:** © Maxar (via Mapbox)
- **Map Data:** © OpenStreetMap contributors
- **ML Framework:** Ultralytics YOLO (AGPL-3.0)
- **Built for:** Parity Inc.
- **Built with:** Claude Code by Anthropic

## Contact

For questions about this project, see [HANDOVER.md](HANDOVER.md) for support resources.

---

**Note:** This tool was built for NYC addresses. Accuracy may vary in other regions. See documentation for adaptation guidance.
