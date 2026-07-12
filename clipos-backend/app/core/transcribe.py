import json
import subprocess
from pathlib import Path

from app.config import settings
from app.models.job import Job, engine
from sqlmodel import Session

# This replaces the Riva ASR stub with local Whisper for MVP development.
# Swap back to real Riva NIM if/when credentials are confirmed —
# the output schema is identical either way so nothing downstream breaks.

_model = None


def _get_model():
    """Load faster-whisper model once at module level, reuse across calls."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def _extract_audio(video_path: str, audio_path: str) -> None:
    """Extract 16kHz mono WAV audio from a video file via ffmpeg."""
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        audio_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _call_whisper_asr(audio_path: str) -> list[dict]:
    """Transcribe audio using faster-whisper with word-level timestamps.

    Returns list of {"word": str, "start": float, "end": float}.
    """
    model = _get_model()
    segments, _ = model.transcribe(audio_path, word_timestamps=True)

    words = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                words.append({
                    "word": w.word,
                    "start": round(w.start, 4),
                    "end": round(w.end, 4),
                })

    return words


def transcribe_video(job_id: str) -> list[dict]:
    """Full transcription pipeline: extract audio → Whisper → save transcript.json → update DB."""
    raw_dir = Path(settings.storage_raw) / job_id
    video_path = raw_dir / "source.mp4"
    audio_path = raw_dir / "audio.wav"
    transcript_path = raw_dir / "transcript.json"

    if not video_path.exists():
        raise FileNotFoundError(f"No source.mp4 found for job {job_id}")

    try:
        # Step 1: Extract audio
        _extract_audio(str(video_path), str(audio_path))

        # Step 2: Transcribe via faster-whisper
        transcript = _call_whisper_asr(str(audio_path))

        # Step 3: Save transcript.json
        transcript_path.write_text(json.dumps(transcript, indent=2))

        # Step 4: Update job status
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job:
                from datetime import datetime, timezone
                job.status = "transcribed"
                job.updated_at = datetime.now(timezone.utc)
                session.add(job)
                session.commit()

        return transcript

    except Exception as e:
        # Update job status to failed on any error
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job:
                from datetime import datetime, timezone
                job.status = "failed"
                job.error_message = str(e)
                job.updated_at = datetime.now(timezone.utc)
                session.add(job)
                session.commit()
        raise
