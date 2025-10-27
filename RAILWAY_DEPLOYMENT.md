# Railway Deployment Guide

This guide will help you deploy the Building Analysis Tool to Railway (free hosting).

## Why Railway?

- **$5/month free credits** (renews monthly)
- **No credit card required** for signup
- **Zero maintenance** - deploy and forget
- **Your personal account** - survives employer leaving
- **Perfect for portfolio** - you own and control it

---

## Prerequisites

1. **Railway Account**: Sign up at https://railway.app (GitHub login recommended)
2. **Environment Variables**: You'll need:
   - `MAPBOX_API_KEY` - Your Mapbox API token
   - `GOOGLE_API_KEY` - Your Google Maps API key

---

## Deployment Steps

### Option 1: Deploy from GitHub (Recommended)

This is the easiest method and enables auto-deploys on code changes.

#### 1. Push to GitHub

```bash
# Initialize git if not already done
git init
git add .
git commit -m "Prepare for Railway deployment"

# Create a new repo on GitHub, then:
git remote add origin https://github.com/YOUR_USERNAME/parity-building-analysis-tool.git
git push -u origin main
```

#### 2. Deploy to Railway

1. Go to https://railway.app/new
2. Click **"Deploy from GitHub repo"**
3. Select your repository: `parity-building-analysis-tool`
4. Railway will automatically detect the Dockerfile and start building

#### 3. Configure Environment Variables

1. Go to your project in Railway dashboard
2. Click on your service
3. Go to **Variables** tab
4. Add these variables:
   ```
   MAPBOX_API_KEY=<your-mapbox-key>
   GOOGLE_API_KEY=<your-google-key>
   PORT=8080
   ```

#### 4. Wait for Deployment

- First build takes ~6-8 minutes (PyTorch installation)
- Subsequent builds are faster (cached layers)
- Railway will automatically assign a URL like: `https://parity-building-analysis-tool-production.up.railway.app`

#### 5. Test Your Deployment

1. Visit the Railway-provided URL
2. Upload a test CSV with a few addresses
3. Verify processing works and results appear

---

### Option 2: Deploy from CLI

If you prefer command-line deployment:

#### 1. Install Railway CLI

```bash
# Windows (PowerShell)
iwr https://railway.app/install.ps1 | iex

# macOS/Linux
curl -fsSL https://railway.app/install.sh | sh
```

#### 2. Login to Railway

```bash
railway login
```

#### 3. Initialize Project

```bash
cd parity-building-analysis-tool
railway init
```

#### 4. Add Environment Variables

```bash
railway variables set MAPBOX_API_KEY="your-key-here"
railway variables set GOOGLE_API_KEY="your-key-here"
railway variables set PORT=8080
```

#### 5. Deploy

```bash
railway up
```

#### 6. Get Your URL

```bash
railway domain
```

---

## Post-Deployment

### Share with Your Employer

1. Copy the Railway URL (e.g., `https://your-app.up.railway.app`)
2. Send them the URL - they can use it immediately
3. No need to give them Railway account access
4. App will keep running even after you leave the company

### Monitor Usage (Optional)

1. Railway dashboard shows:
   - Monthly usage ($X of $5 used)
   - Request count
   - Build/deploy logs
2. With ~1000 requests/month, you'll use ~$0.50-$1.00/month
3. Well within the $5 free credit limit

### If You Need to Redeploy

Railway auto-deploys when you push to GitHub, but you can also:

```bash
railway up
```

Or trigger a redeploy from the Railway dashboard (Deployments → Redeploy).

---

## Architecture Changes from Google Cloud

| Google Cloud | Railway |
|--------------|---------|
| Cloud Run | Railway container hosting |
| Cloud Tasks | Background thread worker |
| Cloud Storage (GCS) | Local filesystem (ephemeral) |
| GCS signed URLs | Local `/files/` route |
| service.yaml | railway.json + Dockerfile.railway |
| Cloud Secret Manager | Railway environment variables |

**Important**: Files are stored on disk (not object storage). This is fine because:
- Users download results immediately
- Results are available until next deploy
- Deployments are rare (set and forget)

---

## Troubleshooting

### Build Fails

**Error**: "Out of memory during build"
- Railway free tier has build memory limits
- Current Dockerfile is optimized (CPU-only PyTorch)
- If still failing, try reducing workers in Dockerfile

**Error**: "Dockerfile not found"
- Check that `Dockerfile.railway` exists
- Verify `railway.json` points to correct Dockerfile

### App Crashes

**Error**: "Application crashed on startup"
- Check Railway logs: `railway logs`
- Verify environment variables are set correctly
- Ensure MAPBOX_API_KEY and GOOGLE_API_KEY are valid

**Error**: "Worker not starting"
- Check logs for database initialization errors
- Verify storage directory can be created

### Jobs Not Processing

**Error**: "Job stays in 'queued' status"
- Background worker may not be running
- Check logs for worker thread errors
- Verify `/health` endpoint shows `"worker_running": true`

**Error**: "Job fails immediately"
- Check CSV format (must have 'Address' column)
- Verify Mapbox/Google API keys are valid
- Check logs for geocoding or image download errors

### Files Not Loading

**Error**: "Images don't appear in results"
- Check `/files/<path>` route is working
- Verify files were uploaded to storage directory
- Check browser console for 404 errors

---

## Cost Estimation

**Monthly Usage (1000 requests)**:
- Active compute time: ~5 hours
- Storage: ~500MB (ephemeral)
- Bandwidth: ~500MB download

**Railway Cost**:
- Free tier: $5 credit/month
- Your usage: ~$0.50/month
- **Out of pocket: $0** ✅

**Comparison**:
- Google Cloud: $0 (but requires credit card, billing account)
- Fly.io: $0 (but requires credit card)
- Railway: $0 (no credit card needed) ⭐

---

## Long-Term Maintenance

### Expected: ZERO

This is a "deploy and forget" setup:
- No database to maintain (SQLite)
- No object storage to manage
- No worker processes to monitor
- Auto-deploys from GitHub (if connected)

### If Something Breaks

1. Check Railway logs: Dashboard → Deployments → Logs
2. Restart service: Dashboard → Settings → Restart
3. Redeploy: Push to GitHub or `railway up`

### If You Need to Update Code

1. Make changes locally
2. Push to GitHub: `git push`
3. Railway auto-deploys (if GitHub connected)
4. Or manually: `railway up`

---

## For Your Portfolio/Interviews

When showcasing this project:

1. **Live Demo**: Share the Railway URL
2. **Ownership**: Mention you migrated from Google Cloud to Railway
3. **Architecture**: Explain the simplified architecture (threads vs Cloud Tasks)
4. **Trade-offs**: Discuss ephemeral storage vs object storage decisions
5. **Cost**: Highlight $0/month production deployment

**Sample Talking Point**:
> "I originally built this on Google Cloud Run with Cloud Tasks and Cloud Storage, but when handing it off, I migrated to Railway for zero-cost hosting. I replaced Cloud Tasks with a threaded job queue and Cloud Storage with local filesystem storage, reducing complexity while maintaining functionality. The app handles ~1000 requests/month at $0 cost, perfect for a low-traffic internal tool."

---

## Support

- Railway Docs: https://docs.railway.app
- Railway Community: https://discord.gg/railway
- This project's issues: (your GitHub repo)

---

## Summary Checklist

- [ ] Sign up for Railway account
- [ ] Get Mapbox API key
- [ ] Get Google API key
- [ ] Push code to GitHub
- [ ] Deploy to Railway (GitHub or CLI)
- [ ] Set environment variables
- [ ] Test with sample CSV
- [ ] Share URL with employer
- [ ] Add to portfolio

**You're done! The app will run indefinitely at $0 cost.** 🎉
