"""
YouTube -> WAV converter backend.
Stack: Flask + yt-dlp + ffmpeg.

Endpoints:
  GET  /api/health                 -> {"ok": true}
  POST /api/convert  {"url": ...}  -> {"job_id": ...}            (202)
  GET  /api/status/<job_id>        -> {"status","progress","title","download_url"}
  GET  /api/download/<token>       -> the .wav file as attachment

Run:
  pip install -r requirements.txt
  python app.py            # dev
  gunicorn -w 2 -b 0.0.0.0:8000 app:app   # production
"""

import os
import re
import time
import uuid
import shutil
import threading
from pathlib import Path

from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS

# ---------------------------------------------------------------- config

BASE_DIR   = Path(os.environ.get("YTWAV_DIR", "/tmp/ytwav"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_DURATION_SEC = int(os.environ.get("YTWAV_MAX_DURATION", "7200"))  # 2 hours, like yttowav.net
FILE_TTL_SEC     = int(os.environ.get("YTWAV_FILE_TTL", "600"))      # delete files 10 min after done
CLEANUP_EVERY    = 120

ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "YTWAV_CORS_ORIGINS", "*"
    ).split(",") if o.strip()
]

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?"
    r"(youtube\.com/(watch\?.*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[\w\-]{11}"
)

# ---------------------------------------------------------------- app

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

jobs = {}          # job_id -> dict(status, progress, title, token, error, finished_at)
jobs_lock = threading.Lock()


def new_token():
    return uuid.uuid4().hex


def set_job(job_id, **kw):
    with jobs_lock:
        jobs[job_id].update(kw)


def convert_worker(job_id: str, url: str):
    """Runs in a background thread: download best audio, transcode to WAV."""
    import yt_dlp  # imported lazily so /api/health works without it

    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    wav_path = job_dir / "audio.wav"

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            # download = first 80% of progress bar, transcode = last 20%
            pct = int(done / total * 80) if total else 10
            set_job(job_id, progress=min(pct, 80))
        elif d.get("status") == "finished":
            set_job(job_id, progress=85)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        # WAV: 16-bit PCM, 44.1 kHz stereo (CD quality, like yttowav.net)
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
            "preferredquality": "0",
        }],
        "postprocessor_args": ["-ar", "44100", "-ac", "2", "-sample_fmt", "s16"],
        "keepvideo": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get("duration") or 0
            if duration > MAX_DURATION_SEC:
                raise ValueError(
                    f"Video is too long ({duration // 60} min). "
                    f"Maximum supported length is {MAX_DURATION_SEC // 60} minutes."
                )
            title = (info.get("title") or "audio")[:80]
            set_job(job_id, status="working", title=title, progress=5)

            ydl.download([url])

        # yt-dlp names the postprocessed file from the outtmpl stem
        produced = job_dir / "source.wav"
        if not produced.exists():
            # fallback: whatever .wav got created
            wavs = list(job_dir.glob("*.wav"))
            if not wavs:
                raise RuntimeError("Audio conversion produced no file.")
            produced = wavs[0]
        produced.rename(wav_path)

        token = new_token()
        set_job(job_id, status="done", progress=100, token=token,
                finished_at=time.time(),
                download_url=f"/api/download/{token}")
    except Exception as e:  # noqa: BLE001 - surface any failure to the client
        msg = str(e)
        if "Private video" in msg or "login required" in msg.lower():
            msg = "This video is private or age-restricted and cannot be converted."
        elif "Video unavailable" in msg:
            msg = "This video is unavailable or was removed."
        set_job(job_id, status="error", error=msg[:300],
                finished_at=time.time())


def cleanup_worker():
    """Deletes finished job files after FILE_TTL_SEC, forever."""
    while True:
        time.sleep(CLEANUP_EVERY)
        now = time.time()
        with jobs_lock:
            stale = [jid for jid, j in jobs.items()
                     if j.get("finished_at") and now - j["finished_at"] > FILE_TTL_SEC]
            for jid in stale:
                shutil.rmtree(BASE_DIR / jid, ignore_errors=True)
                # drop the token mapping too
                jobs.pop(jid, None)


# ---------------------------------------------------------------- routes

@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.post("/api/convert")
def convert():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not YOUTUBE_RE.match(url):
        return jsonify(error="Please paste a valid YouTube video URL."), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "progress": 0, "title": "",
                        "token": None, "error": "", "finished_at": None,
                        "url": url}
    threading.Thread(target=convert_worker, args=(job_id, url), daemon=True).start()
    return jsonify(job_id=job_id), 202


@app.get("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown or expired job."), 404
    out = {"status": job["status"], "progress": job["progress"],
           "title": job["title"], "error": job["error"]}
    if job["status"] == "done":
        out["download_url"] = job["download_url"]
    return jsonify(out)


@app.get("/api/download/<token>")
def download(token):
    with jobs_lock:
        match = [(jid, j) for jid, j in jobs.items()
                 if j.get("token") == token and j.get("status") == "done"]
    if not match:
        abort(404)
    job_id, job = match[0]
    wav_path = BASE_DIR / job_id / "audio.wav"
    if not wav_path.exists():
        abort(404)
    safe_title = re.sub(r"[^\w\- ]", "", job["title"])[:60] or "audio"
    return send_file(
        wav_path,
        mimetype="audio/wav",
        as_attachment=True,
        download_name=f"{safe_title}.wav",
    )


# ---------------------------------------------------------------- main

threading.Thread(target=cleanup_worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
