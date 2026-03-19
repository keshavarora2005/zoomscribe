@"
#!/bin/bash
set -e

echo "ZoomScribe starting..."

mkdir -p /tmp/pulse
pulseaudio --start --exit-idle-time=-1 --disallow-exit --daemon --log-level=warn

echo "PulseAudio started"
sleep 1

mkdir -p /tmp/zoomscribe

echo "Starting API on port ${PORT:-8000}..."
exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level info
"@ | Out-File -FilePath docker/start.sh -Encoding utf8 -NoNewline