FROM python:3.12-slim

# ffmpeg is required for the WAV conversion
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Render sets $PORT automatically
CMD ["sh", "-c", "gunicorn -w 2 --threads 4 -b 0.0.0.0:$PORT app:app"]
