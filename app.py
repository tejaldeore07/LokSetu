from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, send_from_directory, jsonify
from google import genai
from PIL import Image
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from functools import wraps
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
import csv
import hashlib
import io
import mimetypes
import os
import re
import subprocess
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
import uuid
import requests

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "complaints.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
STATIC_FOLDER = os.path.join(BASE_DIR, "static")
USE_CLOUD_BACKEND = os.getenv("USE_CLOUD_BACKEND", "false").lower() == "true"
GCS_BUCKET = os.getenv("GCS_BUCKET", "")
SEED_CLOUD_DATA = os.getenv("SEED_CLOUD_DATA", "true").lower() == "true"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "mp4", "mov", "avi", "mkv"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv"}
NEWSAPI_KEY = (os.getenv("NEWSAPI_KEY") or os.getenv("NEWS_API_KEY") or "").strip()
GNEWS_API_KEY = (os.getenv("GNEWS_API_KEY") or os.getenv("GNEWS_KEY") or "").strip()
NEWSDATA_API_KEY = (os.getenv("NEWSDATA_API_KEY") or os.getenv("NEWSDATA_KEY") or "").strip()
NEWS_SEARCH_LOCATION = "Pune, Maharashtra"
NEWS_SEARCH_RADIUS_KM = 30
NEWS_KEYWORDS = [
    "pothole",
    "garbage",
    "sanitation",
    "sewage",
    "pollution",
    "road",
    "roads",
    "waste",
    "drainage",
    "water",
    "rain",
    "traffic",
    "waterlogging",
    "flooding",
    "broken road",
    "streetlight",
    "illegal dumping",
    "traffic congestion",
]
NEWS_SEARCH_QUERY = f'Pune civic issue ({" OR ".join(NEWS_KEYWORDS)})'
RSS_FEEDS = [
    ("Times of India", "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms"),
    ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
    ("Indian Express", "https://indianexpress.com/section/cities/pune/feed/"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"),
]

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "loksetu-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
ADMIN_PASSWORD = os.getenv("LOKSETU_ADMIN_PASSWORD", "tejal07")
if USE_CLOUD_BACKEND:
    if not os.getenv("FLASK_SECRET_KEY"):
        raise RuntimeError("FLASK_SECRET_KEY must be stored in Secret Manager for deployment.")
    if not os.getenv("LOKSETU_ADMIN_PASSWORD"):
        raise RuntimeError("LOKSETU_ADMIN_PASSWORD must be stored in Secret Manager for deployment.")

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None
_firestore_db = None
_storage_client = None
_cloud_initialized = False


@app.errorhandler(413)
def upload_too_large(_error):
    flash("File is too large. Please upload a file smaller than 25 MB.")
    return redirect(url_for("home"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if USE_CLOUD_BACKEND:
        ensure_cloud_data()
        return
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(STATIC_FOLDER, exist_ok=True)
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                severity INTEGER,
                priority TEXT,
                status TEXT,
                description TEXT,
                image_path TEXT,
                latitude REAL,
                longitude REAL
            )
            """
        )
        existing_columns = [row[1] for row in conn.execute("PRAGMA table_info(complaints)").fetchall()]
        migrations = {
            "created_at": "ALTER TABLE complaints ADD COLUMN created_at TEXT",
            "verification_count": "ALTER TABLE complaints ADD COLUMN verification_count INTEGER DEFAULT 0",
            "department": "ALTER TABLE complaints ADD COLUMN department TEXT",
            "duplicate_of": "ALTER TABLE complaints ADD COLUMN duplicate_of INTEGER",
            "area_label": "ALTER TABLE complaints ADD COLUMN area_label TEXT",
            "resolved_image_path": "ALTER TABLE complaints ADD COLUMN resolved_image_path TEXT",
            "resolved_at": "ALTER TABLE complaints ADD COLUMN resolved_at TEXT",
            "media_type": "ALTER TABLE complaints ADD COLUMN media_type TEXT DEFAULT 'image'",
            "track_code": "ALTER TABLE complaints ADD COLUMN track_code TEXT",
            "citizen_description": "ALTER TABLE complaints ADD COLUMN citizen_description TEXT",
            "source": "ALTER TABLE complaints ADD COLUMN source TEXT DEFAULT 'Citizen Upload'",
            "source_link": "ALTER TABLE complaints ADD COLUMN source_link TEXT",
        }
        for column, sql in migrations.items():
            if column not in existing_columns:
                conn.execute(sql)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS x_candidates (
                id TEXT PRIMARY KEY,
                platform_post_id TEXT UNIQUE NOT NULL,
                post_url TEXT NOT NULL,
                post_text TEXT,
                author_name TEXT,
                author_username TEXT,
                media_filename TEXT NOT NULL,
                media_type TEXT DEFAULT 'image',
                latitude REAL,
                longitude REAL,
                location_clue TEXT,
                category TEXT,
                severity INTEGER,
                description TEXT,
                department TEXT,
                urgency TEXT,
                post_created_at TEXT,
                scanned_at TEXT,
                status TEXT DEFAULT 'Pending',
                report_id INTEGER
            )
            """
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute("UPDATE complaints SET created_at = ? WHERE created_at IS NULL", (now,))
        conn.execute("UPDATE complaints SET verification_count = 0 WHERE verification_count IS NULL")
        conn.execute("UPDATE complaints SET media_type = 'image' WHERE media_type IS NULL")
        conn.execute("UPDATE complaints SET source = 'Citizen Upload' WHERE source IS NULL")
        rows = conn.execute("SELECT id, category, latitude, longitude, department, area_label, track_code FROM complaints").fetchall()
        for row in rows:
            department = row["department"] or department_for_category(row["category"])
            area = row["area_label"] or area_from_location(row["latitude"], row["longitude"])
            code = row["track_code"] or make_track_code(row["id"])
            conn.execute(
                "UPDATE complaints SET department=?, area_label=?, track_code=? WHERE id=?",
                (department, area, code, row["id"]),
            )
        conn.commit()


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("owner_logged_in"):
            return redirect(url_for("owner_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extension_for(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def media_type_for(filename):
    ext = extension_for(filename)
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "image"


def unique_filename(filename):
    safe_name = secure_filename(filename)
    if not safe_name:
        return f"report-{uuid.uuid4().hex}.jpg"
    name, ext = os.path.splitext(safe_name)
    return f"{name}-{uuid.uuid4().hex[:8]}{ext.lower()}"


def make_track_code(report_id):
    return f"LS-{int(report_id):05d}"


def parse_ai_response(text):
    data = {"Category": "Other", "Severity": "3", "Description": "Citizen report submitted for review."}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in data and value.strip():
            data[key] = value.strip()
    try:
        severity = int(data["Severity"])
    except ValueError:
        severity = 3
    severity = max(1, min(5, severity))
    return data["Category"], severity, data["Description"]


def parse_issue_ai_response(text):
    data = {
        "Category": "Other",
        "Severity": "3",
        "Description": "Citizen report submitted for review.",
        "Department": "",
        "Location Clue": "",
        "Urgency": "",
    }
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in data and value.strip():
            data[key] = value.strip()
    try:
        severity = int(data["Severity"])
    except ValueError:
        severity = 3
    severity = max(1, min(5, severity))
    category = data["Category"] or "Other"
    department = data["Department"] or department_for_category(category)
    return {
        "category": category,
        "severity": severity,
        "description": data["Description"],
        "department": department,
        "location_clue": data["Location Clue"],
        "urgency": data["Urgency"] or priority_from_severity(severity),
    }


def priority_from_severity(severity):
    if severity >= 5:
        return "Critical"
    if severity >= 4:
        return "High"
    if severity >= 3:
        return "Medium"
    return "Low"


def department_for_category(category):
    category = (category or "").lower()
    if "pothole" in category or "road" in category or "traffic" in category:
        return "Roads & Transport Department"
    if "garbage" in category or "pollution" in category:
        return "Sanitation & Environment Department"
    if "streetlight" in category or "light" in category:
        return "Electricity / Public Works Department"
    if "water" in category or "leak" in category:
        return "Water Supply Department"
    return "Municipal Field Operations"


def area_from_location(latitude, longitude):
    if not latitude or not longitude:
        return "Location pending"
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return "Location pending"
    return f"Zone {abs(int(lat * 10)) % 9 + 1} / Ward {abs(int(lon * 10)) % 24 + 1}"


def haversine_km(lat1, lon1, lat2, lon2):
    try:
        lat1, lon1, lat2, lon2 = map(float, (lat1, lon1, lat2, lon2))
    except (TypeError, ValueError):
        return None
    earth_radius = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return earth_radius * 2 * atan2(sqrt(a), sqrt(1 - a))


def find_duplicate(reports, category, latitude, longitude):
    if not latitude or not longitude:
        return None
    rows = sorted(reports, key=lambda row: int(row["id"]), reverse=True)
    for row in rows:
        if row.get("status") == "Resolved" or row.get("latitude") is None or row.get("longitude") is None:
            continue
        same_category = (row["category"] or "").strip().lower() == (category or "").strip().lower()
        distance = haversine_km(latitude, longitude, row["latitude"], row["longitude"])
        if same_category and distance is not None and distance <= 0.35:
            return row["id"]
    return None


def rows_to_reports(rows):
    return [dict(row) for row in rows]


def get_firestore_db():
    global _firestore_db
    if _firestore_db is None:
        from google.cloud import firestore

        _firestore_db = firestore.Client()
    return _firestore_db


def get_storage_client():
    global _storage_client
    if _storage_client is None:
        from google.cloud import storage

        _storage_client = storage.Client()
    return _storage_client


def ensure_cloud_data():
    global _cloud_initialized
    if _cloud_initialized:
        return
    if not GCS_BUCKET:
        raise RuntimeError("GCS_BUCKET must be configured when USE_CLOUD_BACKEND=true.")

    db = get_firestore_db()
    existing = list(db.collection("complaints").limit(1).stream())
    if not existing and SEED_CLOUD_DATA and os.path.exists(DB_PATH):
        seed_conn = sqlite3.connect(DB_PATH)
        seed_conn.row_factory = sqlite3.Row
        try:
            seed_rows = seed_conn.execute("SELECT * FROM complaints ORDER BY id ASC").fetchall()
            highest_id = 0
            for row in seed_rows:
                report = dict(row)
                report_id = int(report["id"])
                highest_id = max(highest_id, report_id)
                db.collection("complaints").document(str(report_id)).set(report)
            if highest_id:
                db.collection("_meta").document("counters").set(
                    {"complaints": highest_id},
                    merge=True,
                )
        finally:
            seed_conn.close()
    _cloud_initialized = True


def next_cloud_report_id():
    from google.cloud import firestore

    db = get_firestore_db()
    counter_ref = db.collection("_meta").document("counters")
    transaction = db.transaction()

    @firestore.transactional
    def increment_counter(txn):
        snapshot = counter_ref.get(transaction=txn)
        current = int(snapshot.to_dict().get("complaints", 0)) if snapshot.exists else 0
        next_id = current + 1
        txn.set(counter_ref, {"complaints": next_id}, merge=True)
        return next_id

    return increment_counter(transaction)


def get_report_by_id(report_id):
    init_db()
    if USE_CLOUD_BACKEND:
        snapshot = get_firestore_db().collection("complaints").document(str(int(report_id))).get()
        return snapshot.to_dict() if snapshot.exists else None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE id=?", (report_id,)).fetchone()
    return dict(row) if row else None


def get_report_by_track_code(track_code):
    init_db()
    if USE_CLOUD_BACKEND:
        rows = (
            get_firestore_db()
            .collection("complaints")
            .where("track_code", "==", track_code)
            .limit(1)
            .stream()
        )
        snapshot = next(iter(rows), None)
        return snapshot.to_dict() if snapshot else None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM complaints WHERE track_code=?", (track_code,)).fetchone()
    return dict(row) if row else None


def create_report(report):
    init_db()
    if USE_CLOUD_BACKEND:
        report_id = next_cloud_report_id()
        stored = dict(report)
        stored["id"] = report_id
        stored["track_code"] = make_track_code(report_id)
        get_firestore_db().collection("complaints").document(str(report_id)).set(stored)
        return report_id

    columns = list(report.keys())
    placeholders = ", ".join("?" for _ in columns)
    with get_db() as conn:
        cursor = conn.execute(
            f"INSERT INTO complaints ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(report[column] for column in columns),
        )
        report_id = cursor.lastrowid
        conn.execute(
            "UPDATE complaints SET track_code=? WHERE id=?",
            (make_track_code(report_id), report_id),
        )
        conn.commit()
    return report_id


def update_report_fields(report_id, **fields):
    init_db()
    if not fields:
        return
    if USE_CLOUD_BACKEND:
        get_firestore_db().collection("complaints").document(str(int(report_id))).update(fields)
        return
    assignments = ", ".join(f"{column}=?" for column in fields)
    with get_db() as conn:
        conn.execute(
            f"UPDATE complaints SET {assignments} WHERE id=?",
            (*fields.values(), report_id),
        )
        conn.commit()


def increment_verification(report_id):
    init_db()
    if USE_CLOUD_BACKEND:
        from google.cloud import firestore

        get_firestore_db().collection("complaints").document(str(int(report_id))).update(
            {"verification_count": firestore.Increment(1)}
        )
        return
    with get_db() as conn:
        conn.execute(
            "UPDATE complaints SET verification_count = COALESCE(verification_count, 0) + 1 WHERE id=?",
            (report_id,),
        )
        conn.commit()


def delete_report_record(report_id):
    init_db()
    if USE_CLOUD_BACKEND:
        get_firestore_db().collection("complaints").document(str(int(report_id))).delete()
        return
    with get_db() as conn:
        conn.execute("DELETE FROM complaints WHERE id=?", (report_id,))
        conn.commit()


def save_media(file_storage, filename):
    if USE_CLOUD_BACKEND:
        temp_path = os.path.join(tempfile.gettempdir(), filename)
        file_storage.save(temp_path)
        bucket = get_storage_client().bucket(GCS_BUCKET)
        bucket.blob(f"reports/{filename}").upload_from_filename(
            temp_path,
            content_type=file_storage.mimetype or mimetypes.guess_type(filename)[0],
        )
        return temp_path

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(STATIC_FOLDER, exist_ok=True)
    upload_path = os.path.join(UPLOAD_FOLDER, filename)
    static_path = os.path.join(STATIC_FOLDER, filename)
    file_storage.save(upload_path)
    shutil.copy(upload_path, static_path)
    return upload_path


def save_downloaded_media(content, filename, content_type=None):
    stream = io.BytesIO(content)
    storage = FileStorage(stream=stream, filename=filename, content_type=content_type)
    return save_media(storage, filename)


def extract_video_frames(video_path, max_frames=4):
    frame_paths = []
    temp_dir = tempfile.mkdtemp(prefix="loksetu-video-")
    try:
        try:
            import cv2

            capture = cv2.VideoCapture(video_path)
            total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            if total > 0:
                positions = [int(total * pct) for pct in (0.15, 0.35, 0.6, 0.85)][:max_frames]
            else:
                positions = list(range(max_frames))
            for index, frame_no in enumerate(positions):
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
                ok, frame = capture.read()
                if not ok:
                    continue
                frame_path = os.path.join(temp_dir, f"frame-{index}.jpg")
                cv2.imwrite(frame_path, frame)
                frame_paths.append(frame_path)
            capture.release()
        except Exception:
            frame_paths = []

        if not frame_paths:
            for second in (1, 3, 6, 10)[:max_frames]:
                frame_path = os.path.join(temp_dir, f"frame-{second}.jpg")
                command = [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    str(second),
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    frame_path,
                ]
                try:
                    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=20)
                    if os.path.exists(frame_path):
                        frame_paths.append(frame_path)
                except Exception:
                    continue
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return [], None
    return frame_paths, temp_dir


def gemini_issue_prompt(media_context):
    return f"""
You are an AI civic issue inspector for a local government reporting platform.
Analyze the provided {media_context}. Identify the most important visible civic issue.
Use visible scene details and any readable text. If this is a social media screenshot, read the post text too.

Choose exactly one category:
1. Pothole
2. Garbage
3. Streetlight
4. Water Leak
5. Pollution
6. Road Traffic
7. Other

Respond ONLY in this format:
Category:
Severity:
Description:
Department:
Location Clue:
Urgency:
    """


def analyze_image_with_gemini(image_path, citizen_description="", media_context="image"):
    if client is None:
        return {
            "category": "Other",
            "severity": 3,
            "description": citizen_description or "Image uploaded successfully. Add GEMINI_API_KEY in .env for AI classification.",
            "department": "Municipal Field Operations",
            "location_clue": "",
            "urgency": "Medium",
        }
    try:
        with Image.open(image_path) as img:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[gemini_issue_prompt(media_context), img],
            )
        result = parse_issue_ai_response(response.text or "")
        if citizen_description:
            result["description"] = f"{result['description']} Citizen note: {citizen_description}"
        return result
    except Exception:
        return {
            "category": "Other",
            "severity": 3,
            "description": citizen_description or "Report received. AI analysis is temporarily unavailable and requires official review.",
            "department": "Municipal Field Operations",
            "location_clue": "",
            "urgency": "Medium",
        }


def analyze_video_with_gemini(video_path, citizen_description=""):
    if client is None:
        return {
            "category": "Other",
            "severity": 3,
            "description": citizen_description or "Video uploaded successfully. Add GEMINI_API_KEY in .env for AI video frame analysis.",
            "department": "Municipal Field Operations",
            "location_clue": "",
            "urgency": "Medium",
        }

    frame_paths, temp_dir = extract_video_frames(video_path)
    if not frame_paths:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return {
            "category": "Other",
            "severity": 3,
            "description": citizen_description or "Video report received. Frame extraction is unavailable and requires official review.",
            "department": "Municipal Field Operations",
            "location_clue": "",
            "urgency": "Medium",
        }
    images = []
    try:
        for frame_path in frame_paths:
            image = Image.open(frame_path)
            images.append(image.copy())
            image.close()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                gemini_issue_prompt("representative video frames"),
                *images,
            ],
        )
        result = parse_issue_ai_response(response.text or "")
        if citizen_description:
            result["description"] = f"{result['description']} Citizen note: {citizen_description}"
        return result
    except Exception:
        return {
            "category": "Other",
            "severity": 3,
            "description": citizen_description or "Video report received. AI frame analysis is temporarily unavailable and requires official review.",
            "department": "Municipal Field Operations",
            "location_clue": "",
            "urgency": "Medium",
        }
    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def analyze_media(analysis_path, media_type, citizen_description="", social=False):
    if media_type == "video":
        return analyze_video_with_gemini(analysis_path, citizen_description)
    context = "social media screenshot with visible post text" if social else "image"
    return analyze_image_with_gemini(analysis_path, citizen_description, context)


def create_complaint_from_analysis(filename, media_type, ai_result, latitude=None, longitude=None, citizen_description="", source="Citizen Upload", source_link=None):
    category = ai_result["category"]
    severity = ai_result["severity"]
    priority = priority_from_severity(severity)
    department = ai_result.get("department") or department_for_category(category)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    geo_area = area_from_location(latitude, longitude)
    area_label = geo_area if geo_area != "Location pending" else (ai_result.get("location_clue") or geo_area)
    duplicate_of = find_duplicate(load_reports("DESC"), category, latitude, longitude)

    return create_report(
        {
            "category": category,
            "severity": severity,
            "priority": priority,
            "status": "Reported",
            "description": ai_result["description"],
            "image_path": filename,
            "latitude": latitude,
            "longitude": longitude,
            "created_at": created_at,
            "verification_count": 0,
            "department": department,
            "duplicate_of": duplicate_of,
            "area_label": area_label,
            "media_type": media_type,
            "citizen_description": citizen_description,
            "resolved_image_path": None,
            "resolved_at": None,
            "source": source,
            "source_link": source_link,
        }
    )


class NewsMonitorError(RuntimeError):
    pass


def news_request(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "LOKSETU civic news intelligence monitor"},
        timeout=18,
    )
    response.raise_for_status()
    return response


def x_candidate_exists(platform_post_id):
    init_db()
    if USE_CLOUD_BACKEND:
        matches = (
            get_firestore_db()
            .collection("x_candidates")
            .where("platform_post_id", "==", str(platform_post_id))
            .limit(1)
            .stream()
        )
        return next(iter(matches), None) is not None
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM x_candidates WHERE platform_post_id=?",
            (str(platform_post_id),),
        ).fetchone()
    return row is not None


def article_id(article_url):
    return hashlib.sha1(article_url.encode("utf-8")).hexdigest()[:18]


def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", value).strip()


def article_has_civic_signal(article):
    text = " ".join(
        [
            article.get("title", ""),
            article.get("description", ""),
            article.get("content", ""),
            article.get("source_name", ""),
        ]
    ).lower()
    if "pune" not in text and "pimpri" not in text and "maharashtra" not in text:
        return False
    return any(keyword.lower() in text for keyword in NEWS_KEYWORDS)


def normalize_article(source_name, title, url, image_url=None, description="", published_at="", content=""):
    title = clean_text(title)
    url = (url or "").strip()
    if not title or not url:
        return None
    return {
        "platform_post_id": f"news-{article_id(url)}",
        "post_url": url,
        "post_text": title,
        "description_text": clean_text(description),
        "content": clean_text(content),
        "author_name": source_name,
        "author_username": source_name.lower().replace(" ", "-"),
        "source_media_url": image_url,
        "media_type": "image",
        "latitude": None,
        "longitude": None,
        "location_clue": NEWS_SEARCH_LOCATION,
        "post_created_at": published_at or "",
    }


def fetch_newsapi_articles():
    if not NEWSAPI_KEY:
        return []
    response = news_request(
        "https://newsapi.org/v2/everything",
        {
            "apiKey": NEWSAPI_KEY,
            "q": NEWS_SEARCH_QUERY,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
        },
    )
    articles = []
    for item in response.json().get("articles", []):
        source = (item.get("source") or {}).get("name") or "NewsAPI"
        article = normalize_article(
            source,
            item.get("title"),
            item.get("url"),
            item.get("urlToImage"),
            item.get("description"),
            item.get("publishedAt"),
            item.get("content"),
        )
        if article:
            articles.append(article)
    return articles


def fetch_gnews_articles():
    if not GNEWS_API_KEY:
        return []
    response = news_request(
        "https://gnews.io/api/v4/search",
        {
            "token": GNEWS_API_KEY,
            "q": NEWS_SEARCH_QUERY,
            "lang": "en",
            "country": "in",
            "max": 10,
        },
    )
    articles = []
    for item in response.json().get("articles", []):
        source = (item.get("source") or {}).get("name") or "GNews"
        article = normalize_article(
            source,
            item.get("title"),
            item.get("url"),
            item.get("image"),
            item.get("description"),
            item.get("publishedAt"),
            item.get("content"),
        )
        if article:
            articles.append(article)
    return articles


def fetch_newsdata_articles():
    if not NEWSDATA_API_KEY:
        return []
    response = news_request(
        "https://newsdata.io/api/1/news",
        {
            "apikey": NEWSDATA_API_KEY,
            "q": "Pune civic issue OR pothole OR garbage OR pollution OR waterlogging",
            "language": "en",
            "country": "in",
            "size": 10,
        },
    )
    articles = []
    for item in response.json().get("results", []):
        source = item.get("source_name") or "NewsData.io"
        article = normalize_article(
            source,
            item.get("title"),
            item.get("link"),
            item.get("image_url"),
            item.get("description"),
            item.get("pubDate"),
            item.get("content"),
        )
        if article:
            articles.append(article)
    return articles


def rss_item_text(item, tag):
    child = item.find(tag)
    return child.text if child is not None and child.text else ""


def rss_image_url(item):
    for enclosure in item.findall("enclosure"):
        url = enclosure.attrib.get("url")
        media_type = enclosure.attrib.get("type", "")
        if url and media_type.startswith("image/"):
            return url
    for child in item:
        if child.tag.lower().endswith("content") and child.attrib.get("url"):
            return child.attrib["url"]
    description = rss_item_text(item, "description")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description, flags=re.IGNORECASE)
    return match.group(1) if match else None


def fetch_rss_articles():
    articles = []
    for source_name, feed_url in RSS_FEEDS:
        try:
            response = news_request(feed_url)
            root = ET.fromstring(response.content)
        except Exception:
            continue
        for item in root.findall(".//item")[:10]:
            article = normalize_article(
                source_name,
                rss_item_text(item, "title"),
                rss_item_text(item, "link"),
                rss_image_url(item),
                rss_item_text(item, "description"),
                rss_item_text(item, "pubDate"),
            )
            if article:
                articles.append(article)
    return articles


def fetch_article_image_from_page(article_url):
    try:
        response = news_request(article_url)
    except Exception:
        return None
    html = response.text[:250000]
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ):
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return requests.compat.urljoin(response.url, match.group(1))
    return None


def search_news_posts():
    providers = [
        fetch_newsapi_articles,
        fetch_gnews_articles,
        fetch_newsdata_articles,
        fetch_rss_articles,
    ]
    articles = []
    seen = set()
    for provider in providers:
        try:
            provider_articles = provider()
        except Exception:
            continue
        for article in provider_articles:
            if article["post_url"] in seen or not article_has_civic_signal(article):
                continue
            seen.add(article["post_url"])
            articles.append(article)
    return articles


def save_x_candidate(candidate):
    init_db()
    stored = dict(candidate)
    stored.setdefault("id", uuid.uuid4().hex)
    if USE_CLOUD_BACKEND:
        get_firestore_db().collection("x_candidates").document(stored["id"]).set(stored)
        return stored["id"]
    columns = list(stored.keys())
    placeholders = ", ".join("?" for _ in columns)
    with get_db() as conn:
        conn.execute(
            f"INSERT INTO x_candidates ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(stored[column] for column in columns),
        )
        conn.commit()
    return stored["id"]


def load_x_candidates(status="Pending", limit=12):
    init_db()
    if USE_CLOUD_BACKEND:
        candidates = [
            snapshot.to_dict()
            for snapshot in get_firestore_db().collection("x_candidates").stream()
        ]
        filtered = [item for item in candidates if item.get("status") == status]
        return sorted(filtered, key=lambda item: item.get("scanned_at", ""), reverse=True)[:limit]
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM x_candidates WHERE status=? ORDER BY scanned_at DESC LIMIT ?",
            (status, int(limit)),
        ).fetchall()
    return rows_to_reports(rows)


def get_x_candidate(candidate_id):
    init_db()
    if USE_CLOUD_BACKEND:
        snapshot = get_firestore_db().collection("x_candidates").document(candidate_id).get()
        return snapshot.to_dict() if snapshot.exists else None
    with get_db() as conn:
        row = conn.execute("SELECT * FROM x_candidates WHERE id=?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def update_x_candidate(candidate_id, **fields):
    if not fields:
        return
    init_db()
    if USE_CLOUD_BACKEND:
        get_firestore_db().collection("x_candidates").document(candidate_id).update(fields)
        return
    assignments = ", ".join(f"{column}=?" for column in fields)
    with get_db() as conn:
        conn.execute(
            f"UPDATE x_candidates SET {assignments} WHERE id=?",
            (*fields.values(), candidate_id),
        )
        conn.commit()


def download_news_media(source_url, platform_post_id, article_url=None):
    if not source_url and article_url:
        source_url = fetch_article_image_from_page(article_url)
    if not source_url:
        raise NewsMonitorError("A news article did not expose a usable image.")
    try:
        response = requests.get(
            source_url,
            headers={"User-Agent": "LOKSETU civic intelligence monitor"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NewsMonitorError("A news image could not be downloaded.") from exc
    if len(response.content) > app.config["MAX_CONTENT_LENGTH"]:
        raise NewsMonitorError("A news image was larger than the 25 MB safety limit.")
    content_type = response.headers.get("content-type", "").split(";")[0].lower()
    if not content_type.startswith("image/"):
        raise NewsMonitorError("A news article returned an unsupported media format.")
    extension = mimetypes.guess_extension(content_type) or ".jpg"
    filename = unique_filename(f"{platform_post_id}{extension}")
    return filename, save_downloaded_media(response.content, filename, content_type)


def scan_x_for_civic_issues(max_candidates=5):
    posts = search_news_posts()
    added = 0
    skipped = 0
    errors = 0
    for post in posts:
        if added >= max_candidates:
            break
        if x_candidate_exists(post["platform_post_id"]):
            skipped += 1
            continue
        filename = None
        analysis_path = None
        try:
            filename, analysis_path = download_news_media(
                post["source_media_url"],
                post["platform_post_id"],
                post["post_url"],
            )
            note = (
                f"News source: {post['author_name']}. Headline: {post['post_text']}. "
                f"Article summary: {post.get('description_text') or post.get('content')}. "
                f"Location context: {post['location_clue']}."
            )
            ai_result = analyze_media(
                analysis_path,
                "image",
                citizen_description=note,
                social=True,
            )
            save_x_candidate(
                {
                    "id": post["platform_post_id"],
                    "platform_post_id": post["platform_post_id"],
                    "post_url": post["post_url"],
                    "post_text": post["post_text"] + (f" - {post.get('description_text')}" if post.get("description_text") else ""),
                    "author_name": post["author_name"],
                    "author_username": post["author_username"],
                    "media_filename": filename,
                    "media_type": "image",
                    "latitude": post["latitude"],
                    "longitude": post["longitude"],
                    "location_clue": (
                        ai_result.get("location_clue")
                        or post["location_clue"]
                        or NEWS_SEARCH_LOCATION
                    ),
                    "category": ai_result["category"],
                    "severity": ai_result["severity"],
                    "description": ai_result["description"],
                    "department": ai_result.get("department") or department_for_category(ai_result["category"]),
                    "urgency": ai_result.get("urgency") or priority_from_severity(ai_result["severity"]),
                    "post_created_at": post["post_created_at"],
                    "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "status": "Pending",
                    "report_id": None,
                }
            )
            added += 1
        except Exception:
            errors += 1
            if filename:
                delete_media(filename)
        finally:
            if USE_CLOUD_BACKEND and analysis_path and os.path.exists(analysis_path):
                os.remove(analysis_path)
    return {"added": added, "skipped": skipped, "errors": errors, "found": len(posts)}


def report_payload(report_id, ai_result):
    report = get_report_by_id(report_id)
    return {
        "report_id": report_id,
        "track_code": report.get("track_code") if report else None,
        "issue_summary": ai_result["description"],
        "category": ai_result["category"],
        "urgency": ai_result.get("urgency") or priority_from_severity(ai_result["severity"]),
        "severity": ai_result["severity"],
        "location_clue": ai_result.get("location_clue") or (report.get("area_label") if report else ""),
        "suggested_department": ai_result.get("department") or (report.get("department") if report else ""),
    }


def wants_json_response():
    return request.is_json or "application/json" in request.headers.get("Accept", "")


def fetch_public_social_media(link):
    if not link:
        return None, None, None
    try:
        response = requests.get(
            link,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 LokSetu civic report importer"},
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception:
        return None, None, None

    content_type = response.headers.get("content-type", "").split(";")[0].lower()
    if content_type.startswith(("image/", "video/")):
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        filename = unique_filename(f"social-media{ext}")
        return response.content, filename, content_type

    html = response.text[:250000]
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
    ):
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if not match:
            continue
        media_url_value = requests.compat.urljoin(response.url, match.group(1))
        try:
            media_response = requests.get(
                media_url_value,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0 LokSetu civic report importer"},
            )
            media_response.raise_for_status()
            media_type = media_response.headers.get("content-type", "").split(";")[0].lower()
            if media_type.startswith(("image/", "video/")):
                ext = mimetypes.guess_extension(media_type) or os.path.splitext(media_url_value.split("?")[0])[1] or ".jpg"
                filename = unique_filename(f"social-media{ext}")
                return media_response.content, filename, media_type
        except Exception:
            continue
    return None, None, None


def delete_media(filename):
    if not filename:
        return
    if USE_CLOUD_BACKEND:
        blob = get_storage_client().bucket(GCS_BUCKET).blob(f"reports/{filename}")
        if blob.exists():
            blob.delete()
        return
    for folder in (STATIC_FOLDER, UPLOAD_FOLDER):
        file_path = os.path.join(folder, filename)
        if os.path.exists(file_path):
            os.remove(file_path)


def media_url(filename):
    if not filename:
        return ""
    if os.path.exists(os.path.join(STATIC_FOLDER, filename)):
        return url_for("static", filename=filename)
    return url_for("cloud_media", filename=filename)


app.jinja_env.globals["media_url"] = media_url


@app.route("/media/<path:filename>")
def cloud_media(filename):
    safe_name = secure_filename(filename)
    if safe_name != filename:
        return Response("Invalid media path.", status=400)
    local_path = os.path.join(STATIC_FOLDER, safe_name)
    if os.path.exists(local_path):
        return send_from_directory(STATIC_FOLDER, safe_name)
    if not USE_CLOUD_BACKEND:
        return Response("Media not found.", status=404)
    blob = get_storage_client().bucket(GCS_BUCKET).blob(f"reports/{safe_name}")
    if not blob.exists():
        return Response("Media not found.", status=404)
    data = blob.download_as_bytes()
    response = Response(
        data,
        mimetype=blob.content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
    )
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def build_dashboard_data(reports):
    total = len(reports)
    high = sum(1 for r in reports if int(r["severity"] or 0) >= 4)
    in_progress = sum(1 for r in reports if r["status"] in ("Under Review", "Assigned"))
    resolved = sum(1 for r in reports if r["status"] == "Resolved")
    verified = sum(int(r["verification_count"] or 0) for r in reports)
    duplicates = sum(1 for r in reports if r.get("duplicate_of"))
    community_score = total * 10 + resolved * 50 + verified * 7 + duplicates * 2

    categories = {}
    departments = {}
    priorities = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    statuses = {"Reported": 0, "Under Review": 0, "Assigned": 0, "Resolved": 0}
    areas = {}
    for r in reports:
        categories[r["category"]] = categories.get(r["category"], 0) + 1
        departments[r["department"]] = departments.get(r["department"], 0) + 1
        areas[r["area_label"]] = areas.get(r["area_label"], 0) + 1
        priorities[r["priority"]] = priorities.get(r["priority"], 0) + 1
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1

    top_category = max(categories, key=categories.get) if categories else "No reports yet"
    top_area = max(areas, key=areas.get) if areas else "No area yet"
    avg_severity = round(sum(int(r["severity"] or 0) for r in reports) / total, 1) if total else 0
    resolution_rate = round((resolved / total) * 100) if total else 0
    urgent_open = sum(1 for r in reports if int(r["severity"] or 0) >= 4 and r["status"] != "Resolved")

    if urgent_open >= 3:
        risk_level = "High civic risk"
        risk_advice = "Create an urgent field action plan for high-severity unresolved reports."
    elif urgent_open >= 1:
        risk_level = "Moderate civic risk"
        risk_advice = "Review severe open reports before the next civic cycle."
    else:
        risk_level = "Stable"
        risk_advice = "No urgent unresolved cluster is visible right now."

    mission_goal = 20
    mission_progress = min(high, mission_goal)
    trust_level = "New Reporter"
    if community_score >= 700:
        trust_level = "Civic Champion"
    elif community_score >= 300:
        trust_level = "Area Guardian"
    elif community_score >= 100:
        trust_level = "Trusted Reporter"
    elif community_score >= 40:
        trust_level = "Verified Citizen"

    reward_cards = [
        {
            "name": "Report Highlight",
            "cost": 80,
            "desc": "Your next valid report is highlighted for faster official review.",
        },
        {
            "name": "Civic Impact Certificate",
            "cost": 200,
            "desc": (
                "Receive a verified LOKSETU certificate for college portfolios, "
                "volunteer applications, and community recognition."
            ),
            "image": "loksetu-civic-certificate-demo.png",
        },
        {
            "name": "Trusted Verifier",
            "cost": 300,
            "desc": (
                "Your issue verifications carry more trust and you receive early "
                "access to special community missions."
            ),
        },
        {
            "name": "Civic Champion",
            "cost": 700,
            "desc": (
                "Lead local missions, receive top public recognition, and become "
                "eligible for ward consultations and future partner benefits."
            ),
        },
    ]

    return {
        "total_reports": total,
        "high_severity": high,
        "in_progress": in_progress,
        "resolved": resolved,
        "verified": verified,
        "duplicates": duplicates,
        "community_score": community_score,
        "mission_goal": mission_goal,
        "mission_progress": mission_progress,
        "trust_level": trust_level,
        "reward_cards": reward_cards,
        "categories": categories,
        "departments": departments,
        "priorities": priorities,
        "statuses": statuses,
        "areas": areas,
        "max_category": max(categories.values()) if categories else 1,
        "max_status": max(statuses.values()) if statuses else 1,
        "max_priority": max(priorities.values()) if priorities else 1,
        "max_department": max(departments.values()) if departments else 1,
        "max_area": max(areas.values()) if areas else 1,
        "chart_total": max(total, 1),
        "top_category": top_category,
        "top_area": top_area,
        "avg_severity": avg_severity,
        "resolution_rate": resolution_rate,
        "urgent_open": urgent_open,
        "risk_level": risk_level,
        "risk_advice": risk_advice,
    }


def load_reports(order="ASC"):
    init_db()
    if USE_CLOUD_BACKEND:
        reports = [
            snapshot.to_dict()
            for snapshot in get_firestore_db().collection("complaints").stream()
        ]
        return sorted(
            reports,
            key=lambda report: int(report["id"]),
            reverse=order.upper() == "DESC",
        )
    direction = "DESC" if order.upper() == "DESC" else "ASC"
    with get_db() as conn:
        rows = conn.execute(f"SELECT * FROM complaints ORDER BY id {direction}").fetchall()
    return rows_to_reports(rows)


def reports_for_area(area_label):
    if not area_label or area_label == "Location pending":
        return []
    return [
        report
        for report in load_reports("DESC")
        if report.get("area_label") == area_label and report.get("status") != "Resolved"
    ]


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/owner", methods=["GET", "POST"])
def owner_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["owner_logged_in"] = True
            return redirect(request.args.get("next") or url_for("reports"))
        flash("Wrong owner password. Try again.")
    return render_template("owner_login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/reports")
@owner_required
def reports():
    reports = load_reports("ASC")
    data = build_dashboard_data(reports)
    return render_template(
        "reports.html",
        reports=reports,
        x_candidates=load_x_candidates(),
        news_sources_configured=True,
        x_search_location=NEWS_SEARCH_LOCATION,
        x_search_radius_km=NEWS_SEARCH_RADIUS_KM,
        **data,
    )


@app.route("/x-monitor/scan", methods=["POST"])
@owner_required
def scan_x_monitor():
    try:
        result = scan_x_for_civic_issues()
    except NewsMonitorError as exc:
        flash(str(exc))
        return redirect(url_for("reports") + "#x-intelligence")
    if result["added"]:
        flash(
            f"AI News Radar analyzed {result['added']} civic news item"
            f"{'s' if result['added'] != 1 else ''} for owner review."
        )
    elif result["found"]:
        flash("No new candidates were added. Matching news items were already reviewed or could not provide usable images.")
    else:
        flash("No recent English civic news with usable images was found for Pune. Try scanning again later.")
    return redirect(url_for("reports") + "#x-intelligence")


@app.route("/x-monitor/<candidate_id>/approve", methods=["POST"])
@owner_required
def approve_x_candidate(candidate_id):
    candidate = get_x_candidate(candidate_id)
    if not candidate or candidate.get("status") != "Pending":
        flash("That news candidate is no longer waiting for review.")
        return redirect(url_for("reports") + "#x-intelligence")
    ai_result = {
        "category": candidate["category"],
        "severity": int(candidate["severity"]),
        "description": candidate["description"],
        "department": candidate["department"],
        "location_clue": candidate["location_clue"],
        "urgency": candidate["urgency"],
    }
    report_id = create_complaint_from_analysis(
        candidate["media_filename"],
        candidate["media_type"],
        ai_result,
        latitude=candidate.get("latitude"),
        longitude=candidate.get("longitude"),
        citizen_description=candidate.get("post_text", ""),
        source="News Media",
        source_link=candidate["post_url"],
    )
    update_x_candidate(candidate_id, status="Approved", report_id=report_id)
    flash(f"News candidate approved and registered as {make_track_code(report_id)}.")
    return redirect(url_for("reports") + "#reportsTable")


@app.route("/x-monitor/<candidate_id>/reject", methods=["POST"])
@owner_required
def reject_x_candidate(candidate_id):
    candidate = get_x_candidate(candidate_id)
    if candidate and candidate.get("status") == "Pending":
        delete_media(candidate.get("media_filename"))
        update_x_candidate(candidate_id, status="Rejected")
        flash("News candidate rejected. It was not added to civic reports.")
    return redirect(url_for("reports") + "#x-intelligence")


@app.route("/transparency")
def transparency():
    reports = load_reports("ASC")
    data = build_dashboard_data(reports)
    return render_template("transparency.html", **data)


@app.route("/track", methods=["GET", "POST"])
def track():
    report = None
    searched = False
    if request.method == "POST":
        searched = True
        code = request.form.get("track_code", "").strip().upper().replace("#", "")
        numeric_id = None
        if code.startswith("LS-"):
            numeric_id = code.replace("LS-", "").lstrip("0") or "0"
        elif code.isdigit():
            numeric_id = code
        if numeric_id:
            report = get_report_by_id(numeric_id)
        if not report:
            report = get_report_by_track_code(code)
    return render_template("track.html", report=report, searched=searched)


@app.route("/update_status/<int:id>", methods=["POST"])
@owner_required
def update_status(id):
    report = get_report_by_id(id)
    if not report:
        return redirect(url_for("reports"))
    current_status = report["status"]
    if current_status == "Reported":
        new_status = "Under Review"
    elif current_status == "Under Review":
        new_status = "Assigned"
    else:
        new_status = "Resolved"
    fields = {"status": new_status}
    if new_status == "Resolved" and not report.get("resolved_at"):
        fields["resolved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    update_report_fields(id, **fields)
    return redirect(url_for("reports"))


@app.route("/resolve_report/<int:id>", methods=["POST"])
@owner_required
def resolve_report(id):
    proof = request.files.get("proof")
    proof_filename = None
    if proof and proof.filename and allowed_file(proof.filename):
        proof_filename = unique_filename("resolved-" + proof.filename)
        temp_path = save_media(proof, proof_filename)
        if USE_CLOUD_BACKEND and os.path.exists(temp_path):
            os.remove(temp_path)
    fields = {
        "status": "Resolved",
        "resolved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if proof_filename:
        fields["resolved_image_path"] = proof_filename
    update_report_fields(id, **fields)
    return redirect(url_for("reports"))


@app.route("/delete_report/<int:id>", methods=["POST"])
@owner_required
def delete_report(id):
    report = get_report_by_id(id)
    if report:
        delete_media(report.get("image_path"))
        delete_media(report.get("resolved_image_path"))
        delete_report_record(id)
    return redirect(url_for("reports"))


@app.route("/verify/<int:id>", methods=["POST"])
def verify_report(id):
    report = get_report_by_id(id)
    latitude = request.form.get("latitude") or None
    longitude = request.form.get("longitude") or None
    citizen_area = area_from_location(latitude, longitude)
    if report and report.get("area_label") != "Location pending" and citizen_area != "Location pending":
        if citizen_area != report.get("area_label"):
            flash("You can verify only issues from your own ward/area.")
            return redirect(url_for("ward_issues", latitude=latitude, longitude=longitude))
    increment_verification(id)
    return redirect(url_for("thank_you", report_id=id, verified="yes"))


@app.route("/ward_issues")
def ward_issues():
    latitude = request.args.get("latitude") or None
    longitude = request.args.get("longitude") or None
    area_label = area_from_location(latitude, longitude)
    nearby_reports = reports_for_area(area_label)
    return render_template(
        "ward_issues.html",
        reports=nearby_reports,
        area_label=area_label,
        latitude=latitude or "",
        longitude=longitude or "",
    )


@app.route("/upload", methods=["POST"])
def upload():
    init_db()
    if "image" not in request.files:
        flash("Please choose an image or video.")
        return redirect(url_for("home"))
    media = request.files["image"]
    if media.filename == "" or not allowed_file(media.filename):
        flash("Please upload a PNG, JPG, JPEG, WEBP, MP4, MOV, AVI, or MKV file.")
        return redirect(url_for("home"))

    filename = unique_filename(media.filename)
    analysis_path = save_media(media, filename)

    latitude = request.form.get("latitude") or None
    longitude = request.form.get("longitude") or None
    media_type = media_type_for(filename)
    citizen_description = request.form.get("citizen_description", "").strip()

    ai_result = analyze_media(analysis_path, media_type, citizen_description)
    report_id = create_complaint_from_analysis(
        filename,
        media_type,
        ai_result,
        latitude=latitude,
        longitude=longitude,
        citizen_description=citizen_description,
        source="Citizen Upload",
    )
    if USE_CLOUD_BACKEND and os.path.exists(analysis_path):
        os.remove(analysis_path)

    if wants_json_response():
        return jsonify(report_payload(report_id, ai_result))
    return redirect(url_for("thank_you", report_id=report_id))


@app.route("/social_import", methods=["POST"])
def social_import():
    init_db()
    source_link = request.form.get("social_link", "").strip()
    screenshot = request.files.get("social_screenshot")
    citizen_description = request.form.get("social_note", "").strip()
    latitude = request.form.get("social_latitude") or None
    longitude = request.form.get("social_longitude") or None

    filename = None
    analysis_path = None
    media_type = "image"

    if screenshot and screenshot.filename:
        if not allowed_file(screenshot.filename) or extension_for(screenshot.filename) not in IMAGE_EXTENSIONS:
            flash("Please upload a PNG, JPG, JPEG, or WEBP screenshot for social media import.")
            return redirect(url_for("home") + "#social-report")
        filename = unique_filename(screenshot.filename)
        analysis_path = save_media(screenshot, filename)
        media_type = "image"
    elif source_link:
        content, fetched_filename, content_type = fetch_public_social_media(source_link)
        if not content:
            flash("That platform did not expose public media automatically. Please upload a screenshot of the post instead.")
            return redirect(url_for("home") + "#social-report")
        filename = fetched_filename
        analysis_path = save_downloaded_media(content, filename, content_type)
        media_type = media_type_for(filename)
    else:
        flash("Paste a public social media link or upload a screenshot.")
        return redirect(url_for("home") + "#social-report")

    if media_type == "video" and not allowed_file(filename):
        flash("Fetched media type is not supported. Please upload a screenshot of the post instead.")
        return redirect(url_for("home") + "#social-report")

    if source_link:
        citizen_description = f"Source post: {source_link}. {citizen_description}".strip()
    ai_result = analyze_media(analysis_path, media_type, citizen_description, social=True)
    report_id = create_complaint_from_analysis(
        filename,
        media_type,
        ai_result,
        latitude=latitude,
        longitude=longitude,
        citizen_description=citizen_description,
        source="Social Media",
        source_link=source_link or None,
    )
    if USE_CLOUD_BACKEND and analysis_path and os.path.exists(analysis_path):
        os.remove(analysis_path)

    if wants_json_response():
        return jsonify(report_payload(report_id, ai_result))
    return redirect(url_for("thank_you", report_id=report_id))


@app.route("/thanks/<int:report_id>")
def thank_you(report_id):
    report = get_report_by_id(report_id)
    duplicate = get_report_by_id(report["duplicate_of"]) if report and report.get("duplicate_of") else None
    if not report:
        return redirect(url_for("home"))
    return render_template(
        "thank_you.html",
        report=report,
        duplicate=duplicate,
        verified=request.args.get("verified") == "yes",
    )


@app.route("/download_csv")
@owner_required
def download_csv():
    reports = load_reports("ASC")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Report ID", "Track Code", "Category", "Severity", "Priority", "Status", "Department", "Area",
        "Description", "Verifications", "Duplicate Of", "Latitude", "Longitude"
    ])
    for r in reports:
        writer.writerow([
            r["id"], r["track_code"], r["category"], r["severity"], r["priority"], r["status"],
            r["department"], r["area_label"], r["description"], r["verification_count"], r["duplicate_of"],
            r["latitude"], r["longitude"]
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=loksetu_reports.csv"},
    )


@app.route("/demo_mode", methods=["POST"])
@owner_required
def demo_mode():
    samples = [
        ("Pothole", 4, "High", "Reported", "Large pothole near main road causing unsafe two-wheeler movement.", "pathhole.jpg", 20.916492, 74.75853),
        ("Garbage", 5, "Critical", "Under Review", "Overflowing waste bin near residential lane needs urgent sanitation response.", "garbage.jpg", 21.1715, 79.1068),
        ("Pollution", 5, "Critical", "Assigned", "Open waste burning creating smoke and breathing risk for nearby residents.", "air_pollution-cb41c38b.jpg", 20.9149, 74.7601),
        ("Other", 4, "High", "Resolved", "Broken public bench repaired after local verification.", "broken_bench-336b58c0.jpg", 20.9171, 74.7591),
    ]
    for category, severity, priority, status, description, image_path, lat, lon in samples:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        dept = department_for_category(category)
        area = area_from_location(lat, lon)
        duplicate_of = find_duplicate(load_reports("DESC"), category, lat, lon)
        create_report(
            {
                "category": category,
                "severity": severity,
                "priority": priority,
                "status": status,
                "description": description,
                "image_path": image_path,
                "latitude": lat,
                "longitude": lon,
                "created_at": created_at,
                "verification_count": 2 if status == "Resolved" else 0,
                "department": dept,
                "duplicate_of": duplicate_of,
                "area_label": area,
                "media_type": "image",
                "citizen_description": "",
                "resolved_image_path": None,
                "resolved_at": created_at if status == "Resolved" else None,
            }
        )
    return redirect(url_for("reports"))


@app.route("/documentation")
@owner_required
def documentation():
    return render_template("documentation.html")


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
