FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# ffmpeg lets yt-dlp fix up odd streams; libgl1 and libglib are needed by the
# opencv-python wheel (scenedetect depends on it, so the headless build isn't an option).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN mkdir -p /data

# One process so the in-memory job list is shared; threads handle requests.
CMD ["sh", "-c", "exec gunicorn app:app --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:${PORT:-8000}"]
