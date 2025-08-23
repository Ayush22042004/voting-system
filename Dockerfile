FROM python:3.12-slim

# Install system deps (optional: gcc for some packages)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV DATABASE_PATH=/data/voting.db

# Use gunicorn for production
CMD ["gunicorn", "-w", "3", "-k", "gthread", "-b", "0.0.0.0:8080", "app:app"]
