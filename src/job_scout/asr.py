"""Local ASR contracts with an explicit editable-draft approval boundary."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from .storage import connect, get_user_profile, initialize_database

MAX_AUDIO_BYTES = 25 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {
    "audio/webm",
    "audio/mp4",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
}


class AsrResult(BaseModel):
    text: str = Field(min_length=1)
    detected_language: str
    duration_ms: int | None = Field(default=None, ge=0)
    provider: str
    provider_version: str


class AsrProvider(Protocol):
    async def transcribe(self, audio_path: Path) -> AsrResult: ...


def store_audio_draft(
    root: Path,
    *,
    profile_id: str,
    filename: str,
    content_type: str,
    content: bytes,
) -> tuple[Path, str]:
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise ValueError("unsupported audio format")
    if not content or len(content) > MAX_AUDIO_BYTES:
        raise ValueError("audio must be between 1 byte and 25 MB")
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".webm", ".m4a", ".mp3", ".wav", ".mp4"}:
        suffix = ".audio"
    digest = hashlib.sha256(content).hexdigest()
    directory = root / profile_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(content)
    return path, digest


def save_transcript_draft(
    database_path: Path,
    *,
    profile_id: str,
    session_id: str | None,
    audio_path: Path,
    audio_sha256: str,
    content_type: str,
    result: AsrResult,
) -> str:
    if not get_user_profile(database_path, profile_id):
        raise ValueError("profile not found")
    initialize_database(database_path)
    transcript_id = f"transcript-{uuid.uuid4().hex}"
    timestamp = datetime.now(UTC).isoformat()
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO voice_transcripts (
                transcript_id, profile_id, session_id, audio_path, audio_sha256,
                content_type, duration_ms, detected_language, provider,
                provider_version, raw_text, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                transcript_id,
                profile_id,
                session_id,
                str(audio_path),
                audio_sha256,
                content_type,
                result.duration_ms,
                result.detected_language,
                result.provider,
                result.provider_version,
                result.text,
                timestamp,
            ),
        )
    return transcript_id


def approve_transcript(
    database_path: Path, transcript_id: str, corrected_text: str
) -> None:
    text = corrected_text.strip()
    if not text:
        raise ValueError("approved transcript cannot be empty")
    initialize_database(database_path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE voice_transcripts
            SET corrected_text = ?, status = 'approved', approved_at = ?
            WHERE transcript_id = ? AND status = 'draft' AND deleted_at IS NULL
            """,
            (text, timestamp, transcript_id),
        )
    if cursor.rowcount != 1:
        raise ValueError("draft transcript not found")


def delete_transcript_audio(database_path: Path, transcript_id: str) -> None:
    initialize_database(database_path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT audio_path FROM voice_transcripts WHERE transcript_id = ?",
            (transcript_id,),
        ).fetchone()
        if not row:
            raise ValueError("transcript not found")
        Path(row["audio_path"]).unlink(missing_ok=True)
        connection.execute(
            "UPDATE voice_transcripts SET deleted_at = ? WHERE transcript_id = ?",
            (timestamp, transcript_id),
        )


def list_transcripts(database_path: Path, profile_id: str) -> list[dict]:
    initialize_database(database_path)
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM voice_transcripts
            WHERE profile_id = ? ORDER BY created_at DESC
            """,
            (profile_id,),
        ).fetchall()
    return [dict(row) for row in rows]
