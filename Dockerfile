# ZoomScribe — Railway-optimised Docker image
# Playwright + Chromium + PulseAudio + ffmpeg + Python API

FROM python:3.11-slim

# ── system deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ffmpeg for audio capture
    ffmpeg \
    # PulseAudio virtual sink
    pulseaudio \
    pulseaudio-utils \
    # Chromium runtime deps (playwright installs its own chromium but needs these)
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libcairo2 \
    # Fonts for Zoom web UI
    fonts-liberation fonts-noto-color-emoji \
    # Utilities
    curl wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps ───────────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's bundled Chromium
RUN playwright install chromium

# ── app code ──────────────────────────────────────────────────────────────────
COPY . .

# ── PulseAudio config for headless ───────────────────────────────────────────
RUN mkdir -p /root/.config/pulse && \
    echo "default-server = unix:/tmp/pulse/native" > /root/.config/pulse/client.conf

COPY docker/pulse-default.pa /etc/pulse/default.pa

# ── entrypoint ────────────────────────────────────────────────────────────────
COPY docker/start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8000
CMD ["/start.sh"]
