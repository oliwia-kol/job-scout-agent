"""Small local-first web panel for the usable no-LLM prototype."""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .career import get_latest_knowledge_base
from .collector import collect_sources
from .cv_tailoring import (
    CvTailoringError,
    get_application_package,
    record_package_event,
)
from .domain import (
    ApplicationStatus,
    CandidateEvidence,
    CandidateProfile,
    EvaluationRunItem,
    EvaluationRunStatus,
    PipelineRun,
)
from .profiles import CvExtractionError, extract_cv_pdf, file_sha256, safe_filename, store_cv_bytes
from .requirement_matrix import MATRIX_VERSION
from .role_direction import ROLE_RULES_VERSION
from .role_fit import PROMPT_VERSION as ROLE_FIT_PROMPT_VERSION
from .settings import Settings
from .sources import load_sources
from .storage import (
    approve_profile_document,
    cancel_run,
    clear_false_negative_feedback,
    connect,
    create_evaluation_run,
    get_collection_monitoring,
    get_collection_run,
    get_current_evaluation_feedback,
    get_latest_evaluation_run,
    get_latest_offer_translation,
    get_offer,
    get_pipeline_run,
    get_user_profile,
    is_profile_ready_for_scoring,
    list_evaluation_run_items,
    list_lab_experiments,
    list_lab_model_profiles,
    list_notifications,
    list_offer_events,
    list_offer_section_evidence,
    list_offer_versions,
    list_offers,
    list_pipeline_runs,
    list_profile_versions,
    list_user_profiles,
    mark_interrupted_runs,
    mark_notification_read,
    persist_cancelled_collection,
    persist_clean_offer,
    persist_monitored_collection,
    profile_readiness,
    resolve_profile_fact_conflict,
    resume_run,
    retry_failed_evaluation_run,
    review_profile_fact,
    save_collection_rejections,
    save_lab_experiment,
    save_lab_model_profile,
    save_profile_document,
    save_profile_fact,
    save_user_profile,
    set_false_negative_feedback,
    update_application_status,
    update_run_state,
)
from .web_ui import STATIC_ROOT, layout

STATUS_LABELS = {
    "new": "Nowa",
    "saved": "Zapisana",
    "rejected": "Odrzucona przeze mnie",
    "applying": "Aplikuję",
    "applied": "CV wysłane",
    "review_later": "Na później",
}


def _profile_fact_value(value: object) -> str:
    """Human-readable fact value without leaking internal JSON into the profile UI."""
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    if isinstance(value, dict) and value.get("name"):
        issuer = f" · {value['issuer']}" if value.get("issuer") else ""
        note = f" ({value['note']})" if value.get("note") else ""
        return f"{value['name']}{issuer}{note}"
    if isinstance(value, dict) and value.get("title"):
        tech = ", ".join(value.get("technologies") or [])
        suffix = f" · {tech}" if tech else ""
        return f"{value['title']}: {value.get('cv_description', '')}{suffix}".strip()
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


CV_ASSET_CATEGORIES = {
    "certification",
    "project",
    "skill",
    "achievement",
    "experience_bullet",
    "profile_summary_variant",
    "keyword",
}


def _split_comma_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


RUN_MODE_LABELS = {
    "single_offer_eval": "Test 1 oferty",
    "live_single_offer": "Ocena 1 pobranej oferty",
    "frozen_evaluation": "Szybki test 10 ofert",
    "frozen_full_pipeline": "Pełny test 10 ofert",
    "dummy_full_flow": "Starszy dummy_full_flow",
}

ProgressCallback = Callable[[int, int, str, str], None]
DemoRunner = Callable[[str, ProgressCallback], Awaitable[None]]
DemoRunPreparer = Callable[[str, str | None], str]


class DemoJobController:
    def __init__(self, database_path: Path, runner: DemoRunner) -> None:
        self.database_path = database_path
        self.runner = runner
        self.task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, run_id: str) -> bool:
        if self.active:
            return False
        self.task = asyncio.create_task(self._run(run_id))
        return True

    def cancel(self) -> bool:
        if not self.active:
            return False
        if self.task:
            self.task.cancel()
        return True

    async def _run(self, run_id: str) -> None:
        try:
            await self.runner(run_id, self.update_progress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            run = get_pipeline_run(self.database_path, run_id)
            if run and run["status"] == EvaluationRunStatus.RUNNING.value:
                update_run_state(
                    self.database_path,
                    run_id,
                    status=EvaluationRunStatus.FAILED.value,
                    current_stage="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=datetime.now(UTC),
                )

    def update_progress(self, completed: int, total: int, company: str, stage: str) -> None:
        # The callback remains available for alternate runners. The UI reads SQLite only.
        del completed, total, company, stage


class ScanController:
    """One in-process scan with an API-friendly, non-persistent live snapshot.

    The final result is persisted in SQLite; keeping only progress in memory avoids
    writing half-finished offer state and makes a server restart safely visible.
    """

    def __init__(self, database_path: Path, project_root: Path) -> None:
        self.database_path = database_path
        self.project_root = project_root
        self.task: asyncio.Task | None = None
        self.snapshot: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, mode: str) -> dict[str, Any] | None:
        if self.active:
            return None
        run_id = f"scan-{uuid.uuid4().hex[:16]}"
        self.snapshot = {
            "run_id": run_id,
            "mode": mode,
            "status": "running",
            "current_source": None,
            "sources_checked": 0,
            "sources_total": 0,
            "discovered": 0,
            "rejected_title": 0,
            "rejected_location": 0,
            "errors": 0,
            "accepted": 0,
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "unavailable": 0,
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.task = asyncio.create_task(self._run(run_id, mode))
        return self.snapshot

    def cancel(self) -> bool:
        if not self.active or not self.task:
            return False
        self.task.cancel()
        return True

    async def _run(self, run_id: str, mode: str) -> None:
        assert self.snapshot is not None
        try:
            sources = [
                source
                for source in load_sources(self.project_root / "config/sources.json")
                if source.enabled
            ]
            if mode == "quick":
                # The quick scan is deliberately broad but shallow: it samples each source.
                limit, max_candidates = 1, 4
            else:
                limit, max_candidates = None, None
            self.snapshot["sources_total"] = len(sources)
            self.snapshot["current_source"] = "Przygotowanie źródeł"

            def update_scan_progress(company: str, checked: int, total: int) -> None:
                self.snapshot.update(
                    current_source=company, sources_checked=checked, sources_total=total
                )

            result = await collect_sources(
                sources,
                limit_per_source=limit,
                max_candidates_per_source=max_candidates,
                progress_callback=update_scan_progress,
            )
            self.snapshot.update(
                current_source="Zapisywanie wyników",
                sources_checked=len(result.observations),
                discovered=sum(item.discovered_count for item in result.observations),
                rejected_title=sum(item.stage == "title" for item in result.rejected),
                rejected_location=sum(item.stage == "location" for item in result.rejected),
                errors=len(result.errors),
                accepted=len(result.offers),
            )
            before = {
                item["id"]: item["current_content_sha256"]
                for item in list_offers(self.database_path, availability="")
            }
            persisted = persist_monitored_collection(
                self.database_path,
                run_id=run_id,
                mode=mode,
                offers=result.offers,
                source_urls={source.id: str(source.career_url) for source in sources},
                observations=result.observations,
                started_at=datetime.fromisoformat(self.snapshot["started_at"]),
            )
            save_collection_rejections(
                self.database_path,
                run_id,
                result.rejected,
                discovered_count=self.snapshot["discovered"],
                processing_error_count=len(result.errors),
            )
            after = list_offers(self.database_path, availability="")
            self.snapshot.update(
                status=persisted["status"],
                current_source=None,
                unavailable=persisted["offers_marked_unavailable"],
                new=sum(item["id"] not in before for item in after),
                updated=sum(
                    item["id"] in before and before[item["id"]] != item["current_content_sha256"]
                    for item in after
                ),
                unchanged=sum(
                    item["id"] in before and before[item["id"]] == item["current_content_sha256"]
                    for item in after
                ),
                finished_at=datetime.now(UTC).isoformat(),
            )
        except asyncio.CancelledError:
            current_source = self.snapshot.get("current_source")
            self.snapshot.update(
                status="cancelled", current_source=None, finished_at=datetime.now(UTC).isoformat()
            )
            persist_cancelled_collection(
                self.database_path,
                run_id=run_id,
                mode=mode,
                started_at=self.snapshot["started_at"],
                sources_total=self.snapshot["sources_total"],
                sources_checked=self.snapshot["sources_checked"],
                current_source=current_source,
            )
            raise
        except Exception as exc:
            self.snapshot.update(
                status="failed",
                current_source=None,
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(UTC).isoformat(),
            )


def create_app(
    database_path: Path,
    *,
    project_root: Path | None = None,
    demo_runner: DemoRunner | None = None,
    demo_run_preparer: DemoRunPreparer | None = None,
) -> FastAPI:
    mark_interrupted_runs(database_path)

    root = project_root or Path.cwd()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(
        title="AI Job Scout",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    runner = demo_runner or (
        lambda run_id, progress: _run_dummy_demo(root, database_path, run_id, progress)
    )
    prepare_run = demo_run_preparer or (
        lambda mode, offer_key: _prepare_dummy_demo_run(root, database_path, mode, offer_key)
    )
    controller = DemoJobController(database_path, runner)
    scan_controller = ScanController(database_path, root)
    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    @app.get("/healthz", include_in_schema=False)
    def health() -> dict:
        with connect(database_path) as connection:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
            database_ok = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            latest_run = connection.execute(
                "SELECT status, finished_at FROM collection_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return {
            "status": "ok" if database_ok else "degraded",
            "database": "ok" if database_ok else "error",
            "schema_version": schema_version,
            "latest_collection": dict(latest_run) if latest_run else None,
        }

    @app.get("/api/scans/active", include_in_schema=False)
    def scan_status() -> dict:
        """Polling endpoint used by the Offers operational bar."""
        if scan_controller.snapshot:
            return scan_controller.snapshot
        latest = get_collection_monitoring(database_path)["latest_run"]
        return {"status": "idle", "latest": latest}

    @app.post("/api/scans", include_in_schema=False)
    def start_scan(payload: dict[str, str]) -> dict:
        mode = payload.get("mode", "quick")
        if mode not in {"quick", "full"}:
            raise HTTPException(status_code=422, detail="mode must be quick or full")
        snapshot = scan_controller.start(mode)
        if snapshot is None:
            return {"active": True, **(scan_controller.snapshot or {})}
        return {"active": False, **snapshot}

    @app.delete("/api/scans/active", include_in_schema=False)
    def cancel_scan() -> dict:
        if not scan_controller.cancel():
            raise HTTPException(status_code=409, detail="No scan is active")
        return {"status": "cancelling", "run_id": scan_controller.snapshot["run_id"]}

    @app.get("/api/scans/{run_id}", include_in_schema=False)
    def scan_detail(run_id: str) -> dict:
        if scan_controller.snapshot and scan_controller.snapshot["run_id"] == run_id:
            return scan_controller.snapshot
        run = get_collection_run(database_path, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Scan not found")
        return run

    @app.get("/api/evaluations/active", include_in_schema=False)
    def evaluation_status() -> dict:
        run = get_latest_evaluation_run(database_path)
        if not run:
            return {"status": "idle"}
        items = list_evaluation_run_items(database_path, run["run_id"])
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "profile_id": run.get("profile_id"),
            "profile_version": run.get("profile_version"),
            "stage": run.get("current_stage"),
            "total": run["total_items"],
            "completed": run["completed_items"],
            "failed": sum(item["status"] == "failed" for item in items),
            "pending": sum(item["status"] == "pending" for item in items),
        }

    @app.post("/api/evaluations", include_in_schema=False)
    async def start_product_evaluation(payload: dict[str, Any]) -> dict:
        profile_id = str(payload.get("profile_id") or "")
        offer_ids = payload.get("offer_ids")
        if not isinstance(offer_ids, list):
            profile_row = get_user_profile(database_path, profile_id)
            if not profile_row:
                raise HTTPException(status_code=400, detail="Choose an approved profile")
            offer_ids = [
                item["id"]
                for item in list_offers(
                    database_path, availability="active", role_visibility="visible"
                )
                if _offer_needs_evaluation(item, profile_row["profile"])
            ]
        if controller.active:
            raise HTTPException(status_code=409, detail="An evaluation is already active")
        try:
            run_id = _prepare_live_offer_evaluation(database_path, profile_id, offer_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not controller.start(run_id):
            cancel_run(database_path, run_id)
            raise HTTPException(status_code=409, detail="An evaluation is already active")
        return {"run_id": run_id, "status": "running"}

    @app.delete("/api/evaluations/active", include_in_schema=False)
    def cancel_product_evaluation() -> dict:
        run = get_latest_evaluation_run(database_path)
        if not run or run["status"] != "running":
            raise HTTPException(status_code=409, detail="No evaluation is active")
        cancel_run(database_path, run["run_id"])
        controller.cancel()
        return {"run_id": run["run_id"], "status": "cancelled"}

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/today", response_class=HTMLResponse)
    def today_page() -> HTMLResponse:
        monitoring = get_collection_monitoring(database_path)
        recent_events = list_offer_events(database_path, limit=5)
        unread_notifications = list_notifications(database_path, unread_only=True, limit=20)
        offers = list_offers(database_path, availability="active", role_visibility="visible")
        profiles = list_user_profiles(database_path)
        in_progress = [
            item
            for item in list_offers(database_path, availability="")
            if item["application_status"] in {"saved", "review_later", "applying", "applied"}
        ]
        latest = monitoring["latest_run"]
        ready_profiles = [item for item in profiles if item["status"] == "ready"]
        if not profiles:
            focus_title = "Zacznijmy spokojnie od Twojego profilu."
            focus_copy = (
                "Dodaj CV, sprawdź odczyt i zatwierdź fakty profilu. "
                "Dopiero potem porównamy je z ofertami."
            )
            primary_action = '<a class="button" href="/profiles/new">Utwórz mój profil</a>'
        elif not ready_profiles:
            cv_approved = [item for item in profiles if item["status"] == "cv_approved"]
            focus_title = (
                "CV jest gotowe. Teraz poznajmy Twój kierunek."
                if cv_approved
                else "Sprawdź odczyt CV i zatwierdź profil."
            )
            focus_copy = (
                "Uzupełnij doświadczenie i preferencje w profilu. "
                "Brak informacji nigdy nie ukryje oferty."
                if cv_approved
                else "Najpierw popraw odczyt CV. Potem zdecydujesz, czy chcesz od razu "
                "doprecyzować preferencje."
            )
            primary_action = (
                f'<a class="button" href="/profiles/{escape((cv_approved or profiles)[0]["profile_id"])}">'
                + ("Uzupełnij profil" if cv_approved else "Sprawdź odczyt CV")
                + "</a>"
            )
        else:
            focus_title = "Masz gotowy profil. Zobacz, co dziś warto sprawdzić."
            focus_copy = (
                "Nie musisz przeglądać wszystkiego. Zacznij od najnowszych ofert, "
                "a ocena pokaże wymagania i luki w profilu."
            )
            primary_action = '<a class="button" href="/">Przejrzyj oferty</a>'
        secondary_action = '<a class="button button--quiet" href="/profiles">Mój profil</a>'
        profile_step = "is-done" if ready_profiles else ""
        scan_step = "is-done" if latest else ""
        decision_step = "is-done" if in_progress else ""
        active_profile_id = ready_profiles[0]["profile_id"] if len(ready_profiles) == 1 else None
        if active_profile_id:
            active_profile = get_user_profile(database_path, active_profile_id)["profile"]
            for offer in offers:
                offer["needs_evaluation"] = _offer_needs_evaluation(offer, active_profile)
        cards = "".join(_offer_card(item, profile_id=active_profile_id) for item in offers[:3])
        if not cards:
            cards = (
                '<div class="empty-state"><h3>Jeszcze cicho.</h3>'
                "<p>Po pierwszym skanie pokażemy tutaj najświeższe role.</p></div>"
            )
        change_cards = "".join(_offer_event_card(item) for item in recent_events) or (
            '<div class="empty-state"><p>Zmiany ofert pojawią się po odświeżeniu źródeł.</p></div>'
        )
        content = f"""<div class="wrap panel-section">
          <section class="today-hero" aria-labelledby="today-focus">
            <div><p class="eyebrow">Twój następny krok</p>
              <h2 id="today-focus">{focus_title}</h2>
              <p>{focus_copy}</p>
              <div class="today-hero__actions">{primary_action}{secondary_action}</div>
            </div>
            <aside class="today-hero__signal" aria-label="Dzisiejszy stan ofert">
              <span class="eyebrow">W skrócie</span>
              <strong>{monitoring["active_offers"]}</strong>
              <span>aktywnych ofert</span>
              <p>{len(in_progress)} zapisanych lub w aplikowaniu</p>
              <a href="/notifications">{len(unread_notifications)} nowych zmian →</a>
            </aside>
          </section>
          <section class="journey" aria-label="Jak działa AI Job Scout">
            <article class="journey-step {profile_step}" data-step="1">
              <h3>Poznajmy Twój profil</h3>
              <p>CV, zatwierdzone fakty i preferencje tworzą kontekst do porównań.</p>
              <a href="/profiles">Otwórz profil →</a>
            </article>
            <article class="journey-step {scan_step}" data-step="2">
              <h3>Scout pilnuje ofert</h3>
              <p>Program zapisuje nowe role, zmiany oraz potwierdzone zniknięcia.</p>
              <a href="/">Zobacz oferty →</a>
            </article>
            <article class="journey-step {decision_step}" data-step="3">
              <h3>Ty podejmujesz decyzję</h3>
              <p>Ocena rozdziela potencjał roli, siłę CV, warunki i rozwój.</p>
              <a href="/applications">Mój proces →</a>
            </article>
          </section>
          <div class="section-heading"><div><p class="eyebrow">Najnowsze</p>
            <h2>Oferty do przejrzenia</h2></div><a href="/">Wszystkie oferty →</a></div>
          <div class="grid">{cards}</div>
          <div class="section-heading"><div><p class="eyebrow">Monitoring</p>
            <h2>Ostatnie zmiany</h2></div>
            <a href="/notifications">Cała aktywność →</a></div>
          <div class="grid">{change_cards}</div>
        </div>"""
        return layout(
            content,
            "Start · AI Job Scout",
            active="today",
            page_title="Dzień dobry",
            eyebrow="Spokojny plan na dziś",
        )

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications_page() -> HTMLResponse:
        notifications = list_notifications(database_path)
        cards = (
            "".join(
                f"""<article class="card"><p class="company">
            {"Nowe" if not item["read_at"] else "Przeczytane"} ·
            {escape(item["created_at"][:16].replace("T", " "))}</p>
            <h3>{escape(item["title"])}</h3><p>{escape(item["body"])}</p>
            <div class="actions"><a class="button" href="{escape(item["private_link"])}">
            Otwórz</a>{"" if item["read_at"] else f'<form method="post" action="/notifications/{item["notification_id"]}/read"><button class="button--quiet" type="submit">Oznacz jako przeczytane</button></form>'}</div>
            </article>"""
                for item in notifications
            )
            or '<div class="empty-state"><h2>Brak powiadomień.</h2></div>'
        )
        return layout(
            f'<div class="wrap panel-section"><div class="grid">{cards}</div></div>',
            "Powiadomienia · AI Job Scout",
            active="notifications",
            page_title="Powiadomienia",
            eyebrow="Spokojna skrzynka zmian",
        )

    @app.post("/notifications/{notification_id}/read")
    def read_notification(notification_id: str) -> RedirectResponse:
        if not mark_notification_read(database_path, notification_id):
            raise HTTPException(status_code=404, detail="Notification not found")
        return RedirectResponse("/notifications", status_code=303)

    @app.get("/applications", response_class=HTMLResponse)
    def applications_page() -> HTMLResponse:
        tracked = [
            item
            for item in list_offers(database_path, availability="")
            if item["application_status"] in {"saved", "review_later", "applying", "applied"}
        ]
        columns = []
        for status, label in (
            ("saved", "Zapisane"),
            ("review_later", "Do decyzji"),
            ("applying", "Przygotowuję"),
            ("applied", "Wysłane"),
        ):
            items = [item for item in tracked if item["application_status"] == status]
            cards = "".join(_offer_card(item) for item in items) or (
                '<div class="empty-state"><p>Brak ofert w tym etapie.</p></div>'
            )
            columns.append(
                f'<section><div class="section-heading"><div><p class="eyebrow">'
                f"{len(items)} ofert</p><h2>{label}</h2></div></div>"
                f'<div class="grid">{cards}</div></section>'
            )
        intro = (
            '<p class="synthetic">To jest pierwszy działający widok procesu. Terminy, '
            "notatki i historia kontaktu zostaną dodane w Fazie 12.</p>"
        )
        return layout(
            f'<div class="wrap panel-section">{intro}{"".join(columns)}</div>',
            "Aplikacje · AI Job Scout",
            active="applications",
            page_title="Aplikacje",
            eyebrow="Od zainteresowania do decyzji",
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page() -> HTMLResponse:
        monitoring = get_collection_monitoring(database_path)
        settings = Settings.from_env()
        latest = monitoring["latest_run"]
        latest_status = (
            f"{latest['sources_ok']}/{latest['sources_total']} źródeł · {latest['status']}"
            if latest
            else "brak skanu"
        )
        telegram_ready = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        content = f"""<div class="wrap panel-section">
          <section class="hero-grid">
            <article class="hero-card"><p class="eyebrow">Prywatność i kontrola</p>
              <h2>Twoje dane zostają tutaj.</h2>
              <p>SQLite, CV, wyniki modeli i historia ofert są przechowywane lokalnie.
              Kanały zewnętrzne pozostają opcjonalne.</p></article>
            <aside class="hero-aside"><p class="eyebrow">Stan systemu</p>
              <strong>{escape(latest_status)}</strong>
              <p class="muted">{monitoring["active_offers"]} aktywnych ofert w lokalnej bazie.</p>
              <a href="/monitoring">Pełna diagnostyka źródeł →</a></aside>
          </section>
          <section class="form-card"><h2>Powiadomienia</h2>
            <div class="runtime-list">
              <li><span>Skrzynka w aplikacji</span><span class="runtime-good">gotowa</span></li>
              <li><span>PWA Web Push</span><span class="runtime-warn">wymaga VAPID</span></li>
              <li><span>Telegram (opcjonalny)</span><span class="{"runtime-good" if telegram_ready else "runtime-warn"}">{"skonfigurowany" if telegram_ready else "brak konfiguracji"}</span></li>
            </div></section>
          <section class="form-card"><h2>Tryb deweloperski</h2>
            <p class="muted">Modele, przebiegi, surowe snapshoty i diagnostyka są
            odseparowane od codziennego produktu.</p>
            <a class="button button--quiet" href="/dev">Otwórz Developer Workspace</a>
          </section>
        </div>"""
        return layout(
            content,
            "Ustawienia · AI Job Scout",
            active="settings",
            page_title="Ustawienia",
            eyebrow="Prywatność, kanały i health",
        )

    @app.get("/dev", response_class=HTMLResponse)
    def developer_workspace() -> HTMLResponse:
        content = """<div class="wrap panel-section">
          <p class="dev-banner"><strong>Warstwa deweloperska.</strong> Zmiany tutaj mogą
          wpływać na wyniki eksperymentów, ale nie są potrzebne do codziennej obsługi ofert.</p>
          <div class="grid">
            <a class="card" href="/lab"><p class="company">Konfiguracja</p>
              <h2>Modele i eksperymenty</h2><p class="muted">Role, GGUF, seed, temperatura,
              dataset i prompt.</p></a>
            <a class="card" href="/model-lab/demo"><p class="company">Playground</p>
              <h2>Uruchom kontrolowany test</h2><p class="muted">Jedna oferta albo zamrożony
              zestaw porównawczy.</p></a>
            <a class="card" href="/model-lab/runs"><p class="company">Ledger</p>
              <h2>Historia przebiegów</h2><p class="muted">Status, retry, błędy i artefakty
              oceny.</p></a>
            <a class="card" href="/monitoring"><p class="company">Źródła</p>
              <h2>Diagnostyka kolektorów</h2><p class="muted">Odkryte oferty, puste odpowiedzi
              i zasada bezpiecznego znikania.</p></a>
          </div>
        </div>"""
        return layout(
            content,
            "Developer Workspace · AI Job Scout",
            shell="developer",
            active="dev",
            page_title="Developer Workspace",
            eyebrow="Modele, pipeline i obserwowalność",
        )

    @app.get("/", response_class=HTMLResponse)
    def index(
        q: str = Query(default=""),
        company: str = Query(default=""),
        status: str = Query(default=""),
        availability: str = Query(default="active"),
        feedback: str = Query(default=""),
        role: str = Query(default="visible"),
        fit: str = Query(default=""),
    ) -> HTMLResponse:
        monitoring = get_collection_monitoring(database_path)
        all_offers = list_offers(database_path)
        offers = list_offers(
            database_path,
            query=q,
            company=company,
            status=status,
            availability=availability,
            feedback=feedback,
            role_visibility=role if role in {"visible", "hidden", "all"} else "visible",
        )
        companies = sorted({item["company"] for item in all_offers}, key=str.casefold)
        company_options = "".join(
            f'<option value="{escape(name)}" {"selected" if name == company else ""}>'
            f"{escape(name)}</option>"
            for name in companies
        )
        status_options = "".join(
            f'<option value="{value}" {"selected" if value == status else ""}>'
            f"{escape(label)}</option>"
            for value, label in STATUS_LABELS.items()
        )
        availability_options = "".join(
            f'<option value="{value}" {"selected" if value == availability else ""}>'
            f"{label}</option>"
            for value, label in (
                ("active", "Widoczne teraz"),
                ("unavailable", "Potwierdzone jako niedostępne"),
                ("", "Wszystkie dostępności"),
            )
        )
        ready_profiles = [
            item
            for item in list_user_profiles(database_path)
            if is_profile_ready_for_scoring(database_path, item["profile_id"])
        ]
        active_profile_id = ready_profiles[0]["profile_id"] if len(ready_profiles) == 1 else None
        active_profile = (
            get_user_profile(database_path, active_profile_id)["profile"]
            if active_profile_id
            else None
        )
        pending_evaluations = (
            sum(
                _offer_needs_evaluation(item, active_profile)
                for item in all_offers
                if item["offer"].get("role_direction") != "software"
            )
            if active_profile
            else 0
        )
        if active_profile:
            for offer in offers:
                offer["needs_evaluation"] = _offer_needs_evaluation(offer, active_profile)
        if fit == "ambitious":
            offers = [offer for offer in offers if _is_ambitious_offer(offer)]
        evaluation_action = (
            f'''<button type="button" data-evaluate-stale data-profile-id="{escape(active_profile_id)}">
              Oceń nowe i nieaktualne</button><span class="muted">Profil: {escape(ready_profiles[0]["display_name"])} · wersja {ready_profiles[0]["current_version"]}</span>'''
            if active_profile_id
            else '<a class="button button--quiet" href="/profiles">Wybierz zatwierdzony profil do oceny</a>'
        )
        cards = "".join(_offer_card(item, profile_id=active_profile_id) for item in offers)
        latest = monitoring["latest_run"]
        latest_note = (
            f"{latest['sources_ok']}/{latest['sources_total']} źródeł OK · "
            f"{latest['mode']} · {latest['finished_at'][:16].replace('T', ' ')}"
            if latest
            else "Nie uruchomiono jeszcze demo-v2-scan"
        )
        filters_are_active = bool(
            company or status or feedback or fit or availability != "active" or role != "visible"
        )
        content = f"""<div class="wrap panel-section" data-offers-page>
          <section class="operations-bar" aria-label="Pobieranie ofert">
            <div><p class="eyebrow">Pobieranie ofert</p><strong data-scan-label>{escape(latest_note)}</strong>
              <p class="muted" data-scan-detail>{"Otwórz szczegóły ostatniego skanu." if latest else "Wybierz szybki lub pełny skan."}</p></div>
            <div class="operations-bar__actions">
              <button type="button" class="button--quiet" data-scan-trigger>Szczegóły</button>
              <button type="button" data-scan-trigger data-scan-mode="quick">Sprawdź nowe oferty</button>
            </div>
            <dialog class="scan-dialog" data-scan-dialog aria-labelledby="scan-dialog-title">
              <form method="dialog"><button class="dialog-close" aria-label="Zamknij">×</button></form>
              <p class="eyebrow">Nowy skan</p><h2 id="scan-dialog-title">Jak szeroko sprawdzić źródła?</h2>
              <p class="muted">Skan pobiera oferty; ocenę dopasowania uruchamiasz osobno.</p>
              <div class="dialog-actions"><button type="button" data-start-scan="quick">Szybki</button>
              <button type="button" class="button--quiet" data-start-scan="full">Pełny</button>
              <button type="button" class="button--quiet" data-cancel-scan hidden>Anuluj skan</button></div>
              <div class="scan-progress" data-scan-progress hidden></div>
            </dialog>
          </section>
          <section class="evaluation-bar" aria-label="Ocena dopasowania">
            <div><p class="eyebrow">Ocena dopasowania</p><strong>{pending_evaluations} ofert do oceny</strong>
              <p class="muted" data-evaluation-progress>Ocena jest niezależna od skanu i zachowuje historię wyników.</p></div>
            <div class="operations-bar__actions">{evaluation_action}<button type="button" class="button--quiet" data-cancel-evaluation hidden>Anuluj ocenę</button></div>
          </section>
          <form class="offer-search" method="get" aria-label="Szukaj i filtruj oferty">
            <label class="sr-only" for="offer-query">Stanowisko, firma lub technologia</label>
            <input id="offer-query" name="q" value="{escape(q)}"
              placeholder="Stanowisko, firma lub technologia…">
            <button type="submit">Szukaj</button>
            <details class="filter-panel" {"open" if filters_are_active else ""}>
              <summary>Więcej filtrów</summary>
              <div class="filter-grid">
                <label class="field">Firma<select name="company">
                  <option value="">Wszystkie firmy</option>{company_options}
                </select></label>
                <label class="field">Etap<select name="status">
                  <option value="">Każdy etap</option>{status_options}
                </select></label>
                <label class="field">Dostępność<select name="availability">
                  {availability_options}</select></label>
                <label class="field">Kierunek<select name="role">
                  <option value="visible" {"selected" if role == "visible" else ""}>AI i do sprawdzenia</option>
                  <option value="hidden" {"selected" if role == "hidden" else ""}>Ukryte role software</option>
                  <option value="all" {"selected" if role == "all" else ""}>Wszystkie kierunki</option>
                </select></label>
                <label class="field">Ocena modelu<select name="feedback">
                  <option value="">Wszystkie</option>
                  <option value="false_negative" {"selected" if feedback == "false_negative" else ""}>Zgłoszona błędna ocena</option>
                </select></label>
                <label class="field">Dopasowanie<select name="fit">
                  <option value="">Wszystkie</option>
                  <option value="ambitious" {"selected" if fit == "ambitious" else ""}>Ambitne — kierunek AI, luki w CV</option>
                </select></label>
                <button type="submit">Zastosuj filtry</button>
              </div>
            </details>
          </form>
          <div class="summary-strip" role="status">
            <p><strong>{len(offers)}</strong> ofert w tym widoku ·
              <strong>{monitoring["active_offers"]}</strong> aktywnych łącznie</p>
            <p>{escape(latest_note)} · <strong>{pending_evaluations}</strong> do ponownej oceny</p>
          </div>
          <div class="grid">{cards or "<div class='empty-state'><h3>Brak ofert</h3><p>Zmień filtry albo uruchom nowy skan.</p></div>"}</div></div>"""
        return layout(
            content,
            "Oferty · AI Job Scout",
            active="offers",
            page_title="Oferty",
            eyebrow="Znajdź role warte Twojej energii",
        )

    @app.get("/monitoring", response_class=HTMLResponse)
    def monitoring_page() -> HTMLResponse:
        monitoring = get_collection_monitoring(database_path)
        recent_events = list_offer_events(database_path, limit=50)
        latest = monitoring["latest_run"]
        last_run = get_collection_run(database_path, latest["run_id"]) if latest else None
        hidden = [
            item for item in (last_run or {}).get("rejections", []) if item["stage"] == "role"
        ]
        hidden_rows = (
            "".join(
                f"<tr><td>{escape(item['company'])}</td><td><a href='{escape(item['url'])}' target='_blank' rel='noreferrer'>{escape(item['title'])}</a></td><td>{escape('; '.join(item['reasons']))}</td></tr>"
                for item in hidden
            )
            or "<tr><td colspan='3' class='muted'>Brak ukrytych ofert software w ostatnim skanie.</td></tr>"
        )
        if latest:
            run_summary = f"""<section class="card"><div class="company">Ostatni przebieg</div>
              <h2>{escape(str(latest["mode"]))} · {escape(str(latest["status"]))}</h2>
              <p><strong>{latest["sources_ok"]}/{latest["sources_total"]}</strong> źródeł OK ·
              zapisano <strong>{latest["offers_saved"]}</strong> ofert · nowe wersje <strong>{latest["new_raw_versions"]}</strong> ·
              niedostępne <strong>{latest["offers_marked_unavailable"]}</strong></p>
              <p class="muted">{escape(str(latest["finished_at"])[:19].replace("T", " "))}</p></section>"""
        else:
            run_summary = "<p class='synthetic'>Brak danych. Uruchom <code>demo-v2-scan --mode sample</code>.</p>"
        rows = (
            "".join(
                f"""<tr><td>{escape(source["company"])}</td><td class="{"source-ok" if source["status"] == "ok" else "source-error"}">{escape(source["status"])}</td>
            <td>{source["discovered_count"]}</td><td>{source["selected_count"]}</td><td>{escape(str(source["error"] or "—"))}</td></tr>"""
                for source in monitoring["sources"]
            )
            or "<tr><td colspan='5' class='muted'>Brak przebiegu do pokazania.</td></tr>"
        )
        event_rows = (
            "".join(
                f"""<tr><td>{escape(item["created_at"][:16].replace("T", " "))}</td>
            <td>{escape(_event_label(item["event_type"]))}</td>
            <td><a href="/offers/{item["offer_id"]}">{escape(item["company"])} — {escape(item["title"])}</a></td>
            <td>{escape(", ".join(item["event"].get("changed_fields") or []) or "—")}</td></tr>"""
                for item in recent_events
            )
            or "<tr><td colspan='4' class='muted'>Brak zmian do pokazania.</td></tr>"
        )
        content = f"""<div class="wrap panel-section"><p class="dev-banner"><strong>Diagnostyka techniczna.</strong>
          Codzienny health pozostaje w Ustawieniach; tutaj widać szczegóły adapterów.</p>
          {run_summary}<section><h2>Źródła z ostatniego odświeżenia</h2><div class="table-wrap"><table><thead><tr><th>Firma</th><th>Status</th><th>Odkryte</th><th>Zapisane</th><th>Informacja</th></tr></thead><tbody>{rows}</tbody></table></div></section>
          <section><h2>Historia zmian ofert</h2><div class="table-wrap"><table><thead>
          <tr><th>Kiedy</th><th>Zdarzenie</th><th>Oferta</th><th>Zmienione pola</th></tr>
          </thead><tbody>{event_rows}</tbody></table></div></section>
          <section><h2>Ukryte oferty software</h2><p class="muted">Ostatni skan: {len(hidden)} ofert. Każdą można otworzyć u źródła.</p><div class="table-wrap"><table><thead><tr><th>Pracodawca</th><th>Oferta</th><th>Powód</th></tr></thead><tbody>{hidden_rows}</tbody></table></div></section>
          <section class="card"><div class="company">Zasada bezpieczeństwa</div><h2>„Niedostępna” oznacza potwierdzoną zmianę</h2><p class="muted">Status ustawiamy tylko po udanym, pełnym odkryciu ofert danego źródła. Błąd albo pusta odpowiedź nie usuwa historii i nie oznacza oferty jako znikniętej.</p></section></div>"""
        return layout(
            content,
            "Źródła · AI Job Scout",
            shell="developer",
            active="sources",
            page_title="Źródła i monitoring",
            eyebrow="Stan kolektorów",
        )

    @app.get("/profiles", response_class=HTMLResponse)
    def profiles_page() -> HTMLResponse:
        profiles = list_user_profiles(database_path)
        cards = "".join(_profile_card(profile) for profile in profiles) or (
            "<div class='empty-state'><h2>Jeszcze Cię nie znamy.</h2>"
            "<p>Dodaj CV, sprawdź odczyt i zatwierdź fakty używane do oceny ofert.</p>"
            "<a class='button' href='/profiles/new'>Zacznij od CV</a></div>"
        )
        content = f"""<div class="wrap panel-section">
          <section class="hero-grid"><article class="hero-card">
            <span class="eyebrow">Twój zawodowy kompas</span>
            <h2>Więcej niż CV.</h2>
            <p>Profil łączy doświadczenie, preferencje, warunki pracy i dowody.
            Dzięki temu Scout nie szuka tylko podobnych słów, lecz pomaga ocenić
            całą rolę uczciwie.</p>
            <p><a class="button" href="/profiles/new">Dodaj nowy profil</a></p>
          </article><aside class="hero-aside"><span class="eyebrow">Prywatność</span>
            <strong>CV zostaje na tym Macu</strong>
            <p class="muted">Najpierw widzisz i poprawiasz odczyt. Dopiero Twoje
            zatwierdzenie pozwala użyć danych do lokalnej oceny.</p>
            <a href="/settings">Prywatność i ustawienia →</a>
          </aside></section>
          <section><div class="profile-grid">{cards}</div></section></div>"""
        return layout(
            content,
            "Mój profil · AI Job Scout",
            active="profile",
            page_title="Profil",
            eyebrow="To, co Scout powinien o Tobie wiedzieć",
            page_actions='<a class="button" href="/profiles/new">Dodaj profil</a>',
        )

    @app.get("/profiles/new", response_class=HTMLResponse)
    def new_profile_page() -> HTMLResponse:
        return layout(
            _profile_form(),
            "Dodaj profil · AI Job Scout",
            active="profile",
            page_title="Dodaj profil i CV",
            eyebrow="Pierwszy krok",
        )

    @app.post("/profiles")
    async def create_profile(
        display_name: str = Form(""),
        target_roles: str = Form(""),
        location_rule: str = Form(""),
        transferable_skills: str = Form(""),
        work_preferences: str = Form(""),
        negative_criteria: str = Form(""),
        evidence: str = Form(""),
        cv_file: UploadFile = File(...),
    ):
        filename = safe_filename(cv_file.filename or "cv.pdf")
        display_name = display_name.strip() or Path(filename).stem.replace("_", " ")
        profile_id = _profile_id(display_name)
        try:
            content = await cv_file.read()
            stored_path = store_cv_bytes(
                database_path.parent / "profile-documents", profile_id, filename, content
            )
            extracted = extract_cv_pdf(stored_path)
            has_legacy_profile_answers = bool(
                _split_csv(target_roles) and location_rule.strip() and _split_lines(evidence)
            )
            if has_legacy_profile_answers:
                evidence_items = [
                    CandidateEvidence(
                        id=f"cv-fact-{index}",
                        statement=value,
                        source=f"CV: {filename}",
                    )
                    for index, value in enumerate(_split_lines(evidence), start=1)
                ]
                profile_payload = CandidateProfile(
                    profile_id=profile_id,
                    version=1,
                    target_roles=_split_csv(target_roles),
                    evidence=evidence_items,
                    transferable_skills=_split_lines(transferable_skills),
                    work_preferences=_split_lines(work_preferences),
                    negative_criteria=_split_lines(negative_criteria),
                    location_rule=location_rule.strip(),
                    approved_at=None,
                ).model_dump(mode="json")
            else:
                profile_payload = _cv_first_profile_draft(profile_id)
        except (CvExtractionError, ValueError) as exc:
            try:
                if "stored_path" in locals():
                    stored_path.unlink(missing_ok=True)
            finally:
                pass
            return layout(
                _profile_form(error=str(exc)),
                "Dodaj profil · AI Job Scout",
                active="profile",
                page_title="Dodaj profil i CV",
                eyebrow="Popraw dane formularza",
            )
        save_user_profile(
            database_path,
            profile_id=profile_id,
            display_name=display_name,
            profile_json=profile_payload,
            status="cv_review",
        )
        save_profile_document(
            database_path,
            document_id=f"cv-{uuid.uuid4().hex}",
            profile_id=profile_id,
            original_filename=filename,
            stored_path=stored_path,
            sha256=file_sha256(content),
            extraction_method=extracted.method,
            extracted_text=extracted.text,
            page_count=extracted.page_count,
            page_text=extracted.pages,
        )
        return RedirectResponse(f"/profiles/{profile_id}", status_code=303)

    @app.get("/profiles/{profile_id}", response_class=HTMLResponse)
    def profile_detail(profile_id: str) -> HTMLResponse:
        profile = get_user_profile(database_path, profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        payload = profile["profile"]
        document = profile.get("document")
        knowledge_base = get_latest_knowledge_base(database_path, profile_id)
        versions = list_profile_versions(database_path, profile_id)
        readiness = profile_readiness(database_path, profile_id)
        facts = "".join(f"<li>{escape(item['statement'])}</li>" for item in payload["evidence"])
        roles = "".join(
            f"<span class='tag'>{escape(role)}</span>" for role in payload["target_roles"]
        )
        imported_career_description = any(
            "career knowledge base" in str(item.get("source", "")).casefold()
            for item in payload.get("evidence", [])
        )
        doc_summary = (
            f"{escape(document['original_filename'])} · {document['page_count']} str. · "
            f"{document['extraction_method']} · {document['character_count']} znaków"
            if document
            else (
                "Profil zaimportowany z opisu kariery · CV możesz dodać później"
                if imported_career_description
                else "Profil utworzony bez dokumentu CV"
            )
        )
        preview_value = (document or {}).get("corrected_text") or (document or {}).get(
            "extracted_text", ""
        )
        preview = escape(preview_value)
        page_previews = "".join(
            f"<details><summary>Strona {index}</summary>"
            f'<pre class="cv-preview">{escape(page)}</pre></details>'
            for index, page in enumerate((document or {}).get("pages") or [], start=1)
        )
        status_heading = {
            "ready": "Gotowy do porównań",
            "cv_approved": "CV jest gotowe — poznajmy Twój kierunek",
            "cv_review": "Sprawdź odczyt CV",
            "draft": "Uzupełnij i potwierdź profil",
        }.get(profile["status"], "Profil wymaga uwagi")
        if document and document.get("review_status") != "approved":
            language = document.get("original_language", "mixed")
            language_options = "".join(
                f'<option value="{value}" {"selected" if value == language else ""}>'
                f"{label}</option>"
                for value, label in (
                    ("pl", "Polski"),
                    ("en", "Angielski"),
                    ("mixed", "Polski + angielski"),
                    ("unknown", "Nie wiem"),
                )
            )
            document_panel = f"""<section class="form-card"><div class="company">
              Wymagane zatwierdzenie</div><h2>Sprawdź odczyt CV przed użyciem w scoringu</h2>
              <p class="muted">Popraw błędy OCR i potwierdź język. Potem wybierzesz,
              doprecyzuj kierunek w profilu.</p>
              <form class="form-grid" method="post"
                action="/profiles/{escape(profile_id)}/documents/{escape(document["document_id"])}/review">
                <label class="field wide">Tekst CV po korekcie
                  <textarea name="corrected_text" required>{preview}</textarea></label>
                <label class="field">Język oryginału
                  <select name="original_language">{language_options}</select></label>
                <label class="field-check wide"><input type="checkbox" name="approve" required>
                  Potwierdzam, że tekst CV jest poprawny i może zostać użyty w lokalnym
                  ocenie ofert.</label>
                <div class="wide"><button type="submit">Zatwierdź odczyt CV</button></div>
              </form></section>"""
        elif document:
            document_panel = f"""<section class="form-card"><div class="company">
              Zatwierdzony odczyt CV</div><pre class="cv-preview">{preview}</pre>
              {page_previews}</section>"""
        else:
            kb_note = (
                f"Zaimportowano Career Knowledge Base v{knowledge_base['version']} jako "
                "lokalny szkic. Do scoringu trafia wyłącznie zatwierdzony zestaw faktów."
                if knowledge_base
                else "Dodaj CV, aby uzupełnić źródła profilu."
            )
            document_panel = f"""<section class="form-card">
              <div class="company">Źródło profilu</div>
              <h2>Profil utworzony z opisu kariery</h2>
              <p class="muted">{escape(kb_note)}</p>
              <a class="button button--quiet" href="/profiles/new">
                Dodaj osobny profil z CV</a></section>"""
        profile_progress_panel = ""
        facts_panel = (
            f'<section class="form-card"><div class="company">Fakty, które LLM może cytować</div><ul>{facts}</ul></section>'
            if facts
            else ""
        )
        approved_fact_rows = (
            "".join(
                f"<li><strong>{escape(item['category'])}</strong>: {escape(_profile_fact_value(item['value']))}"
                f"<br><small>{escape(item['source_type'])} · {escape(item['source_quote'])}</small></li>"
                for item in readiness["approved_facts"]
            )
            or "<li>Brak zatwierdzonych faktów.</li>"
        )
        cv_assets_panel = ""
        draft_fact_rows = (
            "".join(
                f"""<article class="card"><strong>{escape(item["category"])}</strong>: {escape(_profile_fact_value(item["value"]))}
            <p class="muted">Źródło: {escape(item["source_quote"])}</p>
            <form method="post" action="/profiles/{escape(profile_id)}/facts/{escape(item["fact_id"])}/review">
              <button name="decision" value="approve" type="submit">Zatwierdź</button>
              <button class="button--quiet" name="decision" value="reject" type="submit">Odrzuć</button>
            </form></article>"""
                for item in readiness["draft_facts"]
            )
            or "<p class='muted'>Nie ma faktów oczekujących na decyzję.</p>"
        )
        missing_rows = "".join(f"<li>{escape(item)}</li>" for item in readiness["missing"])
        conflict_rows = (
            "".join(
                f"""<article class="card"><strong>Sprzeczność: {escape(item["category"])}</strong>
            <p>{escape(_profile_fact_value(item["value_a"]))} ↔ {escape(_profile_fact_value(item["value_b"]))}</p>
            <form method="post" action="/profiles/{escape(profile_id)}/fact-conflicts/{escape(item["conflict_id"])}/resolve">
              <label class="field">Fakt, który pozostaje aktywny<select name="winner_fact_id" required>
                <option value="{escape(item["fact_id_a"])}">{escape(_profile_fact_value(item["value_a"]))}</option>
                <option value="{escape(item["fact_id_b"])}">{escape(_profile_fact_value(item["value_b"]))}</option>
              </select></label>
              <label class="field">Jak rozstrzygasz?<input name="resolution_note" required></label>
              <button type="submit">Zapisz rozstrzygnięcie</button>
            </form></article>"""
                for item in readiness["conflicts"]
            )
            or "<p class='muted'>Brak nierozstrzygniętych sprzeczności.</p>"
        )
        readiness_panel = f"""<section class="form-card"><div class="company">Gotowość profilu</div>
          <h2>{"Profil gotowy do scoringu" if readiness["ready_for_scoring"] else "Brakuje kilku jawnych decyzji"}</h2>
          <p class="muted">Do scoringu trafiają wyłącznie zatwierdzone fakty z cytatem źródłowym.</p>
          <h3>Co jeszcze ustalić</h3><ul>{missing_rows or "<li>Wszystkie wymagane obszary są opisane.</li>"}</ul>
          <h3>Zatwierdzone fakty używane przez scoring</h3><ul>{approved_fact_rows}</ul>
          <h3>Sprzeczności do rozstrzygnięcia</h3>{conflict_rows}
          <h3>Dodaj fakt do weryfikacji</h3>
          <form class="form-grid" method="post" action="/profiles/{escape(profile_id)}/facts">
            <label class="field">Obszar<select name="category"><option value="target_role">Docelowa rola</option><option value="work_location">Lokalizacja</option><option value="work_model">Model pracy</option><option value="contract">Umowa</option><option value="language">Język</option><option value="travel">Podróże</option><option value="experience">Doświadczenie</option><option value="achievement">Osiągnięcie</option><option value="constraint">Ograniczenie</option><option value="preference">Preferencja</option></select></label>
            <label class="field">Wartość<input name="value" required placeholder="np. Zdalnie z Polski"></label>
            <label class="field">Źródło<select name="source_type"><option value="user_message">Twoja odpowiedź</option><option value="cv">CV</option><option value="career_base">Career Base</option></select></label>
            <label class="field">Identyfikator źródła<input name="source_ref" required placeholder="np. message-... albo CV s. 1"></label>
            <label class="field wide">Dokładny cytat / odwołanie<textarea name="source_quote" required></textarea></label>
            <label class="field">Priorytet<select name="priority"><option value="">Nie dotyczy</option><option value="preference">Preferencja</option><option value="important">Ważne</option><option value="required">Wymagane</option></select></label>
            <label class="field-check"><input type="checkbox" name="usable_for_cv"> Można użyć w CV</label>
            <label class="field-check"><input type="checkbox" name="usable_for_scoring" checked> Można użyć w scoringu po zatwierdzeniu</label>
            <div class="wide"><button type="submit">Dodaj jako draft</button></div>
          </form><h3>Drafty do decyzji</h3>{draft_fact_rows}</section>"""
        content = f"""<div class="wrap panel-section"><section class="hero-grid"><article class="hero-card"><span class="eyebrow">Status profilu</span><h2>{status_heading}</h2><div class="tag-row">{roles}</div><p>{escape(payload.get("location_rule") or "Kierunek ustalimy podczas rozmowy.")}</p><p>Wersja {profile["current_version"]} · {len(versions)} zapisanych wersji</p></article><aside class="hero-aside"><span class="eyebrow">CV lokalnie</span><strong>{doc_summary}</strong><p class="muted">Odczyt zostanie użyty jako kontekst dopiero po Twoim zatwierdzeniu.</p></aside></section>
          {cv_assets_panel}{readiness_panel}{profile_progress_panel}{facts_panel}{document_panel}</div>"""
        return layout(
            content,
            f"Profil {profile['display_name']} · AI Job Scout",
            active="profile",
            page_title=profile["display_name"],
            eyebrow="Profil lokalny",
            page_actions='<a class="button button--quiet" href="/profiles/new">Dodaj kolejny</a>',
        )

    @app.post("/profiles/{profile_id}/facts")
    def create_profile_fact(
        profile_id: str,
        category: str = Form(...),
        value: str = Form(...),
        source_type: str = Form(...),
        source_ref: str = Form(...),
        source_quote: str = Form(...),
        priority: str = Form(""),
        usable_for_scoring: str | None = Form(None),
        usable_for_cv: str | None = Form(None),
    ) -> RedirectResponse:
        try:
            save_profile_fact(
                database_path,
                profile_id=profile_id,
                category=category,
                value={"text": value.strip()},
                source_type=source_type,
                source_ref=source_ref,
                source_quote=source_quote,
                priority=priority or None,
                usable_for_scoring=bool(usable_for_scoring),
                usable_for_cv=bool(usable_for_cv),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/profiles/{profile_id}", status_code=303)

    @app.post("/profiles/{profile_id}/facts/{fact_id}/review")
    def review_fact(profile_id: str, fact_id: str, decision: str = Form(...)) -> RedirectResponse:
        try:
            review_profile_fact(database_path, fact_id=fact_id, approve=decision == "approve")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/profiles/{profile_id}", status_code=303)

    @app.post("/profiles/{profile_id}/fact-conflicts/{conflict_id}/resolve")
    def resolve_fact_conflict(
        profile_id: str,
        conflict_id: str,
        winner_fact_id: str = Form(...),
        resolution_note: str = Form(...),
    ) -> RedirectResponse:
        try:
            resolve_profile_fact_conflict(
                database_path,
                conflict_id=conflict_id,
                winner_fact_id=winner_fact_id,
                resolution_note=resolution_note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/profiles/{profile_id}", status_code=303)

    @app.post("/profiles/{profile_id}/documents/{document_id}/review")
    def review_profile_document(
        profile_id: str,
        document_id: str,
        corrected_text: str = Form(...),
        original_language: str = Form(...),
        approve: str | None = Form(None),
    ) -> RedirectResponse:
        if not approve:
            raise HTTPException(status_code=400, detail="Explicit approval is required")
        try:
            approve_profile_document(
                database_path,
                profile_id=profile_id,
                document_id=document_id,
                corrected_text=corrected_text,
                original_language=original_language,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/profiles/{profile_id}", status_code=303)

    @app.get("/lab", response_class=HTMLResponse)
    def lab_page() -> HTMLResponse:
        settings = Settings.from_env()
        profiles = list_user_profiles(database_path)
        models = list_lab_model_profiles(database_path)
        experiments = list_lab_experiments(database_path)
        runtime_checks = [
            ("llama-server", bool(shutil.which(settings.llama_server_executable))),
            ("Qwen Scout GGUF", settings.scout_llm_model_path.is_file()),
            (
                "model oceny ofert",
                settings.evaluation_model == "gemini-3.8-flash"
                or settings.evaluation_model_path.is_file(),
            ),
            ("MLflow local path", settings.mlflow_tracking_uri.startswith("file:")),
        ]
        runtime_html = "".join(
            f"<li><span>{escape(label)}</span><span class={'runtime-good' if ready else 'runtime-warn'}>{'gotowe' if ready else 'wymaga konfiguracji'}</span></li>"
            for label, ready in runtime_checks
        )
        model_cards = (
            "".join(_lab_model_card(model) for model in models)
            or "<p class='muted'>Dodaj pierwszy profil modelu.</p>"
        )
        experiment_rows = (
            "".join(_experiment_row(item) for item in experiments)
            or "<tr><td colspan='5' class='muted'>Brak zapisanych eksperymentów.</td></tr>"
        )
        profile_options = "".join(
            f"<option value='{escape(profile['profile_id'])}'>{escape(profile['display_name'])}</option>"
            for profile in profiles
            if profile["status"] == "ready"
        )
        model_options = "".join(
            f"<option value='{escape(model['model_profile_id'])}'>{escape(model['display_name'])}</option>"
            for model in models
        )
        content = f"""<div class="wrap panel-section"><section class="hero-grid"><article class="hero-card"><span class="eyebrow"><span class="motion-dot"></span>świadome testy lokalne</span><h2>Konfiguracja jest częścią wyniku.</h2><p>Zapisuj model, rolę, dataset, prompt, seed, temperaturę i context. Porównuj wyniki dopiero na tym samym snapshotcie danych.</p><p><a class="button" href="/model-lab/demo">Otwórz testy Model Lab</a></p></article><aside class="hero-aside"><span class="eyebrow">Środowisko</span><ul class="runtime-list">{runtime_html}</ul></aside></section>
          <section><h2>Profile modeli</h2><div class="lab-grid">{model_cards}</div></section>
          <section class="form-card"><h2>Dodaj lokalny model</h2><form class="form-grid" method="post" action="/lab/models"><label class="field">Nazwa<input name="display_name" required placeholder="Qwen Scout Q4"></label><label class="field">Rola<select name="role"><option value="scout">Scout / ekstrakcja</option><option value="evaluator">Evaluator</option><option value="judge">Judge</option></select></label><label class="field wide">Ścieżka GGUF<input name="model_path" required placeholder="models/model.gguf"></label><label class="field">Context<input name="context_size" type="number" min="1024" value="4096"></label><label class="field">Temperatura<input name="temperature" type="number" step="0.05" min="0" max="2" value="0"></label><label class="field">Seed<input name="seed" type="number" value="42"></label><div class="wide"><button type="submit">Zapisz profil modelu</button></div></form></section>
          <section class="form-card"><h2>Zaplanuj porównywalny eksperyment</h2><form class="form-grid" method="post" action="/lab/experiments"><label class="field">Nazwa eksperymentu<input name="display_name" required placeholder="Qwen extraction baseline"></label><label class="field">Zadanie<select name="task_kind"><option value="extraction">Ekstrakcja ofert</option><option value="evaluation">Ocena dopasowania</option><option value="judge">LLM-as-a-Judge</option></select></label><label class="field">Dataset<select name="dataset_key"><option value="frozen-v1">Frozen 10</option><option value="live-sample">Demo v2 live sample</option><option value="golden-v1">Golden dataset (po dodaniu)</option></select></label><label class="field">Gotowy profil<select name="profile_id"><option value="">Brak - test techniczny</option>{profile_options}</select></label><label class="field">Model<select name="model_profile_id"><option value="">Najpierw dodaj profil modelu</option>{model_options}</select></label><label class="field">Prompt version<input name="prompt_version" value="v1"></label><label class="field">Seed<input name="seed" type="number" value="42"></label><label class="field">Temperatura<input name="temperature" type="number" step="0.05" min="0" max="2" value="0"></label><label class="field">Max tokens<input name="max_tokens" type="number" min="1" placeholder="bez limitu"></label><div class="wide"><button type="submit">Zapisz plan eksperymentu</button></div></form></section>
          <section><h2>Historia konfiguracji</h2><div class="table-wrap"><table><thead><tr><th>Eksperyment</th><th>Zadanie</th><th>Dataset</th><th>Model</th><th>Status</th></tr></thead><tbody>{experiment_rows}</tbody></table></div></section></div>"""
        return layout(
            content,
            "Modele · AI Job Scout",
            shell="developer",
            active="models",
            page_title="Modele i eksperymenty",
            eyebrow="Reprodukowalne konfiguracje",
        )

    @app.post("/lab/models")
    def create_lab_model(
        display_name: str = Form(...),
        role: str = Form(...),
        model_path: str = Form(...),
        context_size: int = Form(...),
        temperature: float = Form(...),
        seed: int = Form(...),
    ) -> RedirectResponse:
        if role not in {"scout", "evaluator", "judge"} or not 0 <= temperature <= 2:
            raise HTTPException(status_code=400, detail="Invalid model configuration")
        save_lab_model_profile(
            database_path,
            model_profile_id=f"model-{uuid.uuid4().hex[:12]}",
            display_name=display_name.strip(),
            role=role,
            model_path=model_path.strip(),
            config_json={"context_size": context_size, "temperature": temperature, "seed": seed},
        )
        return RedirectResponse("/lab", status_code=303)

    @app.post("/lab/experiments")
    def create_lab_experiment(
        display_name: str = Form(...),
        dataset_key: str = Form(...),
        task_kind: str = Form(...),
        profile_id: str = Form(""),
        model_profile_id: str = Form(""),
        prompt_version: str = Form(...),
        seed: int = Form(...),
        temperature: float = Form(...),
        max_tokens: str = Form(""),
    ) -> RedirectResponse:
        if task_kind not in {"extraction", "evaluation", "judge"} or not 0 <= temperature <= 2:
            raise HTTPException(status_code=400, detail="Invalid experiment configuration")
        tokens = int(max_tokens) if max_tokens.strip() else None
        if tokens is not None and tokens < 1:
            raise HTTPException(status_code=400, detail="max_tokens must be positive")
        save_lab_experiment(
            database_path,
            experiment_id=f"experiment-{uuid.uuid4().hex[:12]}",
            display_name=display_name.strip(),
            profile_id=profile_id or None,
            dataset_key=dataset_key,
            task_kind=task_kind,
            model_profile_id=model_profile_id or None,
            config_json={
                "prompt_version": prompt_version.strip(),
                "seed": seed,
                "temperature": temperature,
                "max_tokens": tokens,
            },
        )
        return RedirectResponse("/lab", status_code=303)

    @app.get("/model-lab/demo", response_class=HTMLResponse)
    def model_lab_demo() -> HTMLResponse:
        run = get_latest_evaluation_run(database_path)
        if not run:
            return layout(
                '<div class="wrap detail"><p><a href="/">← Oferty</a></p>'
                "<p>Brak zapisanych przebiegów.</p>"
                f"{_render_demo_start_form(root, database_path)}</div>",
                "Model Lab / Demo",
                shell="developer",
                active="playground",
                page_title="Model Lab / Demo",
                eyebrow="Kontrolowany playground",
            )
        items = list_evaluation_run_items(database_path, run["run_id"])
        cards = "".join(_evaluation_card(item) for item in items)
        profile = run.get("profile") or {}
        synthetic = profile.get("profile_id") == "synthetic-flow-test"
        banner = (
            '<p class="synthetic"><strong>Profil syntetyczny.</strong> Wyniki służą wyłącznie '
            "do testu technicznego i nie są rekomendacjami zawodowymi.</p>"
            if synthetic
            else ""
        )
        model = escape(str((run.get("models") or {}).get("model", "unknown")))
        profile_label = escape(str(profile.get("profile_id", "unknown")))
        profile_version = escape(str(profile.get("version", "?")))
        mode_label = escape(RUN_MODE_LABELS.get(run.get("run_type", ""), "unknown"))

        is_active = run["status"] == "running" if run else False

        if is_active:
            run_action = (
                '<div style="display:flex; gap:10px;">'
                "<button disabled>Demo w toku…</button>"
                '<form method="post" action="/model-lab/demo/cancel" style="margin:0;">'
                '<button type="submit" style="background:#dc2828; '
                'border-color:#dc2828;">Anuluj</button>'
                "</form></div>"
                '<span class="muted">Strona odświeża się co 5 sekund.</span>'
            )
            progress = (
                f"<p><strong>{run.get('completed_items', 0)}/"
                f"{run.get('total_items', 0)}</strong> ukończonych · "
                f"etap: <strong>{escape(run.get('current_stage') or 'idle')}</strong></p>"
            )
        else:
            can_resume = run["status"] in {"interrupted", "cancelled", "partial"}
            failed_count = sum(item["status"] == "failed" for item in items)
            resume_action = (
                '<form method="post" action="/model-lab/demo/resume">'
                '<button type="submit">Wznów pozostałe</button></form>'
                if can_resume
                else ""
            )
            retry_action = (
                f'<form method="post" action="/model-lab/runs/{escape(run["run_id"])}'
                '/retry-failed"><button type="submit">'
                f"Ponów {failed_count} błędów</button></form>"
                if failed_count
                else ""
            )
            run_action = (
                f"{resume_action}{retry_action}{_render_demo_start_form(root, database_path)}"
            )
            progress = ""
        error = (
            f'<p class="warning">Ostatni błąd: {escape(run.get("error") or "")}</p>'
            if run and run.get("error")
            else ""
        )
        content = f"""<div class="wrap detail"><p><a href="/">← Oferty</a></p>
          {banner}
          <div class="run-bar">{run_action}</div>{progress}{error}
          <section class="card"><div class="company">{escape(run["status"])}</div>
            <h2>Przebieg {escape(run["run_id"])}</h2>
            <p><strong>{run["completed_items"]}/{run["total_items"]}</strong> ofert · {model}</p>
            <p class="muted">Tryb: {mode_label} · Profil: {profile_label} v{profile_version} ·
              Self-review: same model</p>
          </section>
          <section><h2>Wyniki</h2><div class="grid">{cards}</div></section>
        </div>"""
        return layout(
            content,
            "Model Lab / Demo",
            auto_refresh=is_active,
            shell="developer",
            active="playground",
            page_title="Model Lab / Demo",
            eyebrow="Kontrolowany playground",
        )

    @app.get("/model-lab/runs", response_class=HTMLResponse)
    def list_runs(status: str = Query(default="")) -> HTMLResponse:
        runs = list_pipeline_runs(database_path, status=status)

        rows: list[str] = []
        for run in runs:
            started = run["started_at"][:19].replace("T", " ")
            status = escape(run["status"])
            run_id = escape(run["run_id"])
            mode_key = run.get("run_type", "unknown")
            mode = escape(RUN_MODE_LABELS.get(mode_key, mode_key))
            model = escape(str((run.get("models") or {}).get("model", "unknown")))

            profile = run.get("profile") or {}
            profile_label = escape(str(profile.get("profile_id", "unknown")))

            progress = f"{run['completed_items']}/{run['total_items']}"
            errors = int(run.get("failed_items") or 0)
            parent = run.get("parent_run_id")
            retry_of = (
                f'<a href="/model-lab/runs/{escape(parent)}">retry: {escape(parent)}</a>'
                if parent
                else "—"
            )

            rows.append(f"""<tr>
              <td>{started}</td>
              <td><a href="/model-lab/runs/{run_id}">{run_id}</a></td>
              <td>{status}</td>
              <td>{mode}</td>
              <td>{model}</td>
              <td>{profile_label}</td>
              <td>{progress}</td>
              <td>{errors}</td>
              <td>{retry_of}</td>
            </tr>""")

        status_options = "".join(
            f'<option value="{value}" {"selected" if value == status else ""}>{label}</option>'
            for value, label in (
                ("", "Wszystkie statusy"),
                ("completed", "Ukończone"),
                ("partial", "Częściowe"),
                ("failed", "Błędy"),
                ("interrupted", "Przerwane"),
                ("cancelled", "Anulowane"),
                ("running", "W toku"),
            )
        )
        body = "".join(rows) or (
            '<tr><td colspan="9" class="muted">Brak runów dla wybranego filtra.</td></tr>'
        )
        content = f"""<div class="wrap detail">
          <p><a href="/">← Oferty</a></p>
          <form class="filters" method="get">
            <select name="status">{status_options}</select>
            <button type="submit">Filtruj</button>
          </form>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Data</th>
                  <th>ID</th>
                  <th>Status</th>
                  <th>Tryb</th>
                  <th>Model</th>
                  <th>Profil</th>
                  <th>Postęp</th>
                  <th>Błędy</th>
                  <th>Ponowienie</th>
                </tr>
              </thead>
              <tbody>{body}</tbody>
            </table>
          </div>
        </div>"""
        return layout(
            content,
            "Historia testów",
            shell="developer",
            active="runs",
            page_title="Historia testów",
            eyebrow="Odtwarzalny ledger",
        )

    @app.get("/model-lab/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str) -> HTMLResponse:
        run = get_pipeline_run(database_path, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        items = list_evaluation_run_items(database_path, run["run_id"])
        cards = "".join(_evaluation_card(item) for item in items)
        profile = run.get("profile") or {}
        synthetic = profile.get("profile_id") == "synthetic-flow-test"
        banner = (
            '<p class="synthetic"><strong>Profil syntetyczny.</strong> Wyniki służą wyłącznie '
            "do testu technicznego i nie są rekomendacjami zawodowymi.</p>"
            if synthetic
            else ""
        )
        model = escape(str((run.get("models") or {}).get("model", "unknown")))
        profile_label = escape(str(profile.get("profile_id", "unknown")))
        profile_version = escape(str(profile.get("version", "?")))
        mode_label = escape(RUN_MODE_LABELS.get(run.get("run_type", ""), "unknown"))

        is_active = run["status"] == "running"

        if is_active:
            run_action = (
                '<div style="display:flex; gap:10px;">'
                "<button disabled>W toku…</button>"
                '<form method="post" action="/model-lab/demo/cancel" style="margin:0;">'
                '<button type="submit" style="background:#dc2828; '
                'border-color:#dc2828;">Anuluj</button>'
                "</form></div>"
                '<span class="muted">Strona odświeża się co 5 sekund.</span>'
            )
            progress = (
                f"<p><strong>{run.get('completed_items', 0)}/"
                f"{run.get('total_items', 0)}</strong> ukończonych · "
                f"etap: <strong>{escape(run.get('current_stage') or 'idle')}</strong></p>"
            )
        else:
            can_resume = run["status"] in {"interrupted", "cancelled", "partial"}
            failed_count = sum(item["status"] == "failed" for item in items)
            resume_action = (
                '<a href="/model-lab/demo"><button>Przejdź do Laboratorium by wznowić</button></a>'
                if can_resume
                else ""
            )
            retry_action = (
                f'<form method="post" action="/model-lab/runs/{escape(run["run_id"])}'
                '/retry-failed"><button type="submit">'
                f"Ponów {failed_count} błędów</button></form>"
                if failed_count
                else ""
            )
            run_action = f"{resume_action}{retry_action}"
            progress = ""

        error = (
            f'<p class="warning">Ostatni błąd: {escape(run.get("error") or "")}</p>'
            if run.get("error")
            else ""
        )
        content = f"""<div class="wrap detail">
          <p><a href="/model-lab/runs">← Historia testów</a></p>
          {banner}
          <div class="run-bar">{run_action}</div>{progress}{error}
          <section class="card"><div class="company">{escape(run["status"])}</div>
            <h2>Przebieg {escape(run["run_id"])}</h2>
            <p><strong>{run["completed_items"]}/{run["total_items"]}</strong> ofert · {model}</p>
            <p class="muted">Tryb: {mode_label} · Profil: {profile_label} v{profile_version} ·
              Self-review: same model</p>
            <p class="muted">Rozpoczęto: {run["started_at"][:19].replace("T", " ")}</p>
          </section>
          <section><h2>Wyniki</h2><div class="grid">{cards}</div></section>
        </div>"""
        return layout(
            content,
            f"Run {run['run_id']}",
            auto_refresh=is_active,
            shell="developer",
            active="runs",
            page_title=f"Szczegóły: {run['run_id']}",
            eyebrow="Przebieg modelu",
        )

    @app.post("/model-lab/demo/resume")
    async def resume_model_lab_demo() -> RedirectResponse:
        run = get_latest_evaluation_run(database_path)
        if not run or run["status"] not in {"interrupted", "cancelled", "partial"}:
            raise HTTPException(status_code=409, detail="No resumable demo run")
        if controller.active:
            raise HTTPException(status_code=409, detail="Demo run is already active")
        resume_run(database_path, run["run_id"])
        if not controller.start(run["run_id"]):
            cancel_run(database_path, run["run_id"])
            raise HTTPException(status_code=409, detail="Demo task is already active")
        return RedirectResponse("/model-lab/demo", status_code=303)

    @app.post("/model-lab/demo/start")
    async def start_model_lab_demo(
        mode: str | None = Form(None),
        offer_key: str | None = Form(None),
        profile_id: str | None = Form(None),
    ) -> RedirectResponse:
        run = get_latest_evaluation_run(database_path)
        if controller.active or (run and run["status"] == "running"):
            raise HTTPException(status_code=409, detail="Demo run is already active")
        try:
            # Default to frozen_evaluation if not provided (e.g. resumption)
            if profile_id:
                run_id = _prepare_dummy_demo_run(
                    root, database_path, mode or "frozen_evaluation", offer_key, profile_id
                )
            else:
                run_id = prepare_run(mode or "frozen_evaluation", offer_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not controller.start(run_id):
            cancel_run(database_path, run_id)
            raise HTTPException(status_code=409, detail="Demo task is already active")
        return RedirectResponse("/model-lab/demo", status_code=303)

    @app.post("/model-lab/demo/cancel")
    async def cancel_model_lab_demo() -> RedirectResponse:
        run = get_latest_evaluation_run(database_path)
        if run and run["status"] == "running":
            cancel_run(database_path, run["run_id"])
        controller.cancel()
        return RedirectResponse("/model-lab/demo", status_code=303)

    @app.post("/model-lab/runs/{run_id}/retry-failed")
    async def retry_failed_run(run_id: str) -> RedirectResponse:
        if controller.active:
            raise HTTPException(status_code=409, detail="Demo run is already active")
        try:
            retry_run_id = retry_failed_evaluation_run(database_path, run_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not controller.start(retry_run_id):
            cancel_run(database_path, retry_run_id)
            raise HTTPException(status_code=409, detail="Demo task is already active")
        return RedirectResponse(f"/model-lab/runs/{retry_run_id}", status_code=303)

    @app.get("/offers/{offer_id}", response_class=HTMLResponse)
    def detail(offer_id: int) -> HTMLResponse:
        item = get_offer(database_path, offer_id)
        if not item:
            raise HTTPException(status_code=404, detail="Offer not found")
        item["translation"] = get_latest_offer_translation(database_path, offer_id)
        item["versions"] = list_offer_versions(database_path, offer_id)
        item["section_evidence"] = list_offer_section_evidence(
            database_path, offer_id, item["current_content_sha256"]
        )
        ready_profiles = [
            profile
            for profile in list_user_profiles(database_path)
            if is_profile_ready_for_scoring(database_path, profile["profile_id"])
        ]
        item["evaluation_profile_id"] = (
            ready_profiles[0]["profile_id"] if len(ready_profiles) == 1 else None
        )
        if item["evaluation_profile_id"]:
            active_profile = get_user_profile(database_path, item["evaluation_profile_id"])[
                "profile"
            ]
            item["needs_evaluation"] = _offer_needs_evaluation(item, active_profile)
            if item["needs_evaluation"]:
                item["assessment"] = None
        item["evaluation_feedback"] = (
            get_current_evaluation_feedback(database_path, offer_id)
            if item.get("assessment")
            else None
        )
        return layout(
            _offer_detail(item),
            item["title"],
            active="offers",
            page_title=item["title"],
            eyebrow=item["company"],
        )

    @app.get("/application-packages/{package_id}", response_class=HTMLResponse)
    def application_package_page(package_id: str) -> HTMLResponse:
        package = get_application_package(database_path, package_id)
        if not package:
            raise HTTPException(status_code=404, detail="Package not found")
        offer = get_offer(database_path, int(package["offer_id"]))
        return layout(
            f"""<div class="wrap panel-section"><article class="form-card">
            <p>PDF: {package["page_count"]} strony A4</p>
            <div class="actions"><a class="button" href="/application-packages/{escape(package_id)}/cv.pdf">Pobierz CV PDF</a>
            <a class="button button--quiet" href="/application-packages/{escape(package_id)}/note">Pobierz notatkę</a>
            <a class="button button--quiet" href="/application-packages/{escape(package_id)}/open-offer" target="_blank">Otwórz formularz oferty</a></div>
            <form method="post" action="/application-packages/{escape(package_id)}/applied"><button type="submit">Oznacz jako wysłane</button></form>
            </article></div>""",
            "Pakiet aplikacyjny · AI Job Scout",
            active="applications",
            page_title=f"{offer['company']} — {offer['title']}",
            eyebrow="Gotowe do ręcznego wysłania",
        )

    @app.get("/application-packages/{package_id}/cv.pdf")
    def download_application_pdf(package_id: str) -> FileResponse:
        package = get_application_package(database_path, package_id)
        if not package:
            raise HTTPException(status_code=404, detail="Package not found")
        record_package_event(database_path, package_id=package_id, event_type="pdf_downloaded")
        return FileResponse(package["pdf_path"], filename=Path(package["pdf_path"]).name)

    @app.get("/application-packages/{package_id}/note")
    def download_application_note(package_id: str) -> FileResponse:
        package = get_application_package(database_path, package_id)
        if not package:
            raise HTTPException(status_code=404, detail="Package not found")
        return FileResponse(package["note_path"], filename=Path(package["note_path"]).name)

    @app.get("/application-packages/{package_id}/open-offer")
    def open_application_offer(package_id: str) -> RedirectResponse:
        package = get_application_package(database_path, package_id)
        if not package:
            raise HTTPException(status_code=404, detail="Package not found")
        offer = get_offer(database_path, int(package["offer_id"]))
        record_package_event(database_path, package_id=package_id, event_type="offer_opened")
        return RedirectResponse(offer["job_url"], status_code=303)

    @app.post("/application-packages/{package_id}/applied")
    def mark_application_applied(package_id: str) -> RedirectResponse:
        try:
            record_package_event(database_path, package_id=package_id, event_type="applied")
        except CvTailoringError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse("/applications", status_code=303)

    @app.post("/offers/{offer_id}/status/{status}")
    def set_status(offer_id: int, status: ApplicationStatus) -> RedirectResponse:
        if not update_application_status(database_path, offer_id, status.value):
            raise HTTPException(status_code=404, detail="Offer not found")
        return RedirectResponse(f"/offers/{offer_id}", status_code=303)

    @app.post("/offers/{offer_id}/evaluation-feedback/false-negative")
    def report_false_negative(
        offer_id: int,
        evaluation_input_sha256: str = Form(...),
        note: str = Form(default=""),
    ) -> RedirectResponse:
        try:
            saved = set_false_negative_feedback(
                database_path, offer_id, evaluation_input_sha256, note
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not saved:
            raise HTTPException(status_code=409, detail="Ocena zmieniła się lub nie jest negatywna")
        return RedirectResponse(f"/offers/{offer_id}", status_code=303)

    @app.post("/offers/{offer_id}/evaluation-feedback/clear")
    def clear_false_negative(
        offer_id: int, evaluation_input_sha256: str = Form(...)
    ) -> RedirectResponse:
        if not clear_false_negative_feedback(database_path, offer_id, evaluation_input_sha256):
            raise HTTPException(status_code=404, detail="Nie znaleziono zgłoszenia")
        return RedirectResponse(f"/offers/{offer_id}", status_code=303)

    return app


def _prepare_dummy_demo_run(
    project_root: Path,
    database_path: Path,
    mode: str,
    offer_key: str | None,
    profile_id: str | None = None,
) -> str:
    from .demo_data import load_candidate_profile, load_frozen_demo_offers
    from .prompts import EVALUATOR_PROMPT_VERSION, JUDGE_PROMPT_VERSION
    from .settings import Settings

    if mode not in RUN_MODE_LABELS:
        raise ValueError("Invalid mode selected")

    settings = Settings.from_env()
    if profile_id:
        stored_profile = get_user_profile(database_path, profile_id)
        if not stored_profile or not is_profile_ready_for_scoring(database_path, profile_id):
            raise ValueError("Selected profile is not ready for local LLM testing")
        profile = CandidateProfile.model_validate(stored_profile["profile"])
    else:
        profile = load_candidate_profile(project_root / "config/demo/dummy-profile-v1.json")

    if mode == "live_single_offer":
        if not offer_key or not offer_key.startswith("live:"):
            raise ValueError("Live offer id required")
        try:
            offer_id = int(offer_key.removeprefix("live:"))
        except ValueError as exc:
            raise ValueError("Invalid live offer id") from exc
        stored_offer = get_offer(database_path, offer_id)
        if not stored_offer:
            raise ValueError("Live offer not found")
        from .ats_scrapers import CleanJob

        offers = [CleanJob.model_validate(stored_offer["offer"])]
    else:
        offers = load_frozen_demo_offers(
            project_root / "config/demo/frozen-offers-v1.json", project_root
        )

    if mode == "single_offer_eval":
        if not offer_key:
            raise ValueError("Offer key required for single offer eval")
        offers = [offer for offer in offers if f"{offer.company}|{offer.title}" == offer_key]
        if not offers:
            raise ValueError("Offer not found")

    started_at = datetime.now(UTC)
    run_id = (
        "demo-flow-"
        + hashlib.sha256(f"{started_at.isoformat()}:{profile.profile_id}".encode()).hexdigest()[:16]
    )

    with connect(database_path) as connection:
        offer_ids = {}
        for offer in offers:
            offer_id, _ = persist_clean_offer(
                connection, offer, source_url=str(offer.url), seen_at=started_at
            )
            offer_ids[(offer.company, offer.title)] = offer_id

    run = PipelineRun(
        run_id=run_id,
        source_id=offers[0].source_id if mode == "live_single_offer" else "frozen-demo",
        run_type=mode,
        started_at=started_at,
        status=EvaluationRunStatus.RUNNING,
        total_items=len(offers),
        current_stage="starting",
        profile_id=profile.profile_id,
        profile_version=profile.version,
        prompt_versions={
            "evaluator": EVALUATOR_PROMPT_VERSION,
            "judge": JUDGE_PROMPT_VERSION,
        },
        model_configurations={"model": settings.local_llm_model},
    )

    items = []
    for offer in offers:
        offer_id = offer_ids[(offer.company, offer.title)]
        snapshot = {
            "company": offer.company,
            "title": offer.title,
            "url": str(offer.url),
            "analysis_text": offer.analysis_text,
            "supplemental_info": offer.supplemental_info.model_dump(mode="json"),
            "raw_sha256": offer.raw_sha256,
        }
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        items.append(
            EvaluationRunItem(
                item_id=f"{run_id}:{offer_id}",
                run_id=run_id,
                offer_id=offer_id,
                input_sha256=hashlib.sha256(snapshot_json.encode()).hexdigest(),
                input_snapshot=snapshot,
            )
        )

    create_evaluation_run(database_path, run, profile, items)
    return run_id


def _prepare_live_offer_evaluation(
    database_path: Path, profile_id: str, offer_ids: list[Any]
) -> str:
    """Create a user-visible evaluation run from current offer snapshots only."""
    from .prompts import EVALUATOR_PROMPT_VERSION, JUDGE_PROMPT_VERSION

    profile_row = get_user_profile(database_path, profile_id)
    if not profile_row or not is_profile_ready_for_scoring(database_path, profile_id):
        raise ValueError("Choose an approved profile before evaluating offers")
    profile = CandidateProfile.model_validate(profile_row["profile"])
    selected = []
    for value in offer_ids:
        try:
            offer = get_offer(database_path, int(value))
        except (TypeError, ValueError):
            continue
        if (
            offer
            and offer["availability_status"] == "active"
            and _offer_needs_evaluation(offer, profile_row["profile"])
        ):
            selected.append(offer)
    if not selected:
        raise ValueError("There are no active offers requiring evaluation")
    started_at = datetime.now(UTC)
    run_id = "product-eval-" + uuid.uuid4().hex[:16]
    run = PipelineRun(
        run_id=run_id,
        source_id="product",
        run_type="product_batch_eval",
        started_at=started_at,
        status=EvaluationRunStatus.RUNNING,
        total_items=len(selected),
        current_stage="starting",
        profile_id=profile.profile_id,
        profile_version=profile.version,
        prompt_versions={"evaluator": EVALUATOR_PROMPT_VERSION, "judge": JUDGE_PROMPT_VERSION},
        model_configurations={"model": Settings.from_env().evaluation_model},
    )
    items = []
    for offer in selected:
        payload = offer["offer"]
        snapshot = {
            "company": offer["company"],
            "title": offer["title"],
            "url": offer["job_url"],
            "analysis_text": payload.get("analysis_text"),
            "supplemental_info": payload.get("supplemental_info") or {},
            "content_sha256": offer.get("current_content_sha256"),
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "model": Settings.from_env().evaluation_model,
            "rules_version": ROLE_RULES_VERSION,
        }
        items.append(
            EvaluationRunItem(
                item_id=f"{run_id}:{offer['id']}",
                run_id=run_id,
                offer_id=offer["id"],
                input_sha256=_evaluation_cache_key(offer, profile_row["profile"]),
                input_snapshot=snapshot,
            )
        )
    create_evaluation_run(database_path, run, profile, items)
    return run_id


def _evaluation_cache_key(offer: dict, profile: dict) -> str:
    settings = Settings.from_env()
    model_file = settings.evaluation_model_path
    model_stat = model_file.stat() if model_file.is_file() else None
    payload = {
        "offer": offer.get("current_content_sha256"),
        "profile_id": profile.get("profile_id"),
        "profile_version": profile.get("version"),
        "model": settings.evaluation_model,
        "model_path": str(model_file),
        "model_file": (model_stat.st_size, model_stat.st_mtime_ns) if model_stat else None,
        "prompt": ROLE_FIT_PROMPT_VERSION,
        "requirements": MATRIX_VERSION,
        "rules": ROLE_RULES_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _offer_needs_evaluation(offer: dict, profile: dict) -> bool:
    return bool(
        offer.get("needs_evaluation")
        or not offer.get("assessment")
        or offer.get("evaluation_input_sha256") != _evaluation_cache_key(offer, profile)
    )


async def _run_dummy_demo(
    project_root: Path,
    database_path: Path,
    run_id: str,
    _progress_callback: ProgressCallback,
) -> None:
    from .demo_data import load_candidate_profile, load_frozen_demo_offers
    from .demo_flow import load_extractions, run_demo_flow_db
    from .settings import Settings

    settings = Settings.from_env()
    run = get_pipeline_run(database_path, run_id)
    if run and run.get("run_type") in {"product_batch_eval", "live_single_offer"}:
        from .product_evaluation import run_product_evaluation

        profile = CandidateProfile.model_validate(run["profile"])
        await run_product_evaluation(database_path, run_id, profile, settings)
        return
    if run and run.get("profile"):
        profile = CandidateProfile.model_validate(run["profile"])
    else:
        profile = load_candidate_profile(project_root / "config/demo/dummy-profile-v1.json")
    if run and run.get("run_type") in {"live_single_offer", "product_batch_eval"}:
        from .ats_scrapers import CleanJob

        run_items = list_evaluation_run_items(database_path, run_id)
        offers = []
        for item in run_items:
            stored_offer = get_offer(database_path, item["offer_id"])
            if not stored_offer:
                raise ValueError(f"Offer {item['offer_id']} disappeared before evaluation")
            offers.append(CleanJob.model_validate(stored_offer["offer"]))
        extractions = {}
    else:
        offers = load_frozen_demo_offers(
            project_root / "config/demo/frozen-offers-v1.json", project_root
        )
        extractions = load_extractions(project_root / "data/demo/extractor-benchmark-qwen-v3.json")

    await run_demo_flow_db(database_path, run_id, offers, profile, extractions, settings)


def _is_ambitious_offer(item: dict) -> bool:
    assessment = item.get("assessment") or {}
    return bool(
        not item.get("needs_evaluation")
        and item.get("offer", {}).get("role_direction") == "target"
        and assessment.get("opportunity_score", 0) >= 7
        and assessment.get("gaps")
    )


def _offer_card(item: dict, *, profile_id: str | None = None) -> str:
    offer = item["offer"]
    locations = _display_location(offer.get("locations") or [])
    status = STATUS_LABELS.get(item["application_status"], item["application_status"])
    availability = item.get("availability_status", "active")
    availability_label = "Widoczna teraz" if availability == "active" else "Nie znaleziona w źródle"
    language = str(item.get("original_language") or "unknown").upper()
    assessment = {}
    if (
        profile_id
        and item.get("assessment_profile_id") == profile_id
        and not item.get("needs_evaluation")
    ):
        assessment = item.get("assessment") or {}
    score = assessment.get("final_score")
    recommendation = {
        "apply": "Warto aplikować",
        "consider": "Warto sprawdzić",
        "prepare_first": "Najpierw przygotowanie",
        "low_priority": "Niski priorytet",
    }.get(str(assessment.get("recommendation") or ""), "Otwórz szczegóły")
    score_html = (
        f'<span class="offer-card__score"><strong>{float(score):.1f}</strong>'
        "<span>/ 10 · ocena wstępna</span></span>"
        if score is not None
        else '<span class="muted">Jeszcze bez oceny</span>'
    )
    review_chip = (
        '<span class="chip">Do ręcznej oceny kierunku</span>'
        if offer.get("role_direction") == "review"
        else ""
    )
    hidden_chip = (
        f'<span class="chip" title="{escape(str(offer.get("role_reason") or ""))}">'
        "Ukryta przez filtr software</span>"
        if offer.get("role_direction") == "software"
        else ""
    )
    feedback_chip = (
        '<span class="chip">Błędna ocena modelu</span>'
        if item.get("has_false_negative_feedback") and not item.get("needs_evaluation")
        else ""
    )
    ambitious_chip = '<span class="chip">Ambitna — sprawdź luki w CV</span>' if _is_ambitious_offer(item) else ""
    return f"""<a class="card offer-card" href="/offers/{item["id"]}">
      <div class="offer-card__top"><span class="offer-card__company">
        {escape(item["company"])}</span>
        <span class="chip chip--{escape(availability)}">{availability_label}</span></div>
      <h2>{escape(item["title"])}</h2>
      <p class="offer-card__location">{escape(locations)}</p>
      <div class="chips"><span class="chip" title="Język oryginału">{escape(language)}</span>
      <span class="chip">{escape(status)}</span>{review_chip}{hidden_chip}{feedback_chip}{ambitious_chip}</div>
      <div class="offer-card__footer">{score_html}
        <span class="offer-card__next">{escape(recommendation)} →</span></div>
    </a>"""


def _display_location(values: list[str]) -> str:
    location = ", ".join(str(value).strip() for value in values if str(value).strip())
    if not location:
        return "Lokalizacja nieznana"
    coordinate_tail = re.search(
        r"\b-?\d{1,3}\.\d{4,}\s+-?\d{1,3}\.\d{4,}\s+(.+)$",
        location,
    )
    if coordinate_tail:
        location = coordinate_tail.group(1).strip()
    return location if len(location) <= 120 else f"{location[:117].rstrip()}…"


def _event_label(event_type: str) -> str:
    return {
        "new": "Nowa oferta",
        "updated": "Treść zmieniona",
        "disappeared": "Zniknęła ze źródła",
        "reappeared": "Ponownie widoczna",
    }.get(event_type, event_type)


def _offer_event_card(item: dict) -> str:
    fields = ", ".join(item["event"].get("changed_fields") or [])
    detail = fields or (
        "Oferta nie występuje już w ostatnim poprawnym odczycie."
        if item["event_type"] == "disappeared"
        else "Dostępność oferty została zaktualizowana."
    )
    return f"""<a class="card" href="/offers/{item["offer_id"]}">
      <div class="company">{escape(_event_label(item["event_type"]))}</div>
      <h3>{escape(item["company"])} — {escape(item["title"])}</h3>
      <p class="muted">{escape(detail)}</p>
      <p class="muted">{escape(item["created_at"][:16].replace("T", " "))}</p>
    </a>"""


def _evaluation_card(item: dict) -> str:
    status = str(item["status"])
    assessment = item.get("final_assessment")
    if status != "completed" or not assessment:
        status_label = {
            "pending": "Oczekuje",
            "running": "W toku",
            "cancelled": "Anulowano",
            "interrupted": "Przerwano",
            "failed": "Błąd oceny",
        }.get(status, status)
        error = item.get("error")
        error_html = f'<p class="warning">{escape(str(error))}</p>' if error else ""
        return f"""<article class="card">
          <div class="company">{escape(item["company"])}</div>
          <h2>{escape(item["title"])}</h2>
          <p class="run-status">{escape(status_label)}</p>{error_html}
          {_technical_details(item)}
        </article>"""

    judgment = item.get("judgment") or {}
    draft = item.get("draft") or {}
    evidence = assessment.get("evidence") or []
    issues = judgment.get("issues") or []
    evidence_html = "".join(
        f"<li>“{escape(value.get('quote', ''))}” "
        f"<span class='muted'>({escape(value.get('source_field', ''))})</span></li>"
        for value in evidence[:4]
    )
    issues_html = "".join(f"<li>{escape(value)}</li>" for value in issues)
    score = float(assessment["final_score"])
    opportunity = float(assessment["opportunity_score"])
    cv_fit = float(assessment["cv_fit_score"])
    work_conditions = float(assessment["work_conditions_score"])
    recommendation = escape(str(assessment.get("recommendation") or "unknown"))
    return f"""<article class="card">
      <div class="company">{escape(item["company"])}</div><h2>{escape(item["title"])}</h2>
      <div class="score">{score:.1f}</div><div class="muted">{recommendation}</div>
      <div class="score-grid">
        <div class="score-box">Opportunity<br><strong>{opportunity:.1f}</strong></div>
        <div class="score-box">CV fit<br><strong>{cv_fit:.1f}</strong></div>
        <div class="score-box">Warunki<br><strong>{work_conditions:.1f}</strong></div>
      </div>
      <details><summary>Evaluator</summary>
        <p>{escape(str(draft.get("reasoning") or "Brak uzasadnienia."))}</p>
      </details>
      <details><summary>Dowody ({len(evidence)})</summary><ul>{evidence_html}</ul></details>
      <details><summary>Self-review: same model ({len(issues)} uwag)</summary>
        <div class="warning"><ul>{issues_html or "<li>Brak uwag</li>"}</ul></div></details>
      {_technical_details(item)}
    </article>"""


def _technical_details(item: dict) -> str:
    timings = item.get("timings") or {}
    retry_counts = item.get("retry_counts") or {}
    if not timings and not retry_counts:
        return ""
    timing_rows = (
        "".join(
            f"<li>{escape(str(stage))}: {escape(str(elapsed))} ms</li>"
            for stage, elapsed in sorted(timings.items())
        )
        or "<li>Brak timingów</li>"
    )
    retry_rows = (
        "".join(
            f"<li>{escape(str(stage))}: {escape(str(count))}</li>"
            for stage, count in sorted(retry_counts.items())
        )
        or "<li>Brak retry</li>"
    )
    return f"""<details><summary>Diagnostyka</summary>
      <p><strong>Timingi</strong></p><ul>{timing_rows}</ul>
      <p><strong>Retry</strong></p><ul>{retry_rows}</ul>
    </details>"""


def _items(items: list[dict]) -> str:
    return (
        "".join(
            f"<li><strong>{escape(item['category'])}</strong> — {escape(item['evidence'])}</li>"
            for item in items
        )
        or "<li>Brak wykrytych informacji</li>"
    )


def _split_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _profile_id(display_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", display_name.casefold()).strip("-") or "profile"
    return f"{base[:40]}-{uuid.uuid4().hex[:8]}"


def _profile_form(*, error: str | None = None) -> str:
    error_html = f'<p class="form-error">{escape(error)}</p>' if error else ""
    return f"""<div class="wrap panel-section"><section class="hero-grid">
      <article class="hero-card"><span class="eyebrow">Krok 1 z 3</span>
        <h2>Dodaj CV. Resztę zrobimy razem.</h2>
        <p>PDF zostanie odczytany lokalnie. Jeśli to skan, uruchomimy OCR po polsku
        i angielsku. W następnym kroku zobaczysz cały tekst przed zatwierdzeniem.</p>
      </article><aside class="hero-aside"><span class="eyebrow">Bez ankiety na start</span>
        <strong>Najpierw plik, potem zatwierdzenie</strong>
        <p class="muted">Role, preferencje i warunki doprecyzujesz w profilu po sprawdzeniu CV.</p></aside></section>{error_html}
      <form class="form-card form-grid" method="post" action="/profiles"
        enctype="multipart/form-data">
        <label class="field wide">Twoje CV w PDF
          <input name="cv_file" type="file" accept="application/pdf,.pdf" required>
          <span class="form-help">maks. 15 MB i 12 stron; plik pozostaje lokalnie</span></label>
        <label class="field wide">Nazwa profilu <span class="muted">(opcjonalnie)</span>
          <input name="display_name" placeholder="np. Profil demonstracyjny — Applied AI">
          <span class="form-help">Jeśli zostawisz puste, użyjemy nazwy pliku.</span></label>
        <div class="wide"><button type="submit">Odczytaj moje CV</button></div>
      </form></div>"""


def _cv_first_profile_draft(profile_id: str) -> dict:
    """A local draft that cannot be mistaken for an approved scoring profile."""
    return {
        "schema_version": "candidate-profile-draft-v1",
        "profile_id": profile_id,
        "version": 1,
        "target_roles": [],
        "evidence": [],
        "transferable_skills": [],
        "work_preferences": [],
        "negative_criteria": [],
        "location_rule": "",
        "approved_at": None,
    }


def _profile_card(profile: dict) -> str:
    payload = profile["profile"]
    roles = "".join(
        f"<span class='tag'>{escape(role)}</span>" for role in payload["target_roles"][:3]
    )
    imported_career_description = any(
        "career knowledge base" in str(item.get("source", "")).casefold()
        for item in payload.get("evidence", [])
    )
    document = (
        f"CV: {escape(str(profile['original_filename']))} · {profile['page_count']} str. · "
        f"{escape(str(profile['extraction_method']))}"
        if profile.get("original_filename")
        else (
            "Opis kariery zaimportowany · CV możesz dodać później"
            if imported_career_description
            else "Profil utworzony bez dokumentu CV"
        )
    )
    status_label = {
        "ready": "Gotowy do LLM",
        "cv_approved": "CV gotowe",
        "cv_review": "Sprawdź odczyt CV",
        "draft": "Szkic",
    }.get(profile["status"], "Wymaga uwagi")
    return f"""<a class="card profile-card" href="/profiles/{escape(profile["profile_id"])}">
      <div class="company">{escape(status_label)}</div><h2>{escape(profile["display_name"])}</h2>
      <div class="tag-row">{roles}</div><p class="muted">{document}</p>
      <p class="{"status-ready" if profile["status"] == "ready" else "status-draft"}">{status_label} →</p></a>"""


def _lab_model_card(model: dict) -> str:
    config = model["config"]
    path_exists = Path(model["model_path"]).is_file()
    return f"""<article class="card lab-card"><div class="company">{escape(model["role"])}</div>
      <h3>{escape(model["display_name"])}</h3><p class="muted">{escape(model["model_path"])}</p>
      <div class="tag-row"><span class="tag">ctx {config.get("context_size")}</span><span class="tag">T {config.get("temperature")}</span><span class="tag">seed {config.get("seed")}</span></div>
      <p class="{"runtime-good" if path_exists else "runtime-warn"}">{"Plik lokalny wykryty" if path_exists else "Ścieżka modelu nie istnieje"}</p></article>"""


def _experiment_row(item: dict) -> str:
    return f"""<tr><td>{escape(item["display_name"])}</td><td>{escape(item["task_kind"])}</td>
      <td>{escape(item["dataset_key"])}</td><td>{escape(str(item["model_name"] or "nie wybrano"))}</td>
      <td>{escape(item["status"])}</td></tr>"""


def _offer_detail(item: dict) -> str:
    offer = item["offer"]
    supplemental = offer.get("supplemental_info") or {}
    locations = escape(_display_location(offer.get("locations") or []))
    current_status = escape(
        STATUS_LABELS.get(item["application_status"], item["application_status"])
    )
    availability = item.get("availability_status", "active")
    availability_label = (
        "Widoczna podczas ostatniego udanego odczytu"
        if availability == "active"
        else "Nie znaleziona podczas ostatniego udanego odczytu źródła"
    )
    availability_meta = item.get("disappeared_at") or item.get("last_checked_at") or "brak danych"
    language = str(item.get("original_language") or "unknown").upper()
    confidence = item.get("language_confidence")
    confidence_label = f"{float(confidence) * 100:.0f}%" if confidence is not None else "brak"
    translation = item.get("translation")
    if language == "EN":
        language_path = "Tekst jest już po angielsku — tłumaczenie nie jest potrzebne."
    elif translation and translation["status"] == "approved":
        language_path = (
            "Zatwierdzona kanoniczna wersja angielska jest gotowa do porównania z profilem."
        )
    elif translation:
        language_path = "Wersja angielska jest szkicem i nie zostanie użyta bez zatwierdzenia."
    else:
        language_path = (
            "Ocena bezpośrednia jest możliwa; kanoniczna wersja angielska nie została "
            "jeszcze przygotowana."
        )
    work_items = (supplemental.get("work_conditions") or []) + (
        supplemental.get("compensation") or []
    )
    interesting_benefits = supplemental.get("interesting_benefits") or []
    travel_requirements = supplemental.get("travel_requirements") or []
    evidence = item.get("section_evidence") or []
    evidence_by_section: dict[str, list[dict]] = {}
    for entry in evidence:
        evidence_by_section.setdefault(entry["section_kind"], []).append(entry)
    description = escape(offer.get("description") or "")
    versions = item.get("versions") or []
    version_items = (
        "".join(
            f"<li><strong>{escape(_event_label(version['change_kind']))}</strong> · "
            f"{escape(version['created_at'][:19].replace('T', ' '))} · "
            f"{escape(', '.join(version['changed_fields']) or 'pierwsza wersja')}</li>"
            for version in versions
        )
        or "<li>Brak wersji historycznych.</li>"
    )
    evaluation_state = (
        "Treść zmieniła się — oferta czeka na ponowną ocenę."
        if item.get("needs_evaluation")
        else "Ocena odpowiada bieżącej wersji treści."
    )
    role_note = (
        f"<p class='muted'>Kierunek oferty wymaga ręcznej oceny: {escape(str(offer.get('role_reason') or 'opis jest niejednoznaczny'))}</p>"
        if offer.get("role_direction") == "review"
        else f"<p class='muted'>Ukryta przez filtr software: {escape(str(offer.get('role_reason') or 'przeważają obowiązki software'))}. Możesz ją zapisać lub zmienić etap aplikacji.</p>"
        if offer.get("role_direction") == "software"
        else ""
    )
    status_buttons = "".join(
        f'<form method="post" action="/offers/{item["id"]}/status/{value}">'
        f'<button type="submit">{escape(label)}</button></form>'
        for value, label in STATUS_LABELS.items()
        if value != item["application_status"]
    )
    evaluation_button = (
        f'<button type="button" data-evaluate-stale data-profile-id="{escape(item["evaluation_profile_id"])}">Oceń tę ofertę</button>'
        if item.get("evaluation_profile_id")
        else '<a class="button button--quiet" href="/profiles">Wybierz profil do oceny</a>'
    )
    assessment = item.get("assessment") or {}
    feedback = item.get("evaluation_feedback")
    evaluation_hash = escape(str(item.get("evaluation_input_sha256") or ""))
    if feedback:
        feedback_section = f"""<section class="form-card">
          <h2>Błędna ocena modelu</h2>
          <p>Zgłosiłaś, że ta oferta może Ci pasować mimo negatywnej oceny modelu.</p>
          <form method="post" action="/offers/{item["id"]}/evaluation-feedback/clear">
            <input type="hidden" name="evaluation_input_sha256" value="{evaluation_hash}">
            <button type="submit" class="button--quiet">Cofnij zgłoszenie</button>
          </form></section>"""
    elif (
        evaluation_hash
        and not item.get("needs_evaluation")
        and assessment.get("recommendation") in {"low_priority", "prepare_first"}
    ):
        feedback_section = f"""<section class="form-card">
          <h2>Model ocenił ofertę jako niepasującą?</h2>
          <p>Jeśli uważasz, że pasuje, oznacz błędną ocenę. To nie zmienia etapu aplikacji.</p>
          <form method="post" action="/offers/{item["id"]}/evaluation-feedback/false-negative">
            <input type="hidden" name="evaluation_input_sha256" value="{evaluation_hash}">
            <label class="field">Co model przeoczył? (opcjonalnie)
              <textarea name="note" maxlength="1000" rows="2"></textarea></label>
            <button type="submit">Błędna ocena — oferta mi pasuje</button>
          </form></section>"""
    else:
        feedback_section = ""
    responsibilities_section = _offer_evidence_section(
        "Obowiązki", evidence_by_section.get("responsibilities", [])
    )
    requirements_section = _offer_evidence_section(
        "Wymagania", evidence_by_section.get("requirements", [])
    )
    conditions_section = _offer_evidence_section(
        "Warunki i wynagrodzenie",
        evidence_by_section.get("work_conditions", [])
        + evidence_by_section.get("compensation", []),
        fallback=work_items,
    )
    benefits_section = _offer_evidence_section(
        "Ciekawsze benefity",
        evidence_by_section.get("benefits", []),
        fallback=interesting_benefits,
        empty=False,
    )
    travel_section = _offer_evidence_section(
        "Podróże służbowe",
        evidence_by_section.get("travel", []),
        fallback=travel_requirements,
    )
    published_at = offer.get("published_at") or "Nie podano"
    employment_type = offer.get("employment_type") or "Nie podano"
    seniority = offer.get("seniority") or "Nie podano"
    decision_card = _offer_decision_card(item, evidence_by_section)
    return f"""<div class="wrap detail"><p><a href="/">← Wszystkie oferty</a></p>
      <div class="chips"><span class="chip">Lokalizacja: {locations}</span>
        <span class="chip">Umowa: {escape(str(employment_type))}</span>
        <span class="chip">Senior: {escape(str(seniority))}</span>
        <span class="chip">Język: {escape(language)}</span></div>
      <p class="muted">Opublikowano: {escape(str(published_at)[:19].replace("T", " "))}</p>
      <p>Aktualny status: <strong>{current_status}</strong></p>
      <p><span class="chip chip--{escape(availability)}">{availability_label}</span>
      <span class="muted">· sprawdzono: {escape(str(availability_meta)[:19].replace("T", " "))}</span></p>
      <p class="muted">Pewność rozpoznania języka: {escape(confidence_label)}</p>
      <p class="muted">{escape(language_path)}</p>
      {role_note}
      <div class="actions">{status_buttons}
        {evaluation_button}
        <a class="button" href="{escape(item["job_url"])}" target="_blank" rel="noreferrer">
          Otwórz ofertę ↗
        </a>
      </div>
      {decision_card}{feedback_section}
      <section><h2>Najważniejsze informacje</h2><p class="muted">Każdy wykryty element można sprawdzić w cytacie ze źródła.</p>
      {conditions_section}</section>
      {responsibilities_section}{requirements_section}{benefits_section}{travel_section}
      <section><h2>Historia i aktualność oceny</h2>
        <p class="muted">{escape(evaluation_state)}</p><ul>{version_items}</ul></section>
      <details><summary>Pełny opis źródłowy</summary>
        <div class="description">{description}</div>
      </details>
    </div>"""


def _offer_decision_card(item: dict, evidence_by_section: dict[str, list[dict]]) -> str:
    """Keep the decision state compact while never filling missing evidence with prose."""
    unknowns = [
        label
        for section, label in (
            ("responsibilities", "obowiązki"),
            ("requirements", "wymagania"),
            ("work_conditions", "warunki pracy"),
            ("compensation", "wynagrodzenie"),
            ("travel", "podróże"),
        )
        if not evidence_by_section.get(section)
    ]
    assessment = item.get("assessment") or {}
    if not assessment:
        return f"""<section class="form-card"><div class="company">Karta decyzji</div>
          <h2>Oferta nie została jeszcze oceniona</h2>
          <p class="muted">Uruchom ocenę po wyborze profilu gotowego do scoringu.</p>
          <p><strong>Niewiadome:</strong> {escape(", ".join(unknowns) or "brak")}</p></section>"""
    score = assessment.get("final_score")
    recommendation = assessment.get("recommendation") or "brak rekomendacji"
    strengths = assessment.get("strengths") or []
    gaps = assessment.get("gaps") or []
    assessment_evidence = assessment.get("evidence") or []
    evidence_rows = (
        "".join(
            f"<li>“{escape(str(entry.get('quote') or ''))}” "
            f"<span class='muted'>({escape(str(entry.get('source_field') or ''))})</span></li>"
            for entry in assessment_evidence
        )
        or "<li>Brak cytatów z oceny — wynik wymaga ponownej oceny.</li>"
    )
    return f"""<section class="form-card"><div class="company">Karta decyzji</div>
      <h2>{escape(str(recommendation))} · {escape(str(score if score is not None else "unknown"))}/10</h2>
      <p class="warning">Ocena wstępna: obecny model nie przeszedł progu jakości. Sprawdź obowiązki i wymagania przed decyzją.</p>
      <p><strong>Dopasowanie:</strong> {escape(", ".join(map(str, strengths)) or "Nie wskazano.")}</p>
      <p><strong>Wymagania do sprawdzenia:</strong> {escape(", ".join(map(str, gaps)) or "Nie wskazano; nie oznacza to braku luk.")}</p>
      <p><strong>Niewiadome:</strong> {escape(", ".join(unknowns) or "brak")}</p>
      <details><summary>Dowody oceny</summary><ul>{evidence_rows}</ul></details></section>"""


def _offer_evidence_section(
    heading: str,
    entries: list[dict],
    *,
    fallback: list[dict] | None = None,
    empty: bool = True,
) -> str:
    """Show only quoted extraction results; absent evidence remains explicitly unknown."""
    if not entries and not fallback and not empty:
        return ""
    if entries:
        rows = "".join(
            f"<li>{('<strong>' + escape(str(entry['category'])) + '</strong> — ') if entry['category'] else ''}"
            f"{escape(str(entry['value'].get('text', '')))}"
            f"<details><summary>Źródło i metoda</summary><p>{escape(entry['source_quote'])}</p>"
            f"<p class='muted'>{escape(entry['extraction_method'])} · pewność {float(entry['confidence']) * 100:.0f}%</p></details></li>"
            for entry in entries
        )
    elif fallback:
        rows = _items(fallback)
    else:
        rows = "<li>Nie znaleziono w ogłoszeniu.</li>"
    return f"<section><h2>{heading}</h2><ul>{rows}</ul></section>"


def _render_demo_start_form(project_root: Path, database_path: Path) -> str:
    from .demo_data import load_frozen_demo_offers

    try:
        offers = load_frozen_demo_offers(
            project_root / "config/demo/frozen-offers-v1.json", project_root
        )
    except FileNotFoundError:
        offers = []

    options = "".join(
        f'<option value="{escape(o.company)}|{escape(o.title)}">'
        f"{escape(o.company)} — {escape(o.title)}</option>"
        for o in offers
    )
    live_options = "".join(
        f'<option value="live:{item["id"]}">{escape(item["company"])} — '
        f"{escape(item['title'])}</option>"
        for item in list_offers(database_path)
    )
    profile_options = "".join(
        f'<option value="{escape(profile["profile_id"])}">'
        f"{escape(profile['display_name'])} (potwierdzony)</option>"
        for profile in list_user_profiles(database_path)
        if is_profile_ready_for_scoring(database_path, profile["profile_id"])
    )

    form_style = "max-width: 400px; flex-direction: column;"
    on_mode_change = (
        "const frozen=document.getElementById('offer-select');"
        "const live=document.getElementById('live-offer-select');"
        "frozen.style.display=this.value==='single_offer_eval'?'block':'none';"
        "frozen.disabled=this.value!=='single_offer_eval';"
        "live.style.display=this.value==='live_single_offer'?'block':'none';"
        "live.disabled=this.value!=='live_single_offer';"
    )
    return f"""
    <form method="post" action="/model-lab/demo/start" class="actions" style="{form_style}">
      <select name="mode" id="mode-select" onchange="{on_mode_change}">
        <option value="frozen_evaluation" selected>Szybki test 10 ofert</option>
        <option value="single_offer_eval">Test 1 oferty</option>
        <option value="live_single_offer">Ocena 1 pobranej oferty</option>
        <option value="frozen_full_pipeline">Pełny test 10 ofert</option>
      </select>
      <select name="profile_id">
        <option value="">Profil syntetyczny (test techniczny)</option>
        {profile_options}
      </select>
      <select name="offer_key" id="offer-select" style="display: none;">
        {options}
      </select>
      <select name="offer_key" id="live-offer-select" style="display: none;" disabled>
        {live_options}
      </select>
      <button type="submit">Uruchom wybrany test</button>
    </form>
    """
