import pytest

from job_scout.asr import (
    AsrResult,
    approve_transcript,
    delete_transcript_audio,
    save_transcript_draft,
    store_audio_draft,
)
from job_scout.storage import connect, initialize_database, save_user_profile


def test_voice_transcript_requires_review_before_approval(tmp_path):
    database = tmp_path / "job_scout.db"
    initialize_database(database)
    save_user_profile(
        database,
        profile_id="voice-profile",
        display_name="Voice Profile",
        profile_json={
            "profile_id": "voice-profile",
            "version": 1,
            "target_roles": ["Applied AI Engineer"],
            "evidence": [],
            "location_rule": "Poland",
        },
        status="draft",
    )
    audio_path, digest = store_audio_draft(
        tmp_path / "audio",
        profile_id="voice-profile",
        filename="answer.webm",
        content_type="audio/webm",
        content=b"synthetic-audio",
    )
    transcript_id = save_transcript_draft(
        database,
        profile_id="voice-profile",
        session_id=None,
        audio_path=audio_path,
        audio_sha256=digest,
        content_type="audio/webm",
        result=AsrResult(
            text="Robocza transkrypcja",
            detected_language="pl",
            provider="fake",
            provider_version="test-v1",
        ),
    )
    with connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM voice_transcripts WHERE transcript_id = ?",
            (transcript_id,),
        ).fetchone()[0] == "draft"
    approve_transcript(database, transcript_id, "Poprawiona transkrypcja")
    with connect(database) as connection:
        row = connection.execute(
            "SELECT status, corrected_text FROM voice_transcripts WHERE transcript_id = ?",
            (transcript_id,),
        ).fetchone()
    assert tuple(row) == ("approved", "Poprawiona transkrypcja")
    delete_transcript_audio(database, transcript_id)
    assert not audio_path.exists()


def test_audio_rejects_unsupported_or_oversized_input(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        store_audio_draft(
            tmp_path,
            profile_id="p",
            filename="x.txt",
            content_type="text/plain",
            content=b"x",
        )
