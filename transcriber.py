"""
ZoomScribe Transcriber — uploads audio to AssemblyAI, polls for result,
generates speaker-labelled PDF.  Reuses + improves the user's original code.
"""

import os
import time
import requests
import logging
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor

logger = logging.getLogger(__name__)

BASE_URL = "https://api.assemblyai.com/v2"

# One colour per speaker label (A-Z), cycles if > 26 speakers
SPEAKER_COLORS = [
    "#4F46E5", "#0891B2", "#059669", "#D97706", "#DC2626",
    "#7C3AED", "#DB2777", "#0284C7", "#16A34A", "#CA8A04",
]


def _headers() -> dict:
    key = os.environ.get("ASSEMBLYAI_API_KEY") or os.environ.get("API_KEY")
    if not key:
        raise EnvironmentError("ASSEMBLYAI_API_KEY not set")
    return {"authorization": key}


# ── upload ────────────────────────────────────────────────────────────────────

def upload_audio(file_path: str) -> str:
    logger.info(f"Uploading {file_path} to AssemblyAI…")
    with open(file_path, "rb") as f:
        resp = requests.post(BASE_URL + "/upload",
                             headers=_headers(),
                             files={"file": f},
                             timeout=300)
    resp.raise_for_status()
    url = resp.json()["upload_url"]
    logger.info(f"Upload complete: {url}")
    return url


# ── transcribe ────────────────────────────────────────────────────────────────

def transcribe(audio_url: str, language_detection: bool = True) -> dict:
    payload = {
        "audio_url": audio_url,
        "language_detection": language_detection,
        "punctuate": True,
        "format_text": True,
        "speaker_labels": True,
        "auto_highlights": True,       # key phrases
        "sentiment_analysis": False,   # keep free-tier friendly
    }
    headers = {**_headers(), "content-type": "application/json"}
    logger.info("Submitting transcription job…")
    resp = requests.post(BASE_URL + "/transcript", json=payload, headers=headers)
    resp.raise_for_status()
    transcript_id = resp.json()["id"]
    logger.info(f"Transcript ID: {transcript_id}")

    polling_url = f"{BASE_URL}/transcript/{transcript_id}"
    while True:
        result = requests.get(polling_url, headers=_headers()).json()
        status = result["status"]
        if status == "completed":
            logger.info("Transcription complete")
            return result
        elif status == "error":
            raise RuntimeError(f"AssemblyAI error: {result.get('error')}")
        else:
            logger.info(f"  status: {status} — waiting…")
            time.sleep(5)


# ── pdf ───────────────────────────────────────────────────────────────────────

def _speaker_color(speaker: str) -> HexColor:
    idx = (ord(speaker.upper()) - ord("A")) % len(SPEAKER_COLORS)
    return HexColor(SPEAKER_COLORS[idx])


def save_pdf(result: dict, output_path: str, meeting_title: str = "Meeting Transcript") -> str:
    logger.info(f"Generating PDF → {output_path}")
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
    )
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ZSTitle",
        parent=styles["Title"],
        fontSize=22,
        textColor=HexColor("#111827"),
        spaceAfter=4,
        fontName="Helvetica-Bold",
    )
    meta_style = ParagraphStyle(
        "ZSMeta",
        parent=styles["Normal"],
        fontSize=10,
        textColor=HexColor("#6B7280"),
        spaceAfter=20,
    )
    speaker_name_style = ParagraphStyle(
        "ZSSpeaker",
        parent=styles["Normal"],
        fontSize=10,
        fontName="Helvetica-Bold",
        spaceAfter=2,
    )
    utterance_style = ParagraphStyle(
        "ZSUtterance",
        parent=styles["Normal"],
        fontSize=11,
        textColor=HexColor("#1F2937"),
        spaceAfter=14,
        leading=16,
        leftIndent=12,
    )

    story = []

    # Title
    story.append(Paragraph(meeting_title, title_style))

    # Metadata line
    duration_s = result.get("audio_duration", 0)
    duration_fmt = f"{int(duration_s // 60)}m {int(duration_s % 60)}s" if duration_s else "unknown"
    lang = result.get("language_code", "").upper() or "unknown"
    story.append(Paragraph(f"Duration: {duration_fmt}  ·  Language: {lang}", meta_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#E5E7EB"), spaceAfter=16))

    # Speaker legend
    utterances = result.get("utterances", [])
    if utterances:
        speakers = sorted({u["speaker"] for u in utterances})
        legend_parts = [
            f'<font color="{_speaker_color(s).hexval()}" name="Helvetica-Bold">● Speaker {s}</font>'
            for s in speakers
        ]
        story.append(Paragraph("  ".join(legend_parts), meta_style))
        story.append(Spacer(1, 10))

    # Utterances
    if utterances:
        for utt in utterances:
            speaker = utt.get("speaker", "?")
            text = utt.get("text", "").strip()
            start_ms = utt.get("start", 0)
            ts = f"{int(start_ms/60000):02d}:{int((start_ms%60000)/1000):02d}"
            color = _speaker_color(speaker)

            name_para = Paragraph(
                f'<font color="{color.hexval()}">Speaker {speaker}</font>'
                f'<font color="#9CA3AF">  {ts}</font>',
                speaker_name_style,
            )
            text_para = Paragraph(text, utterance_style)
            story.extend([name_para, text_para])
    else:
        # Fallback plain text
        plain = result.get("text", "No transcript available.")
        story.append(Paragraph(plain, utterance_style))

    doc.build(story)
    logger.info(f"PDF saved: {output_path}")
    return output_path


# ── convenience end-to-end ────────────────────────────────────────────────────

def audio_to_pdf(audio_path: str, output_pdf: str, meeting_title: str = "Meeting Transcript") -> str:
    url = upload_audio(audio_path)
    result = transcribe(url)
    return save_pdf(result, output_pdf, meeting_title)
