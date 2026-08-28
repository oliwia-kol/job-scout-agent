"""Persistent, provenance-aware Career Interview and Knowledge Base."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from .local_llm import LocalLlmClient
from .storage import connect, get_user_profile, initialize_database

CAREER_PROMPT_VERSION = "career-interview-pl-v1"
InterviewStage = Literal[
    "cv_audit",
    "outside_cv",
    "working_style",
    "ambitions",
    "market_strategy",
    "narrative",
    "review",
]
Provenance = Literal["cv_verified", "user_stated", "hypothesis"]


class KnowledgeProposal(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    statement_original: str = Field(min_length=1)
    canonical_english: str | None = None
    provenance: Provenance
    evidence_quote: str = Field(min_length=1)
    source_message_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    behavior: str | None = None
    example: str | None = None
    employer_value: str | None = None
    overinterpretation_risk: str | None = None


class InterviewTurn(BaseModel):
    response: str = Field(min_length=1)
    stage: InterviewStage
    questions: list[str] = Field(default_factory=list, max_length=3)
    session_summary: str = Field(min_length=1)
    knowledge_proposals: list[KnowledgeProposal] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @field_validator("questions")
    @classmethod
    def unique_nonempty_questions(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("questions must be unique")
        return cleaned


class InterviewResponder(Protocol):
    async def respond(self, context: dict) -> InterviewTurn: ...


class BielikInterviewResponder:
    """Structured adapter for a local OpenAI-compatible Bielik endpoint."""

    def __init__(
        self,
        client: LocalLlmClient,
        *,
        model: str,
        system_prompt: str,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt

    async def respond(self, context: dict) -> InterviewTurn:
        latest_user_message = next(
            (
                message["content"]
                for message in reversed(context.get("recent_messages", []))
                if message.get("role") == "user"
            ),
            "",
        )
        result = await self.client.structured_completion(
            model=self.model,
            system_prompt=self.system_prompt,
            user_prompt=(
                "Prowadź następną adaptacyjną rundę. Odpowiedź tekstowa ma najpierw "
                "odnieść się do sensu ostatniej wypowiedzi. Nie zadawaj więcej niż 3 pytań. "
                "Pisz zwięźle: 80–160 słów i maksymalnie 4 najważniejsze propozycje wiedzy. "
                "BEZWZGLĘDNIE pisz response, questions, open_questions, session_summary, "
                "category i statement_original po polsku, nawet gdy CV jest po angielsku. "
                "Angielski może wystąpić w canonical_english i dosłownym cytacie źródła. "
                "Zwróć kandydatów do bazy wiedzy wyłącznie z cytatem i source_message_ids.\n\n"
                + json.dumps(context, ensure_ascii=False, sort_keys=True)
                + "\n\nOSTATNIA WYPOWIEDŹ UŻYTKOWNIKA — odpowiedz na nią po polsku:\n"
                + latest_user_message
            ),
            schema=InterviewTurn,
            schema_name=CAREER_PROMPT_VERSION,
            temperature=0.3,
            max_tokens=1100,
            strict_schema=False,
            instruction_language="pl",
            validate=_validate_polish_turn,
        )
        return result.value


def start_interview(
    path: Path,
    *,
    profile_id: str,
    model_config: dict | None = None,
) -> str:
    profile = get_user_profile(path, profile_id)
    if not profile or profile["status"] not in {"ready", "cv_approved"}:
        raise ValueError("approved profile or reviewed CV is required")
    initialize_database(path)
    session_id = f"interview-{uuid.uuid4().hex}"
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        active = connection.execute(
            """
            SELECT session_id FROM career_interview_sessions
            WHERE profile_id = ? AND status = 'active'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (profile_id,),
        ).fetchone()
        if active:
            return str(active["session_id"])
        connection.execute(
            """
            INSERT INTO career_interview_sessions (
                session_id, profile_id, stage, status, prompt_version,
                model_config_json, summary, created_at, updated_at
            ) VALUES (?, ?, 'cv_audit', 'active', ?, ?, '', ?, ?)
            """,
            (
                session_id,
                profile_id,
                CAREER_PROMPT_VERSION,
                json.dumps(model_config or {}, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
    return session_id


def add_message(
    path: Path,
    *,
    session_id: str,
    role: Literal["user", "assistant"],
    content: str,
    metadata: dict | None = None,
) -> str:
    text = content.strip()
    if not text:
        raise ValueError("interview message cannot be empty")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    message_id = f"message-{uuid.uuid4().hex}"
    with connect(path) as connection:
        session = connection.execute(
            "SELECT status FROM career_interview_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not session or session["status"] != "active":
            raise ValueError("active interview session not found")
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM career_interview_messages WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO career_interview_messages (
                message_id, session_id, sequence, role, content, language,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pl', ?, ?)
            """,
            (
                message_id,
                session_id,
                sequence,
                role,
                text,
                json.dumps(metadata or {}, ensure_ascii=False),
                timestamp,
            ),
        )
        connection.execute(
            "UPDATE career_interview_sessions SET updated_at = ? WHERE session_id = ?",
            (timestamp, session_id),
        )
    return message_id


def build_interview_context(path: Path, session_id: str) -> dict:
    initialize_database(path)
    with connect(path) as connection:
        session = connection.execute(
            "SELECT * FROM career_interview_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise ValueError("interview session not found")
        recent = connection.execute(
            """
            SELECT message_id, role, content, sequence
            FROM career_interview_messages WHERE session_id = ?
            ORDER BY sequence DESC LIMIT 10
            """,
            (session_id,),
        ).fetchall()
        approved = connection.execute(
            """
            SELECT entry_id, category, statement_original, canonical_english, provenance
            FROM career_knowledge_entries
            WHERE profile_id = ? AND status = 'approved'
            ORDER BY reviewed_at DESC LIMIT 200
            """,
            (session["profile_id"],),
        ).fetchall()
        open_questions = connection.execute(
            """
            SELECT question_id, question
            FROM career_open_questions
            WHERE profile_id = ? AND status = 'open'
            ORDER BY created_at DESC LIMIT 20
            """,
            (session["profile_id"],),
        ).fetchall()
    profile = get_user_profile(path, session["profile_id"])
    document = profile.get("document") or {}
    recent_messages = [dict(row) for row in reversed(recent)]
    retrieval_query = " ".join(
        [session["summary"], *(message["content"] for message in recent_messages)]
    )
    ranked_knowledge = sorted(
        (dict(row) for row in approved),
        key=lambda row: _relevance_score(
            retrieval_query,
            " ".join(
                (
                    row["category"],
                    row["statement_original"],
                    row["canonical_english"] or "",
                )
            ),
        ),
        reverse=True,
    )[:20]
    return {
        "session": {
            "session_id": session_id,
            "stage": session["stage"],
            "summary": session["summary"],
            "response_language": "pl",
        },
        "cv": {
            "document_id": document.get("document_id"),
            "reviewed_text": document.get("corrected_text") or document.get("extracted_text", ""),
        },
        "approved_profile": profile["profile"],
        "recent_messages": recent_messages,
        "approved_knowledge": ranked_knowledge,
        "open_questions": [dict(row) for row in open_questions],
    }


def persist_interview_turn(
    path: Path,
    *,
    session_id: str,
    turn: InterviewTurn,
) -> str:
    context = build_interview_context(path, session_id)
    known_sources = {
        message["message_id"]: message["content"]
        for message in context["recent_messages"]
    }
    document_id = context["cv"].get("document_id")
    reviewed_text = context["cv"].get("reviewed_text")
    if document_id and reviewed_text:
        known_sources[document_id] = reviewed_text
    for proposal in turn.knowledge_proposals:
        if not set(proposal.source_message_ids) <= known_sources.keys():
            raise ValueError("knowledge proposal references an unknown source")
        sources = [
            known_sources[source_id] for source_id in proposal.source_message_ids
        ]
        quote_is_grounded = any(
            _normalized(proposal.evidence_quote) in _normalized(source)
            for source in sources
        )
        if not quote_is_grounded:
            raise ValueError("knowledge proposal evidence quote is not present in its source")
    assistant_id = add_message(
        path,
        session_id=session_id,
        role="assistant",
        content=turn.response + _question_suffix(turn.questions),
        metadata={"stage": turn.stage, "question_count": len(turn.questions)},
    )
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        session = connection.execute(
            "SELECT profile_id FROM career_interview_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        for proposal in turn.knowledge_proposals:
            connection.execute(
                """
                INSERT INTO career_knowledge_entries (
                    entry_id, profile_id, session_id, category, statement_original,
                    original_language, canonical_english, provenance, evidence_json,
                    confidence, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pl', ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    f"knowledge-{uuid.uuid4().hex}",
                    session["profile_id"],
                    session_id,
                    proposal.category,
                    proposal.statement_original,
                    proposal.canonical_english,
                    proposal.provenance,
                    json.dumps(
                        {
                            "quote": proposal.evidence_quote,
                            "message_ids": proposal.source_message_ids,
                            "behavior": proposal.behavior,
                            "example": proposal.example,
                            "employer_value": proposal.employer_value,
                            "overinterpretation_risk": proposal.overinterpretation_risk,
                        },
                        ensure_ascii=False,
                    ),
                    proposal.confidence,
                    timestamp,
                ),
            )
        for question in turn.open_questions:
            connection.execute(
                """
                INSERT INTO career_open_questions (
                    question_id, profile_id, session_id, question, status, created_at
                ) VALUES (?, ?, ?, ?, 'open', ?)
                """,
                (
                    f"question-{uuid.uuid4().hex}",
                    session["profile_id"],
                    session_id,
                    question,
                    timestamp,
                ),
            )
        connection.execute(
            """
            UPDATE career_interview_sessions
            SET stage = ?, summary = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (turn.stage, turn.session_summary, timestamp, session_id),
        )
    return assistant_id


def review_knowledge_entry(path: Path, entry_id: str, *, approve: bool) -> None:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        entry = connection.execute(
            "SELECT provenance, status FROM career_knowledge_entries WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        if not entry or entry["status"] != "proposed":
            raise ValueError("proposed knowledge entry not found")
        provenance = (
            "user_stated" if approve and entry["provenance"] == "hypothesis"
            else entry["provenance"]
        )
        connection.execute(
            """
            UPDATE career_knowledge_entries
            SET status = ?, provenance = ?, reviewed_at = ?
            WHERE entry_id = ?
            """,
            ("approved" if approve else "rejected", provenance, timestamp, entry_id),
        )


def create_knowledge_base_version(path: Path, profile_id: str) -> int:
    """Materialize an editable KB snapshot that does not depend on chat history."""
    profile = get_user_profile(path, profile_id)
    if not profile or profile["status"] not in {"ready", "cv_approved"}:
        raise ValueError("reviewed CV or approved profile is required")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        entries = connection.execute(
            """
            SELECT entry_id, category, statement_original, original_language,
                   canonical_english, provenance, evidence_json, confidence
            FROM career_knowledge_entries
            WHERE profile_id = ? AND status = 'approved'
            ORDER BY category, created_at
            """,
            (profile_id,),
        ).fetchall()
        if not entries:
            raise ValueError("at least one approved knowledge entry is required")
        questions = connection.execute(
            """
            SELECT question FROM career_open_questions
            WHERE profile_id = ? AND status = 'open' ORDER BY created_at
            """,
            (profile_id,),
        ).fetchall()
        version = connection.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1
            FROM career_knowledge_base_versions WHERE profile_id = ?
            """,
            (profile_id,),
        ).fetchone()[0]
        content = {
            "schema_version": "career-kb-v1",
            "profile_id": profile_id,
            "profile_version": profile["current_version"],
            "profile_snapshot": profile["profile"],
            "verified_evidence_bank": [
                {
                    **{
                        key: value
                        for key, value in dict(entry).items()
                        if key != "evidence_json"
                    },
                    "evidence": json.loads(entry["evidence_json"]),
                }
                for entry in entries
            ],
            "open_questions": [row["question"] for row in questions],
            "generated_at": timestamp,
        }
        connection.execute(
            """
            INSERT INTO career_knowledge_base_versions (
                profile_id, version, content_json, status, created_at
            ) VALUES (?, ?, ?, 'draft', ?)
            """,
            (
                profile_id,
                version,
                json.dumps(content, ensure_ascii=False, sort_keys=True),
                timestamp,
            ),
        )
    return int(version)


def approve_knowledge_base_version(path: Path, profile_id: str, version: int) -> None:
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT status FROM career_knowledge_base_versions
            WHERE profile_id = ? AND version = ?
            """,
            (profile_id, version),
        ).fetchone()
        if not row or row["status"] != "draft":
            raise ValueError("draft knowledge base version not found")
        connection.execute(
            """
            UPDATE career_knowledge_base_versions
            SET status = 'approved', approved_at = ?
            WHERE profile_id = ? AND version = ?
            """,
            (timestamp, profile_id, version),
        )


def get_latest_knowledge_base(path: Path, profile_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT * FROM career_knowledge_base_versions
            WHERE profile_id = ? ORDER BY version DESC LIMIT 1
            """,
            (profile_id,),
        ).fetchone()
    if not row:
        return None
    return {**dict(row), "content": json.loads(row["content_json"])}


def get_interview(path: Path, session_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        session = connection.execute(
            "SELECT * FROM career_interview_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            return None
        messages = connection.execute(
            """
            SELECT * FROM career_interview_messages
            WHERE session_id = ? ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
        entries = connection.execute(
            """
            SELECT * FROM career_knowledge_entries
            WHERE session_id = ? ORDER BY created_at
            """,
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session),
        "messages": [dict(row) for row in messages],
        "entries": [dict(row) for row in entries],
    }


def get_active_interview(path: Path, profile_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT session_id FROM career_interview_sessions
            WHERE profile_id = ? AND status = 'active'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (profile_id,),
        ).fetchone()
    return get_interview(path, row["session_id"]) if row else None


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _relevance_score(query: str, candidate: str) -> tuple[int, int]:
    query_terms = set(re.findall(r"\w{4,}", _normalized(query)))
    candidate_terms = set(re.findall(r"\w{4,}", _normalized(candidate)))
    overlap = len(query_terms & candidate_terms)
    return overlap, len(candidate_terms)


def _validate_polish_turn(turn: InterviewTurn) -> None:
    user_facing = " ".join(
        [turn.response, turn.session_summary, *turn.questions, *turn.open_questions]
    ).casefold()
    words = set(re.findall(r"\w+", user_facing))
    polish_markers = {
        "czy",
        "jak",
        "jakie",
        "jaki",
        "jest",
        "są",
        "się",
        "twoje",
        "twoja",
        "możesz",
        "dziękuję",
        "oraz",
        "które",
        "jako",
        "dla",
    }
    english_markers = {
        "the",
        "and",
        "your",
        "you",
        "what",
        "how",
        "can",
        "this",
        "that",
        "with",
        "from",
        "for",
        "let",
    }
    polish_score = len(words & polish_markers) + sum(
        user_facing.count(character) for character in "ąćęłńóśźż"
    )
    english_score = len(words & english_markers)
    if polish_score < 2 or english_score > polish_score:
        raise ValueError(
            "response, questions, open_questions and session_summary must be in Polish"
        )


def _question_suffix(questions: list[str]) -> str:
    if not questions:
        return ""
    return "\n\n" + "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
