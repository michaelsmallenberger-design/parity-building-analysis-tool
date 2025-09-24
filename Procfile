web: gunicorn --timeout 300 --bind 0.0.0.0:8080 app:app
worker: python -m rq worker