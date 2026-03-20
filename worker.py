#!/usr/bin/env python3
"""
ZoomScribe Worker — runs as a separate process from the API.
Picks up queued jobs from jobs.json, processes them, updates status.
Run alongside the API: python worker.py &
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("zoomscribe.worker")

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/zoomscribe"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
JOBS_FILE = WORK_DIR / "jobs.json"


def load_jobs() -> dict:
    try:
        if JOBS_FILE.exists():
            return json.loads(JOBS_FILE.read_text())
    except Exception:
        pass
    return {}


def save_jobs(jobs: dict):
    try:
        JOBS_FILE.write_text(json.dumps(jobs, indent=2, default=str))
    except Exception as e:
        logger.warning(f"Could not save jobs: {e}")


def update_job(jobs: dict, job_id: str, **kwargs):
    from datetime import datetime
    jobs[job_id].update(kwargs, updated_at=datetime.utcnow().isoformat() + "Z")
    save_jobs(jobs)
    logger.info(f"[{job_id[:8]}] {kwargs}")


async def process_job(job_id: str):
    jobs = load_jobs()
    job = jobs.get(job_id)
    if not job:
        logger.error(f"Job {job_id} not found")
        return

    audio_path = str(WORK_DIR / f"{job_id}.mp3")
    pdf_path   = str(WORK_DIR / f"{job_id}.pdf")

    try:
        update_job(jobs, job_id, status="joining")

        from bot import join_and_record
        meta = await join_and_record(
            zoom_url=job["zoom_url"],
            output_audio_path=audio_path,
            bot_name=job["bot_name"],
        )

        jobs = load_jobs()  # reload in case API updated
        update_job(jobs, job_id, status="recording",
                   duration_seconds=meta["duration_seconds"])

        update_job(jobs, job_id, status="transcribing")

        from transcriber import audio_to_pdf
        await asyncio.to_thread(
            audio_to_pdf,
            audio_path,
            pdf_path,
            job.get("meeting_title", "Meeting Transcript"),
        )

        jobs = load_jobs()
        update_job(jobs, job_id, status="done", pdf_ready=True)

        try:
            Path(audio_path).unlink(missing_ok=True)
        except Exception:
            pass

        logger.info(f"[{job_id[:8]}] Done! PDF at {pdf_path}")

    except Exception:
        import traceback
        err = traceback.format_exc()
        logger.error(f"[{job_id[:8]}] Failed:\n{err}")
        jobs = load_jobs()
        update_job(jobs, job_id, status="error", error=err[-400:])


async def main():
    logger.info("Worker started — polling for jobs...")
    processing = set()

    while True:
        try:
            jobs = load_jobs()
            for job_id, job in jobs.items():
                if job["status"] == "queued" and job_id not in processing:
                    logger.info(f"Picked up job {job_id[:8]}")
                    processing.add(job_id)
                    asyncio.create_task(process_job(job_id))

            # Clean up finished jobs from processing set
            done = {jid for jid in processing
                    if jobs.get(jid, {}).get("status") in ("done", "error")}
            processing -= done

        except Exception as e:
            logger.warning(f"Worker loop error: {e}")

        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())