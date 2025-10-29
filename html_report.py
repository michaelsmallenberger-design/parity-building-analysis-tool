"""
HTML Report Generator
Generates self-contained HTML reports with base64-embedded images.
Optimized for easy copy-paste to spreadsheets.
"""
import os
import json
import base64
from typing import List, Dict, Any, Optional
from datetime import datetime

def _encode_image_to_base64(image_path: str) -> Optional[str]:
    """
    Encode an image file to base64 string.

    Args:
        image_path: Path to the image file

    Returns:
        Base64 encoded string or None if file doesn't exist
    """
    if not os.path.exists(image_path):
        return None

    try:
        with open(image_path, 'rb') as f:
            img_data = f.read()
            return base64.b64encode(img_data).decode('utf-8')
    except Exception as e:
        print(f"Error encoding image {image_path}: {e}")
        return None

def _get_confidence_category(confidence: Optional[float]) -> tuple:
    """
    Categorize confidence score.

    Returns:
        (category_name, css_class, display_text)
    """
    if confidence is None:
        return ("No Detection", "low", "N/A")

    percentage = confidence * 100
    if percentage >= 70:
        return ("High Confidence", "high", f"{percentage:.1f}%")
    elif percentage >= 40:
        return ("Needs Review", "medium", f"{percentage:.1f}%")
    else:
        return ("Low Confidence", "low", f"{percentage:.1f}%")

def generate_html_report(
    web_results: List[Dict[str, Any]],
    job_id: str,
    output_path: str,
    get_local_path_func=None
) -> str:
    """
    Generate a self-contained HTML report with embedded images.

    Args:
        web_results: List of result dictionaries from task processing
        job_id: Job identifier
        output_path: Where to save the HTML file
        get_local_path_func: Function to convert blob paths to local paths

    Returns:
        Path to the generated HTML file
    """

    # Organize results by confidence level
    high_confidence = []
    needs_review = []
    low_confidence = []

    for idx, result in enumerate(web_results, start=1):
        # Handle errors
        if result.get('error'):
            low_confidence.append({
                'index': idx,
                'address': result.get('address', 'Unknown'),
                'confidence': None,
                'confidence_text': result.get('error'),
                'original_img': None,
                'result_img': None,
                'error': True
            })
            continue

        # Get image paths and encode to base64
        original_url = result.get('original_image_url', '')
        result_url = result.get('result_image_url', '')

        # Convert URLs to local paths if function provided
        original_local = None
        result_local = None

        if get_local_path_func:
            # Extract blob path from URL (format: /files/uploads/job_id/filename.jpg)
            if original_url.startswith('/files/'):
                original_blob = original_url.replace('/files/', '')
                original_local = str(get_local_path_func(original_blob))
            if result_url.startswith('/files/'):
                result_blob = result_url.replace('/files/', '')
                result_local = str(get_local_path_func(result_blob))

        # Encode images to base64
        original_b64 = _encode_image_to_base64(original_local) if original_local else None
        result_b64 = _encode_image_to_base64(result_local) if result_local else None

        confidence = result.get('confidence_score')
        category, css_class, conf_text = _get_confidence_category(confidence)

        item = {
            'index': idx,
            'address': result.get('address', 'Unknown'),
            'confidence': confidence,
            'confidence_text': conf_text,
            'confidence_class': css_class,
            'original_img': f"data:image/jpeg;base64,{original_b64}" if original_b64 else None,
            'result_img': f"data:image/jpeg;base64,{result_b64}" if result_b64 else None,
            'error': False
        }

        # Categorize
        if confidence is not None:
            percentage = confidence * 100
            if percentage >= 70:
                high_confidence.append(item)
            elif percentage >= 40:
                needs_review.append(item)
            else:
                low_confidence.append(item)
        else:
            low_confidence.append(item)

    # Generate HTML
    html = _generate_html_content(
        high_confidence=high_confidence,
        needs_review=needs_review,
        low_confidence=low_confidence,
        job_id=job_id,
        total_count=len(web_results)
    )

    # Write to file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return output_path

def _generate_html_content(
    high_confidence: List[Dict],
    needs_review: List[Dict],
    low_confidence: List[Dict],
    job_id: str,
    total_count: int
) -> str:
    """Generate the HTML content - optimized for copy-paste to spreadsheets."""

    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    # Summary stats
    towers_detected = len(high_confidence) + len(needs_review)
    detection_rate = (towers_detected / total_count * 100) if total_count > 0 else 0

    # Combine all results into one list, sorted by index
    all_results = high_confidence + needs_review + low_confidence
    all_results.sort(key=lambda x: x['index'])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cooling Tower Analysis Report - {job_id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Helvetica', 'Arial', sans-serif;
            line-height: 1.5;
            color: #333;
            background: #f5f5f5;
            padding: 1.5rem;
        }}
        .container {{
            max-width: 1600px;
            margin: 0 auto;
            background: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .header {{
            text-align: center;
            margin-bottom: 2rem;
            padding-bottom: 1.5rem;
            border-bottom: 3px solid #667eea;
        }}
        .header h1 {{
            color: #2c3e50;
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }}
        .header .subtitle {{
            color: #7f8c8d;
            font-size: 1rem;
        }}
        .summary {{
            background: #f8f9fa;
            padding: 1rem;
            border-radius: 6px;
            margin-bottom: 2rem;
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
            gap: 1rem;
        }}
        .summary-item {{
            text-align: center;
        }}
        .summary-item .label {{
            font-size: 0.85rem;
            color: #7f8c8d;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .summary-item .value {{
            font-size: 1.25rem;
            font-weight: bold;
            color: #2c3e50;
            margin-top: 0.25rem;
        }}
        .instructions {{
            background: #e7f3ff;
            border-left: 4px solid #2196F3;
            padding: 1rem 1.5rem;
            margin-bottom: 1.5rem;
            border-radius: 4px;
        }}
        .instructions strong {{
            color: #1976D2;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
            background: white;
            font-size: 0.95rem;
        }}
        thead {{
            background: #2c3e50;
            color: white;
            position: sticky;
            top: 0;
        }}
        th {{
            padding: 0.75rem 0.5rem;
            text-align: left;
            font-weight: 600;
            border-right: 1px solid #4a5f7f;
        }}
        th:last-child {{
            border-right: none;
        }}
        td {{
            padding: 0.6rem 0.5rem;
            border-bottom: 1px solid #dee2e6;
            border-right: 1px solid #e9ecef;
        }}
        td:last-child {{
            border-right: none;
        }}
        tbody tr:nth-child(even) {{
            background: #f8f9fa;
        }}
        tbody tr:hover {{
            background: #e3f2fd;
        }}
        .detected-yes {{
            color: #2e7d32;
            font-weight: bold;
        }}
        .detected-no {{
            color: #7f8c8d;
        }}
        .view-images {{
            color: #1976D2;
            text-decoration: none;
            cursor: pointer;
            font-size: 0.9rem;
        }}
        .view-images:hover {{
            text-decoration: underline;
        }}
        .image-modal {{
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.9);
            overflow: auto;
        }}
        .modal-content {{
            margin: 2% auto;
            display: block;
            max-width: 90%;
            max-height: 85%;
        }}
        .close-modal {{
            position: absolute;
            top: 20px;
            right: 40px;
            color: #f1f1f1;
            font-size: 40px;
            font-weight: bold;
            cursor: pointer;
        }}
        .close-modal:hover {{
            color: #bbb;
        }}
        .modal-caption {{
            text-align: center;
            color: #ccc;
            padding: 10px;
            font-size: 1.1rem;
        }}
        .footer {{
            margin-top: 3rem;
            padding-top: 1.5rem;
            border-top: 2px solid #e0e0e0;
            text-align: center;
            color: #7f8c8d;
            font-size: 0.85rem;
        }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .container {{ box-shadow: none; }}
            .instructions, .view-images {{ display: none; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 Cooling Tower Analysis Report</h1>
            <p class="subtitle">Generated on {timestamp}</p>
        </div>

        <div class="summary">
            <div class="summary-item">
                <div class="label">Total Addresses</div>
                <div class="value">{total_count}</div>
            </div>
            <div class="summary-item">
                <div class="label">Towers Detected</div>
                <div class="value">{towers_detected}</div>
            </div>
            <div class="summary-item">
                <div class="label">Detection Rate</div>
                <div class="value">{detection_rate:.1f}%</div>
            </div>
            <div class="summary-item">
                <div class="label">Job ID</div>
                <div class="value" style="font-size: 0.9rem; word-break: break-all;">{job_id}</div>
            </div>
        </div>

        <div class="instructions">
            <strong>📋 Copy to Spreadsheet:</strong> Click and drag to select the table below, then press Ctrl+C (Windows) or Cmd+C (Mac) to copy.
            Paste directly into Excel or Google Sheets. Images can be viewed by clicking "View Images" links.
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 40px;">#</th>
                    <th>Address</th>
                    <th style="width: 100px;">Detected</th>
                    <th style="width: 100px;">Confidence</th>
                    <th style="width: 150px;">Notes</th>
                    <th style="width: 100px;">Images</th>
                </tr>
            </thead>
            <tbody>
"""

    # Generate table rows - single unified table
    for item in all_results:
        # Determine detected status
        if item.get('error'):
            detected = "Error"
            detected_class = "detected-no"
            confidence_display = "—"
            notes = item['confidence_text']
        elif item['confidence'] is not None and item['confidence'] >= 0.40:
            detected = "Yes"
            detected_class = "detected-yes"
            confidence_display = item['confidence_text']
            if item['confidence'] >= 0.70:
                notes = "High confidence"
            else:
                notes = "Review recommended"
        else:
            detected = "No"
            detected_class = "detected-no"
            confidence_display = item['confidence_text'] if not item.get('error') else "—"
            notes = "No detection" if not item.get('error') else ""

        html += f"""
                <tr>
                    <td>{item['index']}</td>
                    <td>{item['address']}</td>
                    <td class="{detected_class}">{detected}</td>
                    <td>{confidence_display}</td>
                    <td>{notes}</td>
                    <td>"""

        # Add view images link if images exist
        if item.get('original_img') or item.get('result_img'):
            html += f'<a href="#" class="view-images" onclick="showImages({item["index"]}, event); return false;">View Images</a>'
        else:
            html += "—"

        html += """</td>
                </tr>
"""

    html += """
            </tbody>
        </table>

        <!-- Image Modal -->
        <div id="imageModal" class="image-modal" onclick="closeModal()">
            <span class="close-modal" onclick="closeModal()">&times;</span>
            <div class="modal-caption" id="modalCaption"></div>
            <img class="modal-content" id="modalImage">
        </div>

        <script>
            // Store image data
            const imageData = {
"""

    # Add image data for JavaScript
    for item in all_results:
        if item.get('original_img') or item.get('result_img'):
            html += f"""
                {item['index']}: {{
                    address: {json.dumps(item['address'])},
                    original: {json.dumps(item.get('original_img', ''))},
                    result: {json.dumps(item.get('result_img', ''))}
                }},
"""

    html += """
            };

            function showImages(index, event) {
                event.stopPropagation();
                const data = imageData[index];
                if (!data) return;

                const modal = document.getElementById('imageModal');
                const img = document.getElementById('modalImage');
                const caption = document.getElementById('modalCaption');

                // Show result image if available, otherwise original
                img.src = data.result || data.original;
                caption.innerHTML = `<strong>#${index}: ${data.address}</strong><br>
                    ${data.result ? 'Detection Result' : 'Original Image'}<br>
                    <small>Click anywhere to close</small>`;

                modal.style.display = 'block';
            }

            function closeModal() {
                document.getElementById('imageModal').style.display = 'none';
            }

            // Close modal on Escape key
            document.addEventListener('keydown', function(event) {
                if (event.key === 'Escape') {
                    closeModal();
                }
            });
        </script>
"""

    # Footer
    html += """
        <div class="footer">
            <p><strong>Imagery:</strong> © Maxar (via Mapbox) | <strong>Map Data:</strong> © OpenStreetMap contributors</p>
            <p><strong>ML Framework:</strong> Ultralytics YOLO (AGPL-3.0) | Built for <strong>Parity Housing</strong></p>
            <p style="margin-top: 1rem;">
                <em>This report is self-contained and can be opened in any web browser.</em><br>
                <em>Images are embedded directly - no internet connection required.</em>
            </p>
        </div>
    </div>
</body>
</html>
"""

    return html
