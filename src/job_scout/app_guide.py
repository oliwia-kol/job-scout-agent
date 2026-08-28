"""Persistent Bielik guide available before a candidate profile exists."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from .local_llm import LocalLlmClient
from .storage import connect, initialize_database

APP_GUIDE_PROMPT_VERSION = "app-guide-pl-v2"
APP_GUIDE_WELCOME = (
    "Cześć, jestem Bielik. Mogę przeprowadzić Cię przez cały AI Job Scout. "
    "Zacznij od zakładki Profil: zobaczysz tam, co już wiem i co warto uzupełnić. "
    "Jeśli profil jest gotowy, możemy od razu rozmawiać o ofertach; jeśli nie, "
    "pomogę Ci spokojnie przejść kolejne kroki. Możesz też zapytać, jak działa "
    "dowolna część programu."
)
APP_GUIDE_WELCOME_ACTIONS = [
    {"label": "Otwórz profil", "path": "/profiles"},
    {"label": "Zobacz aktualne oferty", "path": "/"},
]


class GuideAction(BaseModel):
    label: str = Field(min_length=1, max_length=70)
    path: str = Field(min_length=1, max_length=240)


class AppGuideTurn(BaseModel):
    response_pl: str = Field(min_length=1)
    suggested_actions: list[GuideAction] = Field(default_factory=list, max_length=3)


class AppGuideResponder(Protocol):
    async def respond(self, context: dict) -> AppGuideTurn: ...


class BielikAppGuideResponder:
    def __init__(self, client: LocalLlmClient, *, model: str, system_prompt: str) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt

    async def respond(self, context: dict) -> AppGuideTurn:
        response = await self.client.structured_completion(
            model=self.model,
            system_prompt=self.system_prompt,
            user_prompt=(
                "Odpowiedz na ostatnią wiadomość użytkownika po polsku. Odnieś się do "
                "aktualnego stanu aplikacji. Nie przypisuj użytkownikowi żadnych cech, "
                "kompetencji ani preferencji, jeśli nie ma zatwierdzonego profilu. "
                "Zaproponuj maksymalnie 3 działania wyłącznie z allowed_actions.\n\n"
                + json.dumps(context, ensure_ascii=False, sort_keys=True)
            ),
            schema=AppGuideTurn,
            schema_name=APP_GUIDE_PROMPT_VERSION,
            strict_schema=False,
            instruction_language="pl",
            max_tokens=450,
            temperature=0.25,
            validate=lambda turn: validate_app_guide_turn(context, turn),
        )
        return response.value


def get_or_create_app_guide(path: Path) -> dict:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            """
            SELECT * FROM app_guide_sessions
            WHERE status = 'active' ORDER BY updated_at DESC LIMIT 1
            """
        ).fetchone()
        if row and row["prompt_version"] != APP_GUIDE_PROMPT_VERSION:
            _refresh_untouched_welcome(connection, str(row["session_id"]))
            connection.execute(
                "UPDATE app_guide_sessions SET prompt_version = ? WHERE session_id = ?",
                (APP_GUIDE_PROMPT_VERSION, row["session_id"]),
            )
        if not row:
            timestamp = datetime.now(UTC).isoformat()
            session_id = f"app-guide-{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO app_guide_sessions (
                    session_id, status, prompt_version, created_at, updated_at
                ) VALUES (?, 'active', ?, ?, ?)
                """,
                (session_id, APP_GUIDE_PROMPT_VERSION, timestamp, timestamp),
            )
            _insert_message(
                connection,
                session_id=session_id,
                role="assistant",
                content=APP_GUIDE_WELCOME,
                metadata={"actions": APP_GUIDE_WELCOME_ACTIONS},
                timestamp=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM app_guide_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
    return get_app_guide(path, str(row["session_id"]))


def _refresh_untouched_welcome(connection, session_id: str) -> None:
    """Refresh only the seeded greeting; never rewrite a real conversation."""
    messages = connection.execute(
        """
        SELECT message_id, role FROM app_guide_messages
        WHERE session_id = ? ORDER BY sequence
        """,
        (session_id,),
    ).fetchall()
    if len(messages) != 1 or messages[0]["role"] != "assistant":
        return
    connection.execute(
        """
        UPDATE app_guide_messages
        SET content = ?, metadata_json = ?
        WHERE message_id = ?
        """,
        (
            APP_GUIDE_WELCOME,
            json.dumps(
                {"actions": APP_GUIDE_WELCOME_ACTIONS},
                ensure_ascii=False,
                sort_keys=True,
            ),
            messages[0]["message_id"],
        ),
    )


def get_app_guide(path: Path, session_id: str) -> dict:
    initialize_database(path)
    with connect(path) as connection:
        session = connection.execute(
            "SELECT * FROM app_guide_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise ValueError("app guide session not found")
        messages = connection.execute(
            """
            SELECT * FROM app_guide_messages
            WHERE session_id = ? ORDER BY sequence
            """,
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session),
        "messages": [
            {**dict(row), "metadata": json.loads(row["metadata_json"])}
            for row in messages
        ],
    }


def add_app_guide_message(
    path: Path,
    *,
    session_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> str:
    if role not in {"user", "assistant"} or not content.strip():
        raise ValueError("valid non-empty guide message required")
    initialize_database(path)
    timestamp = datetime.now(UTC).isoformat()
    with connect(path) as connection:
        session = connection.execute(
            "SELECT status FROM app_guide_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session or session["status"] != "active":
            raise ValueError("active app guide session not found")
        message_id = _insert_message(
            connection,
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata or {},
            timestamp=timestamp,
        )
        connection.execute(
            "UPDATE app_guide_sessions SET updated_at = ? WHERE session_id = ?",
            (timestamp, session_id),
        )
    return message_id


def update_app_guide_message_metadata(
    path: Path, *, message_id: str, metadata: dict
) -> bool:
    """Update delivery state without changing the durable conversation text."""
    initialize_database(path)
    with connect(path) as connection:
        cursor = connection.execute(
            "UPDATE app_guide_messages SET metadata_json=? WHERE message_id=?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), message_id),
        )
    return cursor.rowcount == 1


def get_app_guide_message(path: Path, message_id: str) -> dict | None:
    initialize_database(path)
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM app_guide_messages WHERE message_id=?", (message_id,)
        ).fetchone()
    if not row:
        return None
    return {**dict(row), "metadata": json.loads(row["metadata_json"])}


def build_app_guide_context(path: Path, session_id: str) -> dict:
    guide = get_app_guide(path, session_id)
    with connect(path) as connection:
        profiles = [
            dict(row)
            for row in connection.execute(
                """
                SELECT profile_id, display_name, status
                FROM user_profiles ORDER BY updated_at DESC
                """
            )
        ]
        offers = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, company, title FROM offers
                WHERE availability_status = 'active'
                ORDER BY last_seen_at DESC LIMIT 6
                """
            )
        ]
        latest_scan = connection.execute(
            """
            SELECT status, mode, sources_ok, sources_total, finished_at
            FROM collection_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
    ready_profiles = [item for item in profiles if item["status"] == "ready"]
    interview_profiles = [
        item for item in profiles if item["status"] in {"ready", "cv_approved"}
    ]
    allowed_actions = {
        "/today": "Przejdź do ekranu Dzisiaj",
        "/profiles": "Otwórz profile",
        "/profiles/new": "Dodaj profil i CV",
        "/": "Zobacz wszystkie oferty",
        "/applications": "Otwórz proces aplikacji",
        "/settings": "Otwórz ustawienia",
        "/notifications": "Otwórz powiadomienia",
    }
    for profile in interview_profiles:
        allowed_actions[f"/profiles/{profile['profile_id']}/interview"] = (
            f"Rozpocznij lub kontynuuj wywiad: {profile['display_name']}"
        )
    for offer in offers:
        path_value = (
            f"/offers/{offer['id']}/ask" if ready_profiles else f"/offers/{offer['id']}"
        )
        allowed_actions[path_value] = f"{offer['company']} — {offer['title']}"
    return {
        "mode": "career_guide" if interview_profiles else "application_guide",
        "application_state": {
            "profiles": profiles,
            "ready_profile_count": len(ready_profiles),
            "interview_profile_count": len(interview_profiles),
            "active_offer_count": len(offers),
            "latest_scan": dict(latest_scan) if latest_scan else None,
        },
        "capabilities": [
            "Dodawanie CV i lokalny OCR PDF",
            "Adaptacyjny wywiad kariery i Career Knowledge Base",
            "Monitoring oraz historia zmian ofert",
            "Rozmowy o ofertach oparte na zatwierdzonym profilu",
            "Lokalne modele i prywatny dostęp przez Tailscale",
        ],
        "allowed_actions": allowed_actions,
        "recent_messages": [
            {
                "message_id": item["message_id"],
                "role": item["role"],
                "content": item["content"],
            }
            for item in guide["messages"][-12:]
        ],
    }


def persist_app_guide_turn(
    path: Path, *, session_id: str, turn: AppGuideTurn
) -> str:
    context = build_app_guide_context(path, session_id)
    validate_app_guide_turn(context, turn)
    return add_app_guide_message(
        path,
        session_id=session_id,
        role="assistant",
        content=turn.response_pl,
        metadata={
            "actions": [action.model_dump() for action in turn.suggested_actions]
        },
    )


def validate_app_guide_turn(context: dict, turn: AppGuideTurn) -> None:
    allowed = context["allowed_actions"]
    if any(
        action.path not in allowed or action.label != allowed[action.path]
        for action in turn.suggested_actions
    ):
        raise ValueError("guide suggested an unavailable or mislabeled application action")
    polish_signal = r"[ąćęłńóśźż]|\b(?:jest|profil|oferta|możesz|najpierw|aplikacja)\b"
    if not re.search(polish_signal, turn.response_pl, re.I):
        raise ValueError("app guide response must be in Polish")


def _insert_message(
    connection,
    *,
    session_id: str,
    role: str,
    content: str,
    metadata: dict,
    timestamp: str,
) -> str:
    sequence = connection.execute(
        """
        SELECT COALESCE(MAX(sequence), 0) + 1
        FROM app_guide_messages WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()[0]
    message_id = f"app-guide-message-{uuid.uuid4().hex}"
    connection.execute(
        """
        INSERT INTO app_guide_messages (
            message_id, session_id, sequence, role, content, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            session_id,
            sequence,
            role,
            content.strip(),
            json.dumps(metadata, ensure_ascii=False),
            timestamp,
        ),
    )
    return message_id
