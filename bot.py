"""
ZoomScribe Bot — joins Zoom web client, captures audio via virtual sink, saves MP3.
Requires: playwright, ffmpeg, pulseaudio (Linux) or blackhole (macOS for local dev)
"""

import asyncio
import os
import re
import subprocess
import time
import logging
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# ── helpers ──────────────────────────────────────────────────────────────────

def _extract_meeting_id(url: str) -> tuple[str, str | None]:
    """Return (meeting_id, password) from any Zoom URL format."""
    # https://zoom.us/j/123456789?pwd=abc
    m = re.search(r"/j/(\d+)", url)
    meeting_id = m.group(1) if m else None
    pwd_m = re.search(r"[?&]pwd=([^&]+)", url)
    password = pwd_m.group(1) if pwd_m else None
    if not meeting_id:
        raise ValueError(f"Cannot parse meeting ID from URL: {url}")
    return meeting_id, password


def _start_virtual_sink(sink_name: str = "zoomscribe_sink") -> subprocess.Popen:
    """
    Linux: create a PulseAudio null sink so we can record what Chromium plays.
    The browser is launched with --alsa-output-device pointing here.
    """
    subprocess.run(
        ["pactl", "load-module", "module-null-sink",
         f"sink_name={sink_name}",
         f"sink_properties=device.description=ZoomScribe"],
        check=True
    )
    # Give pulse a moment
    time.sleep(0.5)
    return sink_name


def _start_recording(output_path: str, sink_name: str) -> subprocess.Popen:
    """
    ffmpeg records from the monitor of our virtual sink.
    monitor source = sink_name + '.monitor'
    """
    monitor = f"{sink_name}.monitor"
    cmd = [
        "ffmpeg", "-y",
        "-f", "pulse",
        "-i", monitor,
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        output_path
    ]
    logger.info(f"Starting ffmpeg: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _stop_recording(proc: subprocess.Popen) -> None:
    """Gracefully stop ffmpeg (sends 'q' to stdin equivalent via SIGTERM then wait)."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


# ── main bot ─────────────────────────────────────────────────────────────────

async def join_and_record(
    zoom_url: str,
    output_audio_path: str,
    bot_name: str = "Notetaker",
    max_duration_seconds: int = 7200,   # 2 hours hard cap
    silence_exit_seconds: int = 120,     # leave if alone for 2 min
) -> dict:
    """
    Join a Zoom meeting via the web client, record audio, return metadata dict.
    """
    meeting_id, password = _extract_meeting_id(zoom_url)
    web_url = f"https://zoom.us/wc/{meeting_id}/join"
    if password:
        web_url += f"?pwd={password}"

    logger.info(f"Joining meeting {meeting_id} as '{bot_name}'")

    # ── virtual audio sink (Linux / Railway) ─────────────────────────────────
    sink_name = "zoomscribe_sink"
    is_linux = os.name == "posix" and os.path.exists("/usr/bin/pactl")
    ffmpeg_proc = None

    if is_linux:
        try:
            _start_virtual_sink(sink_name)
            logger.info("Virtual audio sink created")
        except Exception as e:
            logger.warning(f"PulseAudio setup failed (maybe already loaded): {e}")

    async with async_playwright() as pw:
        # Extra args route audio through our virtual sink on Linux
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",   # auto-allow mic/cam
        ]
        if is_linux:
            browser_args += [
                f"--alsa-output-device=pulse:{sink_name}",
                "--disable-features=AudioServiceOutOfProcess",
            ]

        browser = await pw.chromium.launch(
            headless=True,
            args=browser_args,
        )

        context = await browser.new_context(
            permissions=["microphone", "camera"],
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # ── navigate to Zoom web client ───────────────────────────────────────
        logger.info(f"Navigating to {web_url}")
        await page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)

        # ── fill in name ─────────────────────────────────────────────────────
        try:
            await page.wait_for_selector('input[placeholder*="name" i], input[id*="name" i]',
                                         timeout=15_000)
            name_input = page.locator('input[placeholder*="name" i], input[id*="name" i]').first
            await name_input.fill(bot_name)
            logger.info("Name filled")
        except PWTimeout:
            logger.warning("Name input not found — Zoom UI may have changed")

        # ── click Join / Enter meeting ────────────────────────────────────────
        for selector in [
            'button:has-text("Join")',
            'button:has-text("Enter")',
            '[aria-label*="join" i]',
        ]:
            try:
                btn = page.locator(selector).first
                await btn.wait_for(timeout=5_000)
                await btn.click()
                logger.info(f"Clicked join button: {selector}")
                break
            except PWTimeout:
                continue

        # ── wait for meeting to actually load ────────────────────────────────
        await asyncio.sleep(5)

        # ── dismiss audio/video prompts ───────────────────────────────────────
        for sel in [
            'button:has-text("Join Audio")',
            'button:has-text("Join with Computer Audio")',
            '[aria-label*="audio" i]',
            'button:has-text("Got it")',
            'button:has-text("OK")',
        ]:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(timeout=3_000)
                await btn.click()
                logger.info(f"Dismissed prompt: {sel}")
                await asyncio.sleep(0.5)
            except PWTimeout:
                pass

        # ── mute our own mic so we don't pollute the recording ────────────────
        for sel in ['button[aria-label*="Mute" i]', '[title*="Mute" i]']:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(timeout=3_000)
                await btn.click()
                logger.info("Muted bot mic")
                break
            except PWTimeout:
                pass

        # ── start recording via ffmpeg ────────────────────────────────────────
        if is_linux:
            ffmpeg_proc = _start_recording(output_audio_path, sink_name)
            logger.info(f"Recording started → {output_audio_path}")
        else:
            # Local macOS dev: just log; real recording needs BlackHole or similar
            logger.warning("Non-Linux env: audio recording skipped. Use macOS BlackHole for local dev.")

        # ── stay in meeting until it ends or we hit the cap ──────────────────
        start_time = time.time()
        alone_since = None
        participant_count = 0

        while True:
            elapsed = time.time() - start_time

            if elapsed > max_duration_seconds:
                logger.info("Max duration reached — leaving")
                break

            # Check if meeting ended (Zoom shows a "meeting has ended" overlay)
            ended_el = await page.query_selector(
                '[class*="meeting-ended"], [class*="ended"], '
                'div:has-text("meeting has ended")'
            )
            if ended_el:
                logger.info("Meeting ended — leaving")
                break

            # Rough participant count via participant panel badge
            try:
                badge = await page.query_selector('[aria-label*="participant" i] .count, '
                                                  '.participants-header__count')
                if badge:
                    count_text = await badge.inner_text()
                    participant_count = int(re.search(r"\d+", count_text).group())
                    if participant_count <= 1:
                        if alone_since is None:
                            alone_since = time.time()
                        elif time.time() - alone_since > silence_exit_seconds:
                            logger.info("Alone in meeting too long — leaving")
                            break
                    else:
                        alone_since = None
            except Exception:
                pass

            await asyncio.sleep(10)

        # ── leave the meeting ────────────────────────────────────────────────
        for sel in [
            'button[aria-label*="Leave" i]',
            'button:has-text("Leave")',
            '[title*="Leave" i]',
        ]:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(timeout=3_000)
                await btn.click()
                await asyncio.sleep(1)
                # Confirm "Leave Meeting" if a dialog appears
                confirm = page.locator('button:has-text("Leave Meeting")').first
                await confirm.wait_for(timeout=3_000)
                await confirm.click()
                logger.info("Left meeting gracefully")
                break
            except PWTimeout:
                pass

        await browser.close()

    # ── stop recording ────────────────────────────────────────────────────────
    if ffmpeg_proc:
        _stop_recording(ffmpeg_proc)
        logger.info("Recording stopped")

    duration = int(time.time() - start_time)
    return {
        "meeting_id": meeting_id,
        "duration_seconds": duration,
        "audio_path": output_audio_path if is_linux else None,
        "bot_name": bot_name,
    }
