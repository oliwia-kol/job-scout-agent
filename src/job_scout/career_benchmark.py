"""Live acceptance benchmark for the local Bielik career interviewer."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .career import BielikInterviewResponder, InterviewTurn, _normalized
from .local_llm import LocalLlmClient


@dataclass(frozen=True)
class CareerScenario:
    scenario_id: str
    stage: str
    summary: str
    cv_text: str
    user_message: str
    expected_terms: tuple[str, ...]


@dataclass(frozen=True)
class CareerScenarioResult:
    scenario_id: str
    passed: bool
    checks: dict[str, bool]
    response: dict
    elapsed_ms: int
    error: str | None = None


SCENARIOS = (
    CareerScenario(
        scenario_id="cv-audit",
        stage="cv_audit",
        summary="Rozpoczynamy audyt faktów z CV.",
        cv_text="Built a Python automation that reduced a weekly manual workflow.",
        user_message="Możemy zacząć od tego, co naprawdę wynika z mojego CV.",
        expected_terms=("cv", "automaty"),
    ),
    CareerScenario(
        scenario_id="follow-up-depth",
        stage="outside_cv",
        summary="Użytkowniczka wspomniała o samodzielnym prototypie, bez skali i wyniku.",
        cv_text="Experience in risk operations and process improvement.",
        user_message=(
            "Po godzinach zbudowałam lokalny prototyp do porównywania ofert, "
            "ale nie opisałam jeszcze efektu."
        ),
        expected_terms=("prototyp", "efekt"),
    ),
    CareerScenario(
        scenario_id="skip-redundant-question",
        stage="working_style",
        summary="Brakowało przykładu działania pod presją i mierzalnego efektu.",
        cv_text="Coordinated stakeholders during a time-critical operational incident.",
        user_message=(
            "Gdy termin był zagrożony, podzieliłam problem na trzy części, uzgodniłam "
            "priorytety z zespołem i zamknęliśmy sprawę dzień przed terminem."
        ),
        expected_terms=("termin", "priorytet"),
    ),
    CareerScenario(
        scenario_id="negative-preference",
        stage="ambitions",
        summary="Ustalamy warunki najlepszego środowiska pracy i granice.",
        cv_text="Worked across business and technical teams.",
        user_message=(
            "Nie chcę roli opartej głównie na presales. Najlepiej działam, gdy mogę "
            "budować, testować i odpowiadać za realny efekt produktu."
        ),
        expected_terms=("presales", "efekt"),
    ),
    CareerScenario(
        scenario_id="uncertainty-discipline",
        stage="market_strategy",
        summary="Oceniamy role ambitne i ryzyka screeningu.",
        cv_text="Completed self-directed AI evaluation experiments.",
        user_message=(
            "Nie prowadziłam jeszcze produkcyjnego systemu ML, więc nie chcę, żeby profil "
            "sugerował doświadczenie, którego nie mam."
        ),
        expected_terms=("produkcyj", "doświadc"),
    ),
)


async def run_career_benchmark(
    *,
    base_url: str,
    model: str,
    system_prompt: str,
    scenario_ids: set[str] | None = None,
) -> dict:
    results: list[CareerScenarioResult] = []
    scenarios = [
        scenario
        for scenario in SCENARIOS
        if scenario_ids is None or scenario.scenario_id in scenario_ids
    ]
    async with LocalLlmClient(base_url, timeout_seconds=600) as client:
        health = await client.health()
        responder = BielikInterviewResponder(
            client,
            model=model,
            system_prompt=system_prompt,
        )
        for scenario in scenarios:
            context = _scenario_context(scenario)
            started = time.perf_counter()
            try:
                turn = await responder.respond(context)
                checks = _acceptance_checks(turn, context, scenario.expected_terms)
                results.append(
                    CareerScenarioResult(
                        scenario_id=scenario.scenario_id,
                        passed=all(checks.values()),
                        checks=checks,
                        response=turn.model_dump(mode="json"),
                        elapsed_ms=round((time.perf_counter() - started) * 1000),
                    )
                )
            except Exception as exc:  # live benchmark must preserve per-case failures
                results.append(
                    CareerScenarioResult(
                        scenario_id=scenario.scenario_id,
                        passed=False,
                        checks={},
                        response={},
                        elapsed_ms=round((time.perf_counter() - started) * 1000),
                        error=str(exc),
                    )
                )
    return {
        "schema_version": "career-interview-benchmark-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "model": model,
        "base_url": base_url,
        "health": health,
        "total": len(results),
        "passed": sum(item.passed for item in results),
        "failed": sum(not item.passed for item in results),
        "results": [asdict(item) for item in results],
    }


def write_career_benchmark(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _scenario_context(scenario: CareerScenario) -> dict:
    document_id = f"cv-{scenario.scenario_id}"
    message_id = f"user-{scenario.scenario_id}"
    return {
        "session": {
            "session_id": f"benchmark-{scenario.scenario_id}",
            "stage": scenario.stage,
            "summary": scenario.summary,
            "response_language": "pl",
        },
        "cv": {"document_id": document_id, "reviewed_text": scenario.cv_text},
        "approved_profile": {
            "target_roles": ["Applied AI Engineer", "AI Evaluation Engineer"],
            "location_rule": "Remote from Poland",
        },
        "recent_messages": [
            {
                "message_id": message_id,
                "role": "user",
                "content": scenario.user_message,
                "sequence": 1,
            }
        ],
        "approved_knowledge": [],
    }


def _acceptance_checks(
    turn: InterviewTurn,
    context: dict,
    expected_terms: tuple[str, ...],
) -> dict[str, bool]:
    sources = {
        context["cv"]["document_id"]: context["cv"]["reviewed_text"],
        **{
            message["message_id"]: message["content"]
            for message in context["recent_messages"]
        },
    }
    grounded = True
    for proposal in turn.knowledge_proposals:
        if not set(proposal.source_message_ids) <= sources.keys():
            grounded = False
            break
        if not any(
            _normalized(proposal.evidence_quote) in _normalized(sources[source_id])
            for source_id in proposal.source_message_ids
        ):
            grounded = False
            break
    combined = _normalized(
        " ".join([turn.response, *turn.questions, turn.session_summary])
    )
    latest_message_id = context["recent_messages"][-1]["message_id"]
    latest_message_is_cited = any(
        latest_message_id in proposal.source_message_ids
        for proposal in turn.knowledge_proposals
    )
    return {
        "max_three_questions": len(turn.questions) <= 3,
        "responds_to_latest_answer": (
            any(term in combined for term in expected_terms) or latest_message_is_cited
        ),
        "grounded_proposals": grounded,
        "polish_response": any(
            marker in combined
            for marker in ("ą", "ć", "ę", "ł", "ń", "ó", "ś", "ź", "ż", "dzię", "jak")
        ),
        "nonempty_summary": bool(turn.session_summary.strip()),
    }
