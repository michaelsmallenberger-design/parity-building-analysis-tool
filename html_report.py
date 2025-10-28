"""
HTML Report Generator
Generates self-contained HTML reports with base64-embedded images.
"""
import os
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
    """Generate the HTML content."""

    timestamp = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    # Summary stats
    towers_detected = len(high_confidence) + len(needs_review)
    detection_rate = (towers_detected / total_count * 100) if total_count > 0 else 0

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
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 2rem;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            padding: 3rem;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        .header {{
            text-align: center;
            margin-bottom: 3rem;
            padding-bottom: 2rem;
            border-bottom: 3px solid #667eea;
        }}
        .header h1 {{
            color: #2c3e50;
            font-size: 2.5rem;
            margin-bottom: 0.5rem;
        }}
        .header .subtitle {{
            color: #7f8c8d;
            font-size: 1.1rem;
        }}
        .metadata {{
            background: #f8f9fa;
            padding: 1.5rem;
            border-radius: 8px;
            margin-bottom: 2rem;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
        }}
        .metadata-item {{
            text-align: center;
        }}
        .metadata-item .label {{
            font-size: 0.9rem;
            color: #7f8c8d;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .metadata-item .value {{
            font-size: 1.5rem;
            font-weight: bold;
            color: #2c3e50;
            margin-top: 0.25rem;
        }}
        .section {{
            margin: 3rem 0;
        }}
        .section-header {{
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1.5rem;
            padding-bottom: 0.75rem;
            border-bottom: 2px solid #e0e0e0;
        }}
        .section-header h2 {{
            font-size: 1.8rem;
            color: #2c3e50;
        }}
        .section-header .count {{
            background: #667eea;
            color: white;
            padding: 0.25rem 0.75rem;
            border-radius: 20px;
            font-size: 1rem;
            font-weight: bold;
        }}
        .section-header.high {{ border-bottom-color: #27ae60; }}
        .section-header.high h2 {{ color: #27ae60; }}
        .section-header.medium {{ border-bottom-color: #f39c12; }}
        .section-header.medium h2 {{ color: #e67e22; }}
        .section-header.low {{ border-bottom-color: #95a5a6; }}
        .section-header.low h2 {{ color: #7f8c8d; }}

        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
            background: white;
        }}
        thead {{
            background: #f8f9fa;
        }}
        th {{
            padding: 1rem;
            text-align: left;
            font-weight: 600;
            color: #2c3e50;
            border-bottom: 2px solid #dee2e6;
        }}
        td {{
            padding: 1rem;
            border-bottom: 1px solid #e9ecef;
            vertical-align: top;
        }}
        tr:hover {{
            background: #f8f9fa;
        }}
        .confidence-badge {{
            display: inline-block;
            padding: 0.4rem 0.8rem;
            border-radius: 20px;
            font-weight: bold;
            font-size: 0.9rem;
        }}
        .confidence-high {{
            background: #d4edda;
            color: #155724;
        }}
        .confidence-medium {{
            background: #fff3cd;
            color: #856404;
        }}
        .confidence-low {{
            background: #f8d7da;
            color: #721c24;
        }}
        .image-cell {{
            display: flex;
            gap: 1rem;
            align-items: center;
        }}
        .image-thumbnail {{
            max-width: 150px;
            max-height: 150px;
            border-radius: 4px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            cursor: pointer;
            transition: transform 0.2s;
        }}
        .image-thumbnail:hover {{
            transform: scale(1.05);
        }}
        .image-label {{
            font-size: 0.85rem;
            color: #7f8c8d;
            text-align: center;
            margin-top: 0.25rem;
        }}
        .image-container {{
            text-align: center;
        }}
        .footer {{
            margin-top: 4rem;
            padding-top: 2rem;
            border-top: 2px solid #e0e0e0;
            text-align: center;
            color: #7f8c8d;
            font-size: 0.9rem;
        }}
        .footer a {{
            color: #667eea;
            text-decoration: none;
        }}
        .footer a:hover {{
            text-decoration: underline;
        }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .container {{ box-shadow: none; }}
            .image-thumbnail {{ max-width: 100px; max-height: 100px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🏢 Cooling Tower Analysis Report</h1>
            <p class="subtitle">Rooftop Satellite Imagery Analysis</p>
            <p class="subtitle">Generated on {timestamp}</p>
        </div>

        <div class="metadata">
            <div class="metadata-item">
                <div class="label">Job ID</div>
                <div class="value" style="font-size: 1rem; word-break: break-all;">{job_id}</div>
            </div>
            <div class="metadata-item">
                <div class="label">Total Addresses</div>
                <div class="value">{total_count}</div>
            </div>
            <div class="metadata-item">
                <div class="label">Towers Detected</div>
                <div class="value">{towers_detected}</div>
            </div>
            <div class="metadata-item">
                <div class="label">Detection Rate</div>
                <div class="value">{detection_rate:.1f}%</div>
            </div>
        </div>
"""

    # High Confidence Section
    if high_confidence:
        html += f"""
        <div class="section">
            <div class="section-header high">
                <h2>✅ High Confidence Detections</h2>
                <span class="count">{len(high_confidence)}</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th style="width: 40px;">#</th>
                        <th>Address</th>
                        <th style="width: 120px;">Confidence</th>
                        <th style="width: 350px;">Images</th>
                    </tr>
                </thead>
                <tbody>
"""
        for item in high_confidence:
            html += f"""
                    <tr>
                        <td>{item['index']}</td>
                        <td>{item['address']}</td>
                        <td><span class="confidence-badge confidence-{item['confidence_class']}">{item['confidence_text']}</span></td>
                        <td>
                            <div class="image-cell">
"""
            if item['original_img']:
                html += f"""
                                <div class="image-container">
                                    <img src="{item['original_img']}" alt="Original" class="image-thumbnail">
                                    <div class="image-label">Original</div>
                                </div>
"""
            if item['result_img']:
                html += f"""
                                <div class="image-container">
                                    <img src="{item['result_img']}" alt="Result" class="image-thumbnail">
                                    <div class="image-label">Detection</div>
                                </div>
"""
            html += """
                            </div>
                        </td>
                    </tr>
"""
        html += """
                </tbody>
            </table>
        </div>
"""

    # Needs Review Section
    if needs_review:
        html += f"""
        <div class="section">
            <div class="section-header medium">
                <h2>⚠️ Needs Review</h2>
                <span class="count">{len(needs_review)}</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th style="width: 40px;">#</th>
                        <th>Address</th>
                        <th style="width: 120px;">Confidence</th>
                        <th style="width: 350px;">Images</th>
                    </tr>
                </thead>
                <tbody>
"""
        for item in needs_review:
            html += f"""
                    <tr>
                        <td>{item['index']}</td>
                        <td>{item['address']}</td>
                        <td><span class="confidence-badge confidence-{item['confidence_class']}">{item['confidence_text']}</span></td>
                        <td>
                            <div class="image-cell">
"""
            if item['original_img']:
                html += f"""
                                <div class="image-container">
                                    <img src="{item['original_img']}" alt="Original" class="image-thumbnail">
                                    <div class="image-label">Original</div>
                                </div>
"""
            if item['result_img']:
                html += f"""
                                <div class="image-container">
                                    <img src="{item['result_img']}" alt="Result" class="image-thumbnail">
                                    <div class="image-label">Detection</div>
                                </div>
"""
            html += """
                            </div>
                        </td>
                    </tr>
"""
        html += """
                </tbody>
            </table>
        </div>
"""

    # Low/No Detection Section
    if low_confidence:
        html += f"""
        <div class="section">
            <div class="section-header low">
                <h2>❌ Low/No Detection</h2>
                <span class="count">{len(low_confidence)}</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th style="width: 40px;">#</th>
                        <th>Address</th>
                        <th style="width: 120px;">Status</th>
                        <th style="width: 180px;">Image</th>
                    </tr>
                </thead>
                <tbody>
"""
        for item in low_confidence:
            html += f"""
                    <tr>
                        <td>{item['index']}</td>
                        <td>{item['address']}</td>
                        <td>
"""
            if item.get('error'):
                html += f"{item['confidence_text']}"
            else:
                html += f"<span class=\"confidence-badge confidence-{item['confidence_class']}\">{item['confidence_text']}</span>"

            html += """
                        </td>
                        <td>
"""
            if item['original_img']:
                html += f"""
                            <div class="image-container">
                                <img src="{item['original_img']}" alt="Original" class="image-thumbnail">
                                <div class="image-label">Original</div>
                            </div>
"""
            else:
                html += "N/A"

            html += """
                        </td>
                    </tr>
"""
        html += """
                </tbody>
            </table>
        </div>
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
