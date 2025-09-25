# Reference Dockerfile for Cloud Run
FROM python:3.11-slim

# System deps (libgomp may be required by some ML libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for layered caching
COPY requirements.txt /app/requirements.txt
# If you don't have ultralytics in requirements, ensure it's there; and these:
# google-cloud-storage, google-cloud-tasks, gunicorn
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . /app

# Cloud Run expects to listen on $PORT
ENV PORT=8080

# Gunicorn entrypoint
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
