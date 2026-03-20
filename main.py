"""
ZoomScribe API — FastAPI server
POST /jobs          → submit a Zoom URL, get back a job_id
GET  /jobs/{id}     → poll status
GET  /jobs/{id}/pdf → download the finished transcript PDF
DELETE /jobs/{id}   → cancel / clean up
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path

# ── Windows fix ───────────────────────────────────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("zoomscribe.api")

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ZoomScribe",
    description="Meeting recorder & transcriber",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/zoomscribe"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
JOBS_FILE = WORK_DIR / "jobs.json"


# ── persistent job store ──────────────────────────────────────────────────────
def _load_jobs() -> dict:
    try:
        if JOBS_FILE.exists():
            data = json.loads(JOBS_FILE.read_text())
            # Mark any in-progress jobs as error (they were interrupted by restart)
            for job in data.values():
                if job["status"] in ("queued", "joining", "recording", "transcribing"):
                    job["status"] = "error"
                    job["error"] = "Server restarted mid-job — please resubmit"
                    job["updated_at"] = datetime.utcnow().isoformat() + "Z"
            return data
    except Exception as e:
        logger.warning(f"Could not load jobs file: {e}")
    return {}


def _save_jobs():
    try:
        JOBS_FILE.write_text(json.dumps(JOBS, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Could not save jobs: {e}")


JOBS: dict[str, dict] = _load_jobs()
logger.info(f"Loaded {len(JOBS)} jobs from disk")


# ── job model ─────────────────────────────────────────────────────────────────
class JobStatus(str, Enum):
    queued       = "queued"
    joining      = "joining"
    recording    = "recording"
    transcribing = "transcribing"
    done         = "done"
    error        = "error"


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _update(job_id: str, **kwargs):
    JOBS[job_id].update(kwargs, updated_at=_now())
    _save_jobs()
    logger.info(f"[{job_id[:8]}] {kwargs}")


# ── request / response schemas ────────────────────────────────────────────────
class CreateJobRequest(BaseModel):
    zoom_url: str
    bot_name: str = "Notetaker"
    meeting_title: str = "Meeting Transcript"


class CreateJobResponse(BaseModel):
    job_id: str
    status: str
    message: str


# ── background worker ─────────────────────────────────────────────────────────
async def _run_job(job_id: str):
    job = JOBS[job_id]
    audio_path = str(WORK_DIR / f"{job_id}.mp3")
    pdf_path   = str(WORK_DIR / f"{job_id}.pdf")

    try:
        # 1 — join & record
        _update(job_id, status=JobStatus.joining)
        from bot import join_and_record
        meta = await join_and_record(
            zoom_url=job["zoom_url"],
            output_audio_path=audio_path,
            bot_name=job["bot_name"],
        )
        _update(job_id, status=JobStatus.recording,
                duration_seconds=meta["duration_seconds"])

        # 2 — transcribe + PDF
        _update(job_id, status=JobStatus.transcribing)
        from transcriber import audio_to_pdf
        await asyncio.to_thread(
            audio_to_pdf,
            audio_path,
            pdf_path,
            job.get("meeting_title", "Meeting Transcript"),
        )

        _update(job_id, status=JobStatus.done, pdf_ready=True)

        # Clean up raw audio
        try:
            Path(audio_path).unlink(missing_ok=True)
        except Exception:
            pass

    except Exception:
        import traceback
        err = traceback.format_exc()
        logger.error(f"[{job_id[:8]}] Job failed:\n{err}")
        _update(job_id, status=JobStatus.error, error=err[-400:])


# ── endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "jobs": len(JOBS)}


@app.post("/jobs", response_model=CreateJobResponse, status_code=202)
async def create_job(req: CreateJobRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "id":            job_id,
        "zoom_url":      req.zoom_url,
        "bot_name":      req.bot_name,
        "meeting_title": req.meeting_title,
        "status":        JobStatus.queued,
        "created_at":    _now(),
        "updated_at":    _now(),
        "error":         None,
        "duration_seconds": None,
        "pdf_ready":     False,
    }
    _save_jobs()
    # Worker process picks this up automatically via jobs.json polling
    logger.info(f"Job queued: {job_id} for {req.zoom_url}")
    return CreateJobResponse(
        job_id=job_id,
        status="queued",
        message="Bot is being dispatched to your meeting.",
    )


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/jobs/{job_id}/pdf")
def download_pdf(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != JobStatus.done:
        raise HTTPException(409, f"PDF not ready — status: {job['status']}")
    pdf_path = WORK_DIR / f"{job_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(500, "PDF file missing on server")
    slug = job.get("meeting_title", "transcript").replace(" ", "_").lower()
    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=f"{slug}_{job_id[:8]}.pdf",
    )


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    job = JOBS.pop(job_id, None)
    if not job:
        raise HTTPException(404, "Job not found")
    for ext in [".mp3", ".pdf"]:
        Path(str(WORK_DIR / f"{job_id}{ext}")).unlink(missing_ok=True)
    _save_jobs()


@app.get("/jobs")
def list_jobs():
    return list(JOBS.values())