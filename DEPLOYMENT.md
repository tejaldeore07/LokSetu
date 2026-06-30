# LOKSETU Google Cloud Deployment

LOKSETU deploys to Google Cloud Run in `asia-south1`.

## Cloud services

- Cloud Run: public Flask application
- Firestore: persistent complaints and workflow data
- Cloud Storage: persistent uploaded evidence
- Secret Manager: Gemini API key, optional news API keys, Flask session secret, and owner password
- Cloud Build and Artifact Registry: source build and container storage

## Required runtime settings

- `USE_CLOUD_BACKEND=true`
- `GCS_BUCKET=<project-id>-loksetu-media`
- `SEED_CLOUD_DATA=true`
- `GEMINI_API_KEY` from Secret Manager
- `NEWSAPI_KEY` from Secret Manager, if you use NewsAPI
- `GNEWS_API_KEY` from Secret Manager, if you use GNews
- `NEWSDATA_API_KEY` from Secret Manager, if you use NewsData.io
- `FLASK_SECRET_KEY` from Secret Manager
- `LOKSETU_ADMIN_PASSWORD` from Secret Manager

The packaged SQLite database is used only once to seed an empty Firestore
collection. New cloud reports are stored in Firestore and uploaded evidence is
stored in Cloud Storage.

## Safety

`.env`, `API KEY.docx`, the virtual environment, local uploads, test images,
and development scripts are excluded from the source upload.
