FROM python:3.11-slim

# Install ffmpeg/ffprobe for smart subtitle sync
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=7123
ENV PYTHONUNBUFFERED=1

EXPOSE 7123

CMD ["python", "skills/torrentx/stremio_addon.py"]
