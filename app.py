import os
import pandas as pd
from flask import Flask, request, render_template, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from redis import Redis
from rq import Queue
from rq.job import Job
from tasks import process_address_list

# Initialize the Flask application and Redis Queue
app = Flask(__name__)
redis_conn = Redis()
q = Queue(connection=redis_conn)

# Define folder paths
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('static/results', exist_ok=True)

@app.route('/')
def index():
    """Render the main upload page."""
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    """Handle address file upload and queue the background job."""
    if 'file' not in request.files:
        return redirect(request.url)
    
    file = request.files['file']
    if file.filename == '':
        return redirect(request.url)

    if file:
        filename = secure_filename(file.filename)
        filepath = os.path.abspath(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        file.save(filepath)
        
        try:
            df = pd.read_csv(filepath)
            total_addresses = len(df)
        except Exception:
            total_addresses = 0
        
        wsl_filepath = filepath.replace('C:\\', '/mnt/c/').replace('\\', '/')
        
        job = q.enqueue(process_address_list, wsl_filepath, job_timeout=3600)
        
        return redirect(url_for('results', job_id=job.get_id(), total=total_addresses))

@app.route('/results/<job_id>')
def results(job_id):
    """Render the results page for a specific job."""
    total_count = request.args.get('total', 0, type=int)
    return render_template('results.html', job_id=job_id, total_count=total_count)

@app.route('/status/<job_id>')
def job_status(job_id):
    """Fetch the status and result of a job, including progress."""
    job = Job.fetch(job_id, connection=redis_conn)

    if job.is_finished:
        return jsonify({'status': 'finished', 'result': job.result})
    elif job.is_failed:
        return jsonify({'status': 'failed'})
    else:
        progress = job.meta.get('progress', 0)
        total = job.meta.get('total', 1)
        return jsonify({
            'status': 'processing', 
            'progress': progress, 
            'total': total
        })

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    """Cancel a job in the queue."""
    try:
        job = Job.fetch(job_id, connection=redis_conn)
        job.cancel() # Use RQ's built-in cancel method
        return jsonify({'status': 'cancelled'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 404

if __name__ == '__main__':
    app.run(debug=True)