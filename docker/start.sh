#!/bin/bash
set -e

echo "ZoomScribe starting..."

# Start PulseAudio as system daemon
mkdir -p /tmp/pulse
pulseaudio -D --system=false --exit-idle-time=-1 --disallow-exit --log-level=warn -n --load="module-native-protocol-unix auth-anonymous=1 socket=/tmp/pulse/native" --load="module-null-sink sink_name=zoomscribe_sink sink_properties=device.description=ZoomScribe" --load="module-native-protocol-tcp auth-anonymous=1" 2>/dev/null || true

sleep 2
export PULSE_SERVER=unix:/tmp/pulse/native

echo "PulseAudio attempted"
pactl list sinks short 2>/dev/null || echo "(no sinks yet - continuing)"

mkdir -p /tmp/zoomscribe

echo "Starting API on port ${PORT:-8000}..."
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --log-level info