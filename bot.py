"""
ZoomScribe Bot — joins Zoom web client, captures audio via virtual sink, saves MP3.
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


def _extract_meeting_id(url: str) -> tuple[str, str | None]:
    m = re.search(r"/j/(\d+)", url)
    meeting_id = m.group(1) if m else None
    pwd_m = re.search(r"[?&]pwd=([^&]+)", url)
    password = pwd_m.group(1) if pwd_m else None
    if not meeting_id:
        raise ValueError(f"Cannot parse meeting ID from URL: {url}")
    return meeting_id, password


def _start_virtual_sink(sink_name: str = "zoomscribe_sink") -> str:
    subprocess.run(
        ["pactl", "load-module", "module-null-sink",
         f"sink_name={sink_name}",
         f"sink_properties=device.description=ZoomScribe"],
        check=True
    )
    time.sleep(0.5)
    return sink_name


def _start_recording(output_path: str, sink_name: str) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-y",
        "-f", "pulse",
        "-i", f"{sink_name}.monitor",
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
    try:
        os.makedirs("/tmp/zoomscribe", exist_ok=True)
        await page.screenshot(path=f"/tmp/zoomscribe/{name}.png", timeout=10_000)
        logger.info(f"Screenshot: {name} | URL: {page.url[:80]}")
    except Exception as e:
        logger.warning(f"Screenshot failed: {e}")


async def _wait_for_meeting(page, timeout_seconds: int = 120) -> bool:
    logger.info("Waiting to enter meeting...")
    start = time.time()

    while time.time() - start < timeout_seconds:
        try:
            # Sign 1: Mute button visible (only inside meeting)
            mute_btn = await page.query_selector(
                'button[aria-label*="Mute" i], '
                'button[aria-label*="Unmute" i]'
            )
            if mute_btn:
                logger.info("Mute button found — inside meeting!")
                return True

            # Sign 2: Leave button visible
            leave_btn = await page.query_selector('button[aria-label*="Leave" i]')
            if leave_btn:
                logger.info("Leave button found — inside meeting!")
                return True

            # Sign 3: Meeting toolbar visible
            toolbar = await page.query_selector(
                '[class*="toolbar"], [class*="footer"], [class*="meeting-app"]'
            )
            if toolbar:
                logger.info("Toolbar found — inside meeting!")
                return True

            # Keep dismissing popups
            for sel in [
                'button:has-text("Join with Computer Audio")',
                'button:has-text("Join Audio")',
                'button:has-text("Got it")',
                'button:has-text("OK")',
                'button:has-text("Allow")',
            ]:
                try:
                    btn = page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=300)
                    await btn.click()
                    logger.info(f"Dismissed: {sel}")
                except PWTimeout:
                    pass

            logger.info(f"Not in meeting yet... URL: {page.url[:60]}")

        except Exception as e:
            logger.warning(f"Wait loop error: {e}")

        await asyncio.sleep(2)

    logger.warning("Timed out waiting to enter meeting")
    return False


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
            "--disable-blink-features=AutomationControlled",
        ]
        if is_linux:
            browser_args += [
                "--alsa-output-device=pulse:zoomscribe_sink",
                "--disable-features=AudioServiceOutOfProcess",
            ]

        browser = await pw.chromium.launch(headless=True, args=browser_args)
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

        # navigate
        logger.info(f"Navigating to {web_url}")
        try:
            await page.goto(web_url, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            logger.warning("Page load timeout — continuing")
        await asyncio.sleep(4)
        await _screenshot(page, "01_loaded")

        # fill name
        for sel in [
            'input[id*="name" i]',
            'input[placeholder*="name" i]',
            'input[aria-label*="name" i]',
            '#inputname',
            'input[type="text"]',
        ]:
            try:
                el = page.locator(sel).first
                await el.wait_for(state="visible", timeout=8_000)
                await el.click()
                await el.fill(bot_name)
                logger.info(f"Name filled via: {sel}")
                break
            except PWTimeout:
                continue

        await asyncio.sleep(1)

        # click join
        for sel in [
            'button:has-text("Join")',
            'button:has-text("Join Meeting")',
            'button:has-text("Join Now")',
            'button:has-text("Enter")',
        ]:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=5_000)
                await btn.click()
                logger.info(f"Clicked join: {sel}")
                break
            except PWTimeout:
                continue

        await asyncio.sleep(3)

        # wait until inside meeting
        joined = await _wait_for_meeting(page, timeout_seconds=120)

        if not joined:
            logger.error("Failed to enter meeting")
            await browser.close()
            return {"meeting_id": meeting_id, "duration_seconds": 0,
                    "audio_path": None, "bot_name": bot_name}

        await _screenshot(page, "03_inside_meeting")
        logger.info("Inside the meeting!")

        # mute mic
        for sel in ['button[aria-label*="Mute" i]', 'button[aria-label*="mute my microphone" i]']:
            try:
                btn = page.locator(sel).first
                await btn.wait_for(state="visible", timeout=3_000)
                await btn.click()
                logger.info("Muted bot mic")
                break
            except PWTimeout:
                pass

        # start recording
        if is_linux:
            ffmpeg_proc = _start_recording(output_audio_path, sink_name)
            logger.info(f"Recording started → {output_audio_path}")
        else:
            logger.warning("Non-Linux: audio recording skipped")

        # stay in meeting
        start_time = time.time()

        while True:
            if time.time() - start_time > max_duration_seconds:
                logger.info("Max duration reached")
                break

            try:
                current_url = page.url

                # Redirected away = meeting ended
                if "/wc/" not in current_url:
                    logger.info(f"Redirected away — meeting ended")
                    break

                # Check page text
                content = await page.content()
                if any(p.lower() in content.lower() for p in [
                    "meeting has ended", "been ended by the host",
                    "this meeting has ended"
                ]):
                    logger.info("Meeting ended text found")
                    break

                # Back on pre-join = kicked
                if "join?" in current_url:
                    logger.info("Back on pre-join — kicked from meeting")
                    break

            except Exception as e:
                logger.warning(f"Loop error: {e}")

            await asyncio.sleep(8)

        # leave
        try:
            btn = page.locator('button[aria-label*="Leave" i]').first
            await btn.wait_for(state="visible", timeout=3_000)
            await btn.click()
            await asyncio.sleep(1)
            confirm = page.locator('button:has-text("Leave Meeting")').first
            await confirm.wait_for(state="visible", timeout=3_000)
            await confirm.click()
        except Exception:
            pass

        await browser.close()

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