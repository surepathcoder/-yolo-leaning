# VisionGuard backend + vision engine (CPU build).
# For an NVIDIA GPU image see deployment/gpu/backend.gpu.Dockerfile.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend:/app

# libgl1 / libglib2.0-0 are required by OpenCV; ffmpeg decodes RTSP streams.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the CPU torch wheels explicitly so the image does not pull ~2GB of
# CUDA libraries that a CPU deployment cannot use.
COPY backend/requirements.txt /app/backend/requirements.txt
COPY vision/requirements.txt /app/vision/requirements.txt
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        torch==2.6.0 torchvision==0.21.0 \
 && pip install -r /app/backend/requirements.txt \
 && pip install -r /app/vision/requirements.txt

COPY backend /app/backend
COPY vision /app/vision
COPY alembic.ini /app/alembic.ini

RUN useradd --create-home --uid 10001 visionguard \
 && mkdir -p /app/data/media /app/data/evidence \
 && chown -R visionguard:visionguard /app
USER visionguard

WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
