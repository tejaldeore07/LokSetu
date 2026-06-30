FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py database.py complaints.db ./
COPY templates ./templates
COPY static ./static

RUN mkdir -p /app/uploads

CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 8 --timeout 300 app:app
