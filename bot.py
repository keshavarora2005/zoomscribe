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
    m = re.search(r"/j/(\d+)", url)
    meeting_id = m.group(1) if m else None
    pwd_m = re.search(r"[?&]pwd=([^&]+)", url)
    password = pwd_m.group(1) if pwd_m else None
    if not meeting_id:
        raise ValueError(f"Cannot parse meeting ID from URL: {url}")
    return meeting_id, password


def _start_virtual_sink(sink_name: str = "zoomscribe_sink") -> str:
    """Linux: create a PulseAudio null sink."""
    subprocess.run(
        ["pactl", "load-module", "module-null-sink",
         f"sink_name={sink_name}",
         f"sink_properties=device.description=ZoomScribe"],
        check=True
    )
    time.sleep(0.5)
    return sink_name


def _start_recording(output_path: str, sink_name: str) -> subprocess.Popen:
    """ffmpeg records from the monitor of our virtual sink."""
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
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


async def _screenshot(page, name: str):
    """Save a debug screenshot — never crashes the bot."""
    try:
        os.makedirs("/tmp/zoomscribe", exist_ok=True)
        await page.screenshot(path=f"/tmp/zoomscribe/{name}.png")
        logger.info(f"Screenshot: {name}.png | URL: {page.url} | Title: {await page.title()}")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")


# ── main bot ─────────────────────────────────────────────────────────────────

async def join_and_record(
    zoom_url: str,
    output_audio_path: str,
    bot_name: str = "Notetaker",
    max_duration_seconds: int = 7200,
    silence_exit_seconds: int = 120,
) -> dict:
    meeting_id, password = _extract_meeting_id(zoom_url)
    web_url = f"https://zoom.us/wc/{meeting_id}/join"
    if password:
        web_url += f"?pwd={password}"

    logger.info(f"Joining meeting {meeting_id} as '{bot_name}'")

    # ── virtual audio sink ────────────────────────────────────────────────────
    sink_name = "zoomscribe_sink"
    is_linux = os.name == "posix" and os.path.exists("/usr/bin/pactl")
    ffmpeg_proc = None

    if is_linux:
        try:
            _start_virtual_sink(sink_name)
            logger.info("Virtual audio sink created")
        except Exception as e:
            logger.warning(f"PulseAudio setup failed: {e}")

    async with async_playwright() as pw:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--disable-blink-features=AutomationControlled",  # hide headless
        ]
        if is_linux:
            browser_args += [
                "--alsa-output-device=pulse:zoomscribe_sink",
                "--disable-features=AudioServiceOutOfProcess",
            ]

        browser = await pw.chromium.launch(
            headless=True,
            args=browser_args,
        )

        context = await browser.new_context(
            permissions=["microphone", "camera"],
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()

        # ── navigate ──────────────────────────────────────────────────────────
        logger.info(f"Navigating to {web_url}")
        try:
            await page.goto(web_url, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            logger.warning("Page load timeout — continuing anyway")
        await asyncio.sleep(4)
        await _screenshot(page, "01_loaded")

        # ── fill name ─────────────────────────────────────────────────────────
        name_selectors = [
            'input[placeholder*="name" i]',
            'input[id*="name" i]',
            'input[aria-label*="name" i]',
            '#inputname',
            '#your-name',
            'input[type="text"]',
        ]
        name_filled = False
        for sel in name_selectors:
            try:
                el = page.locator(sel).first
                await el.wait_for(state="visible", timeout=5_000)
                await el.click()
                await el.fill(bot_name)
                logger.info(f"Name filled via: {sel}")
                name_filled = True
                break
            except PWTimeout:
                continue

        if not name_filled:
            logger.warning("Name input not found — attempting to join without name")

        await asyncio.sleep(1)
        await _screenshot(page, "02_name_filled")

        # ── click join ────────────────────────────────────────────────────────
        join_selectors = [
            'button:has-text("Join")',
            'button:has-text("Join Meeting")',
            'button:has-text("Join Now")',
            'button:has-text("Enter")',
            '[aria-label*="join" i]',
            'button[class*="join" i]',
        ]
        for sel in join_selectors:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                logger.info(f"Clicked join: {sel}")
                break
            except PWTimeout:
                continue

        # ── wait for meeting UI to load ───────────────────────────────────────
        await asyncio.sleep(10)
        await _screenshot(page, "03_after_join")
        logger.info(f"After join URL: {page.url}")

        # ── dismiss all popups ────────────────────────────────────────────────
        popup_selectors = [
            'button:has-text("Join with Computer Audio")',
            'button:has-text("Join Audio")',
            'button:has-text("Computer Audio")',
            'button:has-text("Got it")',
            'button:has-text("OK")',
            'button:has-text("Allow")',
            'button:has-text("Continue")',
            'button:has-text("Dismiss")',
            '[aria-label*="close" i]',
        ]
        for sel in popup_selectors:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=2_000)
                await btn.click()
                logger.info(f"Dismissed: {sel}")
                await asyncio.sleep(0.5)
            except PWTimeout:
                pass

        # ── mute bot mic ──────────────────────────────────────────────────────
        for sel in [
            'button[aria-label*="Mute" i]',
            '[title*="Mute" i]',
            'button[aria-label*="mute my microphone" i]',
        ]:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=3_000)
                await btn.click()
                logger.info("Muted bot mic")
                break
            except PWTimeout:
                pass

        await _screenshot(page, "04_in_meeting")

        # ── start recording ───────────────────────────────────────────────────
        if is_linux:
            ffmpeg_proc = _start_recording(output_audio_path, sink_name)
            logger.info(f"Recording started → {output_audio_path}")
        else:
            logger.warning("Non-Linux: audio recording skipped")

        # ── stay in meeting ───────────────────────────────────────────────────
        start_time = time.time()
        alone_since = None

        while True:
            elapsed = time.time() - start_time

            if elapsed > max_duration_seconds:
                logger.info("Max duration reached — leaving")
                break

            # Check meeting ended
            try:
                ended = await page.query_selector(
                    '[class*="meeting-ended"], [class*="ended"], '
                    'div:has-text("This meeting has been ended")'
                )
                if ended:
                    logger.info("Meeting ended — leaving")
                    break
            except Exception:
                pass

            # Check if we got kicked to a non-meeting page
            current_url = page.url
            if "zoom.us/wc" not in current_url and "zoom.us/j" not in current_url:
                logger.info(f"Redirected away from meeting ({current_url}) — leaving")
                break

            # Participant count
            try:
                badge = await page.query_selector(
                    '[aria-label*="participant" i] .count, '
                    '.participants-header__count, '
                    '[class*="participants-count"]'
                )
                if badge:
                    count_text = await badge.inner_text()
                    count_match = re.search(r"\d+", count_text)
                    if count_match:
                        count = int(count_match.group())
                        if count <= 1:
                            if alone_since is None:
                                alone_since = time.time()
                            elif time.time() - alone_since > silence_exit_seconds:
                                logger.info("Alone too long — leaving")
                                break
                        else:
                            alone_since = None
            except Exception:
                pass

            await asyncio.sleep(10)

        # ── leave meeting ─────────────────────────────────────────────────────
        for sel in ['button[aria-label*="Leave" i]', 'button:has-text("Leave")']:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=3_000)
                await btn.click()
                await asyncio.sleep(1)
                confirm = page.locator('button:has-text("Leave Meeting")').first
                await confirm.wait_for(state="visible", timeout=3_000)
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