# Production Dockerfile for CareHarmony Pipecat + Nova 2 Sonic Voice Gateway
FROM --platform=linux/amd64 python:3.12-slim

# Install system audio and build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    python3-dev \
    ffmpeg \
    libsndfile1 \
    libsndfile1-dev \
    pkg-config \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy backend source code
COPY . .

# Set Python path to ensure module discovery
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000 5060/udp 10000-10050/udp

# Default entrypoint for Pipecat Voice Gateway
CMD ["python", "-m", "gateway.pipeline"]
