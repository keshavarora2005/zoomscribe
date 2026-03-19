# ZoomScribe 🎙️

> Paste a Zoom link → bot joins as "Notetaker" → speaker-labelled PDF transcript delivered.

---

## Stack

| Layer | Tech | Why |
|---|---|---|
| Bot | Playwright (headless Chromium) | Joins Zoom web client, no SDK needed |
| Audio | PulseAudio null-sink + ffmpeg | Captures meeting audio on Linux |
| API | FastAPI + uvicorn | Job management, polling, PDF download |
| Transcription | AssemblyAI (free tier) | 100 hrs/month free, speaker labels |
| Deploy | Railway free tier | Persistent container, 512MB RAM, Linux |
| Frontend | Vanilla HTML/CSS/JS | Zero dependencies, host anywhere |

---

## Local development

### Prerequisites
- Python 3.11+
- ffmpeg (`brew install ffmpeg` or `apt install ffmpeg`)
- For audio capture on Mac: install [BlackHole 2ch](https://existential.audio/blackhole/) and set it as output

### Setup

```bash
git clone https://github.com/you/zoomscribe
cd zoomscribe

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
# edit .env — add your ASSEMBLYAI_API_KEY
```

### Run API

```bash
uvicorn api.main:app --reload --port 8000
```

### Test the bot (no meeting needed)

```bash
python -c "
import asyncio
from bot.bot import join_and_record
asyncio.run(join_and_record(
    'https://zoom.us/j/YOUR_MEETING_ID',
    '/tmp/test_recording.mp3',
    bot_name='TestBot',
    max_duration_seconds=60
))
"
```

### Test transcription only

```bash
python -c "
from bot.transcriber import audio_to_pdf
audio_to_pdf('path/to/audio.mp3', '/tmp/out.pdf', 'Test Meeting')
"
```

### Open the frontend

Just open `frontend/index.html` in your browser. It points to `http://localhost:8000` by default.

---

## API endpoints

```
POST   /jobs              Submit a Zoom URL
GET    /jobs/{id}         Poll job status
GET    /jobs/{id}/pdf     Download transcript PDF
DELETE /jobs/{id}         Cancel + clean up
GET    /jobs              List all jobs
GET    /health            Health check
```

### Submit a job

```bash
curl -X POST http://localhost:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "zoom_url": "https://zoom.us/j/123456789?pwd=abc",
    "bot_name": "Notetaker",
    "meeting_title": "Product Standup"
  }'
```

Response:
```json
{
  "job_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "status": "queued",
  "message": "Bot is being dispatched to your meeting."
}
```

### Poll status

```bash
curl http://localhost:8000/jobs/f47ac10b-...
```

Statuses: `queued → joining → recording → transcribing → done`

### Download PDF

```bash
curl http://localhost:8000/jobs/f47ac10b-.../pdf -o transcript.pdf
```

---

## Deploy to Railway (free)

1. **Create account** at [railway.app](https://railway.app) — free tier gives $5 credit/month

2. **Create new project** → Deploy from GitHub repo

3. **Set environment variables** in Railway dashboard:
   ```
   ASSEMBLYAI_API_KEY=your_key_here
   WORK_DIR=/tmp/zoomscribe
   PORT=8000
   ```

4. **Railway auto-detects** the `Dockerfile` and builds it

5. **Get your URL** — something like `https://zoomscribe-production.up.railway.app`

6. **Update the frontend** — edit `frontend/index.html`, change:
   ```js
   const API = 'https://zoomscribe-production.up.railway.app';
   ```
   Then host `frontend/index.html` on any static host (Netlify, GitHub Pages, etc. — all free)

---

## Environment variables

| Variable | Description | Required |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | AssemblyAI API key | ✅ |
| `WORK_DIR` | Where audio/PDF files are stored | No (default: /tmp/zoomscribe) |
| `PORT` | Server port | No (default: 8000) |

---

## Known limitations

- **Bot appears in participant list** — named "Notetaker" (or whatever you set). True invisibility is impossible on Zoom.
- **Zoom web UI changes** — Playwright selectors may need updating if Zoom redesigns their web client. Check `bot/bot.py` if joins start failing.
- **Railway free tier sleeps** — after ~15 min of inactivity the container sleeps. First request after sleep takes ~10s to wake. Consider a free uptime monitor (UptimeRobot) pinging `/health` every 5 min.
- **Single worker** — Railway free tier = 1 instance, 1 bot at a time. Enough for personal use.
- **macOS local dev** — audio recording needs BlackHole or similar virtual audio device. The bot will still join and navigate correctly; only the audio capture is skipped.

---

## Project structure

```
zoomscribe/
├── api/
│   └── main.py          # FastAPI server — job API
├── bot/
│   ├── bot.py           # Playwright bot — joins Zoom, records
│   └── transcriber.py   # AssemblyAI upload → transcribe → PDF
├── docker/
│   ├── pulse-default.pa # PulseAudio virtual sink config
│   └── start.sh         # Container entrypoint
├── frontend/
│   └── index.html       # UI — zero dependencies
├── Dockerfile
├── railway.toml
├── requirements.txt
└── README.md
```

---

## Roadmap ideas

- [ ] Email delivery of PDF when done
- [ ] Webhook callback on completion
- [ ] Google Meet support (same Playwright approach, different selectors)
- [ ] Persistent storage with SQLite (survive Railway restarts)
- [ ] Summary + action items via Claude API post-transcription
- [ ] Multi-language support (AssemblyAI already handles this)
