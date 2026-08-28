import asyncio
import hashlib
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from job_scout.app_guide import (
    AppGuideTurn,
    GuideAction,
    get_or_create_app_guide,
)
from job_scout.ats_scrapers import CleanJob
from job_scout.career import InterviewTurn, KnowledgeProposal, get_active_interview
from job_scout.domain import (
    CandidateEvidence,
    CandidateProfile,
    EvaluationRunItem,
    PipelineRun,
)
from job_scout.local_llm import LocalLlmError
from job_scout.profiles import ExtractedCv
from job_scout.storage import (
    approve_profile_document,
    connect,
    create_evaluation_run,
    get_latest_evaluation_run,
    get_user_profile,
    initialize_database,
    list_evaluation_run_items,
    list_pipeline_runs,
    persist_clean_offer,
    resume_run,
    retry_failed_evaluation_run,
    review_profile_fact,
    save_profile_document,
    save_profile_fact,
    save_user_profile,
)
from job_scout.web import _display_location, _prepare_dummy_demo_run, create_app


def clean_offer(**overrides):
    values = {
        "source_id": "example",
        "company": "Example AI",
        "external_id": "job-42",
        "title": "AI Engineer",
        "url": "https://example.com/jobs/42",
        "locations": ["Warsaw, Poland"],
        "description": "Build AI products in Poland.",
        "analysis_text": "Build AI products in Poland.",
        "raw_sha256": "a" * 64,
        "raw_payload": "<html>first version</html>",
        "extraction_method": "html",
    }
    values.update(overrides)
    return CleanJob(**values)


def test_display_location_hides_scraper_metadata():
    assert _display_location(
        [
            "Warszawa Województwo mazowieckie pl Czerniakowska 87A 00-718 "
            "True False 52.204097 21.0477584 Warszawa, Województwo mazowieckie, Poland"
        ]
    ) == "Warszawa, Województwo mazowieckie, Poland"


def candidate_profile():
    return CandidateProfile(
        profile_id="synthetic-flow-test",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[CandidateEvidence(id="python", statement="Uses Python", source="cv")],
        location_rule="Remote from Poland or Warsaw",
        approved_at=datetime.now(UTC),
    )


class FakeCareerRuntime:
    def __init__(self):
        self.state = "idle"
        self.started = 0
        self.stopped = 0

    def snapshot(self):
        return {
            "state": self.state,
            "ready": self.state == "ready",
            "model": "bielik.gguf",
            "model_available": True,
            "managed": self.state == "ready",
            "detail": None,
        }

    async def refresh_status(self):
        return self.snapshot()

    async def ensure_ready(self):
        self.started += 1
        self.state = "ready"
        return self.snapshot()

    async def stop(self):
        self.stopped += 1
        self.state = "idle"


def client_with_offer(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, clean_offer(), source_url="https://example.com/careers"
        )
    return TestClient(create_app(path)), path, offer_id


def _test_run_preparer(path):
    def prepare(mode: str, offer_key: str | None) -> str:
        latest = get_latest_evaluation_run(path)
        if latest and latest["status"] in {"cancelled", "interrupted", "partial"}:
            resume_run(path, latest["run_id"])
            return latest["run_id"]

        profile = candidate_profile()
        items = []
        for index in range(1, 3):
            offer = clean_offer(
                external_id=f"integration-{index}",
                title=f"Integration Offer {index}",
                url=f"https://example.com/integration/{index}",
                raw_sha256=f"{index:x}" * 64,
            )
            with connect(path) as connection:
                offer_id, _ = persist_clean_offer(
                    connection, offer, source_url="https://example.com/careers"
                )
            snapshot = {"company": offer.company, "title": offer.title}
            items.append(
                EvaluationRunItem(
                    item_id=f"integration-run:{offer_id}",
                    run_id="integration-run",
                    offer_id=offer_id,
                    input_sha256=hashlib.sha256(repr(snapshot).encode()).hexdigest(),
                    input_snapshot=snapshot,
                )
            )
        create_evaluation_run(
            path,
            PipelineRun(
                run_id="integration-run",
                source_id="test",
                run_type="demo",
                total_items=2,
                current_stage="starting",
                profile_id=profile.profile_id,
                profile_version=profile.version,
                model_configurations={"model": "fake-model"},
            ),
            profile,
            items,
        )
        return "integration-run"

    return prepare


def _wait_for(event: threading.Event) -> None:
    for _ in range(100):
        if event.wait(0.02):
            return
    raise AssertionError("background runner did not reach the expected state")


def _create_history_run(path, run_id, run_status, item_status):
    profile = candidate_profile()
    offer = clean_offer(
        external_id=run_id,
        title=f"Offer for {run_id}",
        url=f"https://example.com/history/{run_id}",
        raw_sha256=(run_id[0] * 64),
    )
    with connect(path) as connection:
        offer_id, _ = persist_clean_offer(
            connection, offer, source_url="https://example.com/careers"
        )
    item = EvaluationRunItem(
        item_id=f"{run_id}:{offer_id}",
        run_id=run_id,
        offer_id=offer_id,
        status=item_status,
        input_sha256="f" * 64,
        input_snapshot={"company": offer.company},
        error="model timeout" if item_status == "failed" else None,
    )
    create_evaluation_run(
        path,
        PipelineRun(
            run_id=run_id,
            source_id="history-test",
            run_type="demo",
            status=run_status,
            total_items=1,
            completed_items=int(item_status in {"completed", "failed"}),
            current_stage="finished" if run_status != "running" else "evaluator",
            finished_at=(datetime.now(UTC) if run_status != "running" else None),
            profile_id=profile.profile_id,
            profile_version=profile.version,
            model_configurations={"model": "history-model"},
        ),
        profile,
        [item],
    )


def test_panel_lists_and_opens_offer(tmp_path):
    client, _, offer_id = client_with_offer(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert "AI Engineer" in response.text
    detail = client.get(f"/offers/{offer_id}")
    assert detail.status_code == 200
    assert "Build AI products in Poland" in detail.text
    assert "Widoczna podczas ostatniego udanego odczytu" in detail.text
    assert "Język: EN" in detail.text
    assert "tłumaczenie nie jest potrzebne" in detail.text
    assert "Zapytaj Bielika" in detail.text
    assert "Nie znaleziono w ogłoszeniu." in detail.text
    assert "Karta decyzji" in detail.text
    assert "Oferta nie została jeszcze oceniona" in detail.text


def test_offer_copilot_start_is_available_only_for_ready_profile(tmp_path):
    client, path, offer_id = client_with_offer(tmp_path)
    empty = client.get(f"/offers/{offer_id}/ask")
    assert "Najpierw zatwierdź profil" in empty.text
    profile = candidate_profile()
    save_user_profile(
        path,
        profile_id=profile.profile_id,
        display_name="Synthetic profile",
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    for category, value in (
        ("target_role", "Applied AI Engineer"),
        ("work_location", "Poland"),
        ("work_model", "Remote"),
        ("contract", "No constraint"),
        ("language", "English B2"),
        ("travel", "No travel"),
        ("experience", "Uses Python"),
    ):
        fact_id = save_profile_fact(
            path,
            profile_id=profile.profile_id,
            category=category,
            value={"text": value},
            source_type="user_message",
            source_ref="test-message",
            source_quote=value,
            usable_for_scoring=True,
        )
        review_profile_fact(path, fact_id=fact_id, approve=True)
    page = client.get(f"/offers/{offer_id}/ask")
    assert "Rozpocznij rozmowę" in page.text
    started = client.post(
        f"/offers/{offer_id}/ask/start",
        data={"profile_id": profile.profile_id},
        follow_redirects=False,
    )
    assert started.status_code == 303
    chat = client.get(started.headers["location"])
    assert "Pytanie o ofertę" in chat.text


def test_real_cv_flow_imports_master_approves_mapping_and_starts_offer_session(tmp_path):
    client, path, offer_id = client_with_offer(tmp_path)
    profile = candidate_profile()
    save_user_profile(
        path,
        profile_id=profile.profile_id,
        display_name="Synthetic profile",
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    for category, value in (
        ("target_role", "Applied AI Engineer"),
        ("work_location", "Poland"),
        ("work_model", "Remote"),
        ("contract", "No constraint"),
        ("language", "English B2"),
        ("travel", "No travel"),
        ("experience", "Uses Python"),
    ):
        fact_id = save_profile_fact(
            path,
            profile_id=profile.profile_id,
            category=category,
            value={"text": value},
            source_type="user_message",
            source_ref="test-message",
            source_quote=value,
            usable_for_scoring=True,
            usable_for_cv=category in {"language", "experience"},
        )
        review_profile_fact(path, fact_id=fact_id, approve=True)
    master = b"""<html><head><style>.resume-page{display:block}</style></head><body>
    <div class="highlights-bar"><div class="hl-value">AI automation.</div></div>
    <div class="left-col"><section class="section"><h2 class="section-title">Experience</h2>
    <div class="item-title">Engineer</div><div class="item-desc">Built Python.</div>
    </section></div><section><h2>Profile</h2><div class="profile-text">Engineer.</div></section>
    <section><h2>Skills</h2><div class="skill-items">Python</div></section></body></html>"""

    imported = client.post(
        "/cv/master",
        data={"profile_id": profile.profile_id},
        files={"cv_file": ("master.html", master, "text/html")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    mapping = client.get(imported.headers["location"])
    assert "Sprawdź mapę CV" in mapping.text
    assert "Built Python" in mapping.text
    approved = client.post(imported.headers["location"] + "/approve", follow_redirects=False)
    assert approved.status_code == 303

    offer_page = client.get(f"/offers/{offer_id}")
    assert "Przygotuj CV" in offer_page.text
    chooser = client.get(f"/offers/{offer_id}/tailor")
    assert "Rozpocznij przygotowanie CV" in chooser.text
    started = client.post(
        f"/offers/{offer_id}/tailor/start",
        data={"profile_id": profile.profile_id},
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert started.headers["location"].startswith("/tailoring/tailor-")
    review = client.get(started.headers["location"])
    assert "Wygeneruj propozycje Qwenem" in review.text
    with connect(path) as connection:
        assert connection.execute(
            "SELECT application_status FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()[0] == "applying"


def test_product_shell_exposes_clear_information_architecture(tmp_path):
    client, _, _ = client_with_offer(tmp_path)

    today = client.get("/today")
    offers = client.get("/")
    bielik = client.get("/chat")
    applications = client.get("/applications")
    settings = client.get("/settings")

    assert today.status_code == offers.status_code == bielik.status_code == 200
    assert applications.status_code == 200
    assert settings.status_code == 200
    for response in (today, offers, bielik, applications, settings):
        assert "Start" in response.text
        assert "Oferty" in response.text
        assert "Bielik" in response.text
        assert "Aplikacje" in response.text
        assert "Profil" in response.text
        assert "Ustawienia" in response.text
        assert 'class="skip-link"' in response.text
        assert response.text.count("<main") == 1
    assert 'data-nav="today" aria-current="page"' in today.text
    assert 'data-nav="offers" aria-current="page"' in offers.text
    assert 'data-nav="bielik" aria-current="page"' in bielik.text


def test_bielik_hub_guides_user_from_cv_to_profile_and_offer_chats(tmp_path):
    client, path, offer_id = client_with_offer(tmp_path)

    guide = client.get("/chat")

    assert "jestem Bielik" in guide.text
    assert "Napisz do Bielika" in guide.text
    assert 'action="/chat/messages"' in guide.text
    assert "Najpierw poznajmy Twój profil" in guide.text
    assert 'href="/profiles/new"' in guide.text

    profile = candidate_profile()
    save_user_profile(
        path,
        profile_id=profile.profile_id,
        display_name="Synthetic profile",
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    ready = client.get("/chat")

    assert "Profil jest gotowy" in ready.text
    assert f'href="/profiles/{profile.profile_id}/interview"' in ready.text
    assert f'href="/offers/{offer_id}/ask"' in ready.text


def test_bielik_guide_chat_responds_before_profile_exists(tmp_path):
    class FakeGuideResponder:
        async def respond(self, context):
            assert context["mode"] == "application_guide"
            return AppGuideTurn(
                response_pl=(
                    "Najpierw dodaj CV. Potem wspólnie sprawdzimy profil i oferty."
                ),
                suggested_actions=[
                    GuideAction(label="Dodaj profil i CV", path="/profiles/new")
                ],
            )

    path = tmp_path / "job_scout.db"
    runtime = FakeCareerRuntime()
    client = TestClient(
        create_app(
            path,
            career_runtime=runtime,
            app_guide_responder=FakeGuideResponder(),
        )
    )
    client.get("/chat")
    session_id = get_or_create_app_guide(path)["session"]["session_id"]

    response = client.post(
        "/chat/messages",
        data={"session_id": session_id, "content": "Co mam teraz zrobić?"},
        follow_redirects=False,
    )
    conversation = client.get(response.headers["location"])

    assert response.status_code == 303
    assert "Co mam teraz zrobić?" in conversation.text
    assert "Najpierw dodaj CV." in conversation.text
    assert 'href="/profiles/new"' in conversation.text


def test_global_bielik_api_persists_message_before_background_response(tmp_path):
    class FakeGuideResponder:
        async def respond(self, context):
            assert context["mode"] == "application_guide"
            return AppGuideTurn(response_pl="Gotowa odpowiedź.")

    path = tmp_path / "job_scout.db"
    with TestClient(create_app(path, app_guide_responder=FakeGuideResponder())) as client:
        session = client.get("/api/bielik/sessions/current").json()
        sent = client.post(
            "/api/bielik/messages",
            json={"session_id": session["session"]["session_id"], "content": "Pomóż mi."},
        )
        assert sent.status_code == 200
        assert sent.json()["status"] == "sent"
        for _ in range(30):
            conversation = client.get(
                f"/api/bielik/sessions/{session['session']['session_id']}"
            ).json()
            if conversation["messages"][-1]["role"] == "assistant":
                break
            time.sleep(0.01)
        assert conversation["messages"][-1]["content"] == "Gotowa odpowiedź."
        user_message = conversation["messages"][-2]
        assert user_message["metadata"]["delivery_status"] == "answered"


def test_notification_inbox_reads_offer_ledger_events(tmp_path):
    client, _, _ = client_with_offer(tmp_path)
    today = client.get("/today")
    assert "1 nowych zmian" in today.text
    inbox = client.get("/notifications")
    assert "Nowa oferta: Example AI" in inbox.text
    notification_id = inbox.text.split("/notifications/")[1].split("/read")[0]
    response = client.post(
        f"/notifications/{notification_id}/read", follow_redirects=False
    )
    assert response.status_code == 303
    assert "Przeczytane" in client.get("/notifications").text
    assert "Developer Workspace" not in today.text


def test_offer_score_is_hidden_when_it_cannot_be_attributed_to_current_profile(tmp_path):
    client, path, offer_id = client_with_offer(tmp_path)
    profile = candidate_profile()
    save_user_profile(
        path,
        profile_id=profile.profile_id,
        display_name="Current person",
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    with connect(path) as connection:
        connection.execute(
            "UPDATE offers SET assessment_json = ? WHERE id = ?",
            ('{"final_score": 9.9, "recommendation": "apply"}', offer_id),
        )

    response = client.get("/today")

    assert "9.9" not in response.text
    assert "Jeszcze bez oceny" in response.text


def test_developer_workspace_is_separate_and_links_back_to_product(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    client = TestClient(create_app(path))

    response = client.get("/dev")

    assert response.status_code == 200
    assert "Developer Workspace" in response.text
    assert "Wróć do produktu" in response.text
    assert "Model Lab / Demo" in response.text
    assert "Historia testów" in response.text
    assert "Dzisiaj" not in response.text


def test_pwa_manifest_service_worker_and_local_assets_are_served(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    client = TestClient(create_app(path))

    manifest = client.get("/manifest.webmanifest")
    worker = client.get("/sw.js")
    css = client.get("/static/app.css")
    icon = client.get("/static/icons/scout-mark.svg")

    assert manifest.status_code == worker.status_code == css.status_code == icon.status_code == 200
    assert manifest.json()["start_url"] == "/today"
    assert manifest.json()["display"] == "standalone"
    assert worker.headers["service-worker-allowed"] == "/"
    assert "ai-job-scout-shell-v10" in worker.text
    assert "--forest:" in css.text
    assert "@media (prefers-reduced-motion: reduce)" in css.text


def test_settings_explains_that_bielik_is_managed_by_the_app(tmp_path):
    path = tmp_path / "job_scout.db"
    client = TestClient(create_app(path, career_runtime=FakeCareerRuntime()))

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Bielik w aplikacji" in response.text
    assert "Bielik czeka na uruchomienie" in response.text
    assert 'data-career-runtime' in response.text
    assert 'action="/runtime/career/start"' in response.text
    assert 'action="/runtime/career/stop"' not in response.text


def test_settings_can_start_and_stop_app_managed_bielik(tmp_path):
    path = tmp_path / "job_scout.db"
    runtime = FakeCareerRuntime()
    client = TestClient(create_app(path, career_runtime=runtime))

    started = client.post("/runtime/career/start", follow_redirects=False)
    started_page = client.get(started.headers["location"])
    stopped = client.post("/runtime/career/stop", follow_redirects=False)
    stopped_page = client.get(stopped.headers["location"])

    assert started.status_code == stopped.status_code == 303
    assert started.headers["location"] == "/settings?bielik=started"
    assert "Bielik został uruchomiony" in started_page.text
    assert 'action="/runtime/career/start"' not in started_page.text
    assert 'action="/runtime/career/stop"' in started_page.text
    assert "pamięć modelu zwolniona" in stopped_page.text
    assert runtime.started == runtime.stopped == 1


def test_monitoring_page_explains_safe_disappearance_detection(tmp_path):
    client, _, _ = client_with_offer(tmp_path)
    response = client.get("/monitoring")
    assert response.status_code == 200
    assert "potwierdzoną zmianę" in response.text


def test_profile_upload_and_llm_lab_configuration_are_available(tmp_path, monkeypatch):
    path = tmp_path / "job_scout.db"
    monkeypatch.setattr(
        "job_scout.web.extract_cv_pdf",
        lambda _path: ExtractedCv(text="CV facts for local evaluation", method="ocr", page_count=1),
    )
    client = TestClient(create_app(path))
    created = client.post(
        "/profiles",
        data={
            "display_name": "Test Candidate",
            "target_roles": "Applied AI Engineer, AI Evaluation",
            "location_rule": "Remote from Poland",
            "evidence": "Built local evaluation pipelines\nUses Python daily",
            "ready_for_llm": "on",
        },
        files={"cv_file": ("cv.pdf", b"%PDF-1.4 test", "application/pdf")},
        follow_redirects=False,
    )
    assert created.status_code == 303
    profile_page = client.get(created.headers["location"])
    assert "Sprawdź odczyt CV" in profile_page.text
    profile_id = created.headers["location"].rsplit("/", 1)[-1]
    stored_profile = get_user_profile(path, profile_id)
    document_id = stored_profile["document"]["document_id"]
    assert "Test Candidate" not in client.get("/lab").text
    reviewed = client.post(
        f"/profiles/{profile_id}/documents/{document_id}/review",
        data={
            "corrected_text": "CV facts for local evaluation, checked and corrected",
            "original_language": "en",
            "approve": "on",
        },
        follow_redirects=False,
    )
    assert reviewed.status_code == 303
    assert "Gotowy do porównań" in client.get(reviewed.headers["location"]).text
    lab = client.get("/lab")
    assert lab.status_code == 200
    assert "świadome testy lokalne" in lab.text
    saved_model = client.post(
        "/lab/models",
        data={
            "display_name": "Test Scout",
            "role": "scout",
            "model_path": "models/test.gguf",
            "context_size": "4096",
            "temperature": "0",
            "seed": "42",
        },
        follow_redirects=False,
    )
    assert saved_model.status_code == 303


def test_profile_cv_assets_can_add_and_approve_certification(tmp_path):
    path = tmp_path / "job_scout.db"
    profile = candidate_profile()
    save_user_profile(
        path,
        profile_id=profile.profile_id,
        display_name="Synthetic profile",
        profile_json=profile.model_dump(mode="json"),
        status="ready",
    )
    client = TestClient(create_app(path))

    profile_page = client.get(f"/profiles/{profile.profile_id}")
    assert profile_page.status_code == 200
    assert "Materiały do CV" in profile_page.text
    workspace = client.get("/cv")
    assert workspace.status_code == 200
    assert "CV Workspace" in workspace.text
    assert "Projekt opisany pod CV" in workspace.text
    assert "Skill, osiągnięcie albo bullet" in workspace.text
    created = client.post(
        f"/profiles/{profile.profile_id}/cv-assets/certifications",
        data={
            "name": "AI Agents & Agentic AI",
            "issuer": "Vanderbilt University",
            "note": "Use for agentic AI roles.",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert created.headers["location"] == "/cv"
    with connect(path) as connection:
        row = connection.execute(
            """SELECT fact_id, category, status, usable_for_cv, usable_for_scoring, value_json
            FROM profile_facts WHERE profile_id = ?""",
            (profile.profile_id,),
        ).fetchone()
    assert row["category"] == "certification"
    assert row["status"] == "draft"
    assert row["usable_for_cv"] == 1
    assert row["usable_for_scoring"] == 0
    assert "AI Agents" in row["value_json"]

    reviewed = client.post(
        f"/profiles/{profile.profile_id}/facts/{row['fact_id']}/review",
        data={"decision": "approve"},
        follow_redirects=False,
    )
    assert reviewed.status_code == 303
    assert "AI Agents &amp; Agentic AI" in client.get(reviewed.headers["location"]).text


def test_cv_can_be_added_before_any_questionnaire_and_starts_interview_after_review(
    tmp_path, monkeypatch
):
    path = tmp_path / "job_scout.db"
    monkeypatch.setattr(
        "job_scout.web.extract_cv_pdf",
        lambda _path: ExtractedCv(text="CV text", method="pdf_text", page_count=1),
    )
    client = TestClient(create_app(path))

    created = client.post(
        "/profiles",
        data={"display_name": ""},
        files={"cv_file": ("my_cv.pdf", b"%PDF-1.4 test", "application/pdf")},
        follow_redirects=False,
    )

    assert created.status_code == 303
    profile_id = created.headers["location"].rsplit("/", 1)[-1]
    stored = get_user_profile(path, profile_id)
    assert stored["profile"]["target_roles"] == []
    assert stored["profile"]["evidence"] == []
    document_id = stored["document"]["document_id"]

    reviewed = client.post(
        f"/profiles/{profile_id}/documents/{document_id}/review",
        data={
            "corrected_text": "CV text checked",
            "original_language": "en",
            "approve": "on",
        },
        follow_redirects=False,
    )

    assert reviewed.status_code == 303
    assert get_user_profile(path, profile_id)["status"] == "cv_approved"
    detail = client.get(reviewed.headers["location"])
    assert "CV jest gotowe — poznajmy Twój kierunek" in detail.text
    started = client.post(f"/profiles/{profile_id}/interview/start", follow_redirects=False)
    assert started.status_code == 303


def test_career_interview_is_persistent_adaptive_and_requires_knowledge_review(tmp_path):
    path = tmp_path / "job_scout.db"
    save_user_profile(
        path,
        profile_id="interview-profile",
        display_name="Interview Candidate",
        profile_json={
            "profile_id": "interview-profile",
            "version": 1,
            "target_roles": ["Applied AI Engineer"],
            "evidence": [{"id": "python", "statement": "Uses Python", "source": "CV"}],
            "transferable_skills": [],
            "work_preferences": [],
            "negative_criteria": [],
            "location_rule": "Remote from Poland",
            "approved_at": None,
        },
        status="cv_review",
    )
    save_profile_document(
        path,
        document_id="interview-cv",
        profile_id="interview-profile",
        original_filename="cv.pdf",
        stored_path=tmp_path / "cv.pdf",
        sha256="e" * 64,
        extraction_method="pdf_text",
        extracted_text="Local AI automation project.",
        page_count=1,
    )
    approve_profile_document(
        path,
        profile_id="interview-profile",
        document_id="interview-cv",
        corrected_text="Local AI automation project.",
        original_language="en",
    )

    class FakeResponder:
        async def respond(self, context):
            message = context["recent_messages"][-1]
            return InterviewTurn(
                response="To dobry przykład samodzielności. Dopytam o mierzalny efekt.",
                stage="outside_cv",
                questions=["Co zmieniło się po wdrożeniu?"],
                session_summary="Kandydatka samodzielnie zbudowała automatyzację.",
                knowledge_proposals=[
                    KnowledgeProposal(
                        category="initiative",
                        statement_original="Samodzielnie buduje automatyzacje.",
                        provenance="hypothesis",
                        evidence_quote="Samodzielnie zbudowałam automatyzację",
                        source_message_ids=[message["message_id"]],
                        confidence=0.8,
                    )
                ],
            )

    client = TestClient(create_app(path, interview_responder=FakeResponder()))
    start_page = client.get("/profiles/interview-profile/interview")
    assert "Rozpocznij wywiad" in start_page.text
    assert client.post(
        "/profiles/interview-profile/interview/start", follow_redirects=False
    ).status_code == 303
    session_id = get_active_interview(path, "interview-profile")["session"]["session_id"]
    response = client.post(
        "/profiles/interview-profile/interview/messages",
        data={
            "session_id": session_id,
            "content": "Samodzielnie zbudowałam automatyzację procesu.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Dopytam o mierzalny efekt" in page.text
    assert "Co zmieniło się po wdrożeniu?" in page.text
    assert "Hipoteza Bielika" in page.text
    assert "data-pending-form" in page.text
    assert "aria-live=\"polite\"" in page.text
    interview = get_active_interview(path, "interview-profile")
    entry_id = interview["entries"][0]["entry_id"]
    approved = client.post(
        f"/profiles/interview-profile/knowledge/{entry_id}/approve",
        follow_redirects=False,
    )
    assert approved.status_code == 303
    reviewed_page = client.get(approved.headers["location"])
    assert "Twoja wypowiedź ·\n      approved" in reviewed_page.text
    materialized = client.post(
        "/profiles/interview-profile/interview/knowledge-base",
        follow_redirects=False,
    )
    assert materialized.status_code == 303
    draft_page = client.get(materialized.headers["location"])
    assert "Szkic jest gotowy" in draft_page.text
    finalized = client.post(
        "/profiles/interview-profile/interview/knowledge-base/1/approve",
        follow_redirects=False,
    )
    assert finalized.status_code == 303
    assert "Zatwierdzona i niezależna" in client.get(finalized.headers["location"]).text


def test_career_interview_preserves_user_message_when_bielik_is_unavailable(tmp_path):
    path = tmp_path / "job_scout.db"
    save_user_profile(
        path,
        profile_id="offline-profile",
        display_name="Offline Candidate",
        profile_json={"profile_id": "offline-profile", "version": 1},
        status="cv_review",
    )
    save_profile_document(
        path,
        document_id="offline-cv",
        profile_id="offline-profile",
        original_filename="cv.pdf",
        stored_path=tmp_path / "cv.pdf",
        sha256="a" * 64,
        extraction_method="pdf_text",
        extracted_text="Verified CV.",
        page_count=1,
    )
    approve_profile_document(
        path,
        profile_id="offline-profile",
        document_id="offline-cv",
        corrected_text="Verified CV.",
        original_language="en",
    )

    class OfflineResponder:
        async def respond(self, _context):
            raise LocalLlmError("offline")

    client = TestClient(create_app(path, interview_responder=OfflineResponder()))
    client.post("/profiles/offline-profile/interview/start")
    session_id = get_active_interview(path, "offline-profile")["session"]["session_id"]
    response = client.post(
        "/profiles/offline-profile/interview/messages",
        data={"session_id": session_id, "content": "Ważna odpowiedź do zapisania."},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("error=bielik_unavailable")
    page = client.get(response.headers["location"])
    assert "Twoja odpowiedź została bezpiecznie zapisana" in page.text
    assert "Ważna odpowiedź do zapisania" in page.text


def test_panel_filters_and_updates_status(tmp_path):
    client, path, offer_id = client_with_offer(tmp_path)
    assert f'href="/offers/{offer_id}"' not in client.get("/?q=unrelated").text
    response = client.post(f"/offers/{offer_id}/status/saved", follow_redirects=False)
    assert response.status_code == 303
    with connect(path) as connection:
        status = connection.execute(
            "SELECT application_status FROM offers WHERE id = ?", (offer_id,)
        ).fetchone()[0]
    assert status == "saved"


def test_model_lab_offers_start_action_without_previous_run(tmp_path):
    path = tmp_path / "job_scout.db"
    client = TestClient(create_app(path, demo_run_preparer=lambda mode, key: "unused"))
    response = client.get("/model-lab/demo")
    assert response.status_code == 200
    assert 'action="/model-lab/demo/start"' in response.text
    assert "Historia testów" in response.text


def test_model_lab_can_prepare_a_live_offer_evaluation(tmp_path):
    client, path, offer_id = client_with_offer(tmp_path)

    page = client.get("/model-lab/demo")
    assert 'value="live:' + str(offer_id) + '"' in page.text
    assert "Ocena 1 pobranej oferty" in page.text

    run_id = _prepare_dummy_demo_run(
        project_root=Path.cwd(),
        database_path=path,
        mode="live_single_offer",
        offer_key=f"live:{offer_id}",
    )

    run = get_latest_evaluation_run(path)
    items = list_evaluation_run_items(path, run_id)
    assert run["run_id"] == run_id
    assert run["run_type"] == "live_single_offer"
    assert run["source_id"] == "example"
    assert [item["offer_id"] for item in items] == [offer_id]


def test_run_history_filters_and_links_to_details(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_history_run(path, "completed-history", "completed", "completed")
    _create_history_run(path, "cancelled-history", "cancelled", "cancelled")
    client = TestClient(create_app(path))

    response = client.get("/model-lab/runs?status=completed")

    assert response.status_code == 200
    assert "completed-history" in response.text
    assert "cancelled-history" not in response.text
    assert 'href="/model-lab/runs/completed-history"' in response.text
    assert "Model Lab / Demo" in response.text
    assert "Błędy" in response.text
    assert [run["run_id"] for run in list_pipeline_runs(path, status="completed")] == [
        "completed-history"
    ]


def test_run_detail_shows_status_without_fake_score_and_handles_not_found(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_history_run(path, "cancelled-history", "cancelled", "cancelled")
    client = TestClient(create_app(path))

    detail = client.get("/model-lab/runs/cancelled-history")
    missing = client.get("/model-lab/runs/missing-run")

    assert detail.status_code == 200
    assert "Anulowano" in detail.text
    assert '<div class="score">' not in detail.text
    assert "Model Lab / Demo" in detail.text
    assert missing.status_code == 404


def test_active_run_detail_auto_refreshes(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    client = TestClient(create_app(path))
    _create_history_run(path, "running-history", "running", "running")

    response = client.get("/model-lab/runs/running-history")

    assert response.status_code == 200
    assert "http-equiv='refresh' content='5'" in response.text
    assert "W toku" in response.text


def test_panel_cancel_restart_resume_and_complete_without_reprocessing(tmp_path):
    path = tmp_path / "job_scout.db"
    prepare = _test_run_preparer(path)
    first_started = threading.Event()

    async def first_runner(run_id, _progress):
        with connect(path) as connection:
            first_item = connection.execute(
                """
                SELECT item_id FROM evaluation_run_items
                WHERE run_id = ? ORDER BY item_id LIMIT 1
                """,
                (run_id,),
            ).fetchone()["item_id"]
            connection.execute(
                "UPDATE evaluation_run_items SET status = 'completed' WHERE item_id = ?",
                (first_item,),
            )
            connection.execute(
                """
                UPDATE pipeline_runs
                SET completed_items = 1, current_stage = 'second: evaluator'
                WHERE run_id = ?
                """,
                (run_id,),
            )
        first_started.set()
        await asyncio.Event().wait()

    first_app = create_app(
        path,
        demo_runner=first_runner,
        demo_run_preparer=prepare,
    )
    with TestClient(first_app) as client:
        assert client.post("/model-lab/demo/start", follow_redirects=False).status_code == 303
        _wait_for(first_started)
        assert client.post("/model-lab/demo/start", follow_redirects=False).status_code == 409
        assert client.post("/model-lab/demo/cancel", follow_redirects=False).status_code == 303

    cancelled = get_latest_evaluation_run(path)
    cancelled_items = list_evaluation_run_items(path, cancelled["run_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["current_stage"] == "cancelled"
    assert cancelled["completed_items"] == 1
    assert sorted(item["status"] for item in cancelled_items) == ["cancelled", "completed"]

    second_finished = threading.Event()
    processed_on_resume = []

    async def second_runner(run_id, _progress):
        items = list_evaluation_run_items(path, run_id)
        assert sorted(item["status"] for item in items) == ["completed", "pending"]
        with connect(path) as connection:
            pending = connection.execute(
                """
                SELECT item_id FROM evaluation_run_items
                WHERE run_id = ? AND status = 'pending'
                """,
                (run_id,),
            ).fetchall()
            processed_on_resume.extend(row["item_id"] for row in pending)
            connection.execute(
                """
                UPDATE evaluation_run_items SET status = 'completed'
                WHERE run_id = ? AND status = 'pending'
                """,
                (run_id,),
            )
            connection.execute(
                """
                UPDATE pipeline_runs
                SET status = 'completed', completed_items = 2,
                    current_stage = 'finished', finished_at = ?
                WHERE run_id = ?
                """,
                (datetime.now(UTC).isoformat(), run_id),
            )
        second_finished.set()

    second_app = create_app(
        path,
        demo_runner=second_runner,
        demo_run_preparer=prepare,
    )
    with TestClient(second_app) as client:
        page = client.get("/model-lab/demo")
        assert "Wznów pozostałe" in page.text
        assert client.post("/model-lab/demo/resume", follow_redirects=False).status_code == 303
        _wait_for(second_finished)
        for _ in range(100):
            if get_latest_evaluation_run(path)["status"] == "completed":
                break
            time.sleep(0.02)

    completed = get_latest_evaluation_run(path)
    assert completed["status"] == "completed"
    assert completed["completed_items"] == completed["total_items"] == 2
    assert completed["current_stage"] == "finished"
    assert completed["finished_at"] is not None
    assert len(processed_on_resume) == 1


def test_retry_failed_items_creates_linked_run_without_completed_items(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    _create_history_run(path, "failed-history", "failed", "failed")

    retry_run_id = retry_failed_evaluation_run(path, "failed-history")
    retry_run = list_pipeline_runs(path)[0]
    retry_items = list_evaluation_run_items(path, retry_run_id)

    assert retry_run["run_id"] == retry_run_id
    assert retry_run["parent_run_id"] == "failed-history"
    assert retry_run["total_items"] == 1
    assert [item["status"] for item in retry_items] == ["pending"]


def test_start_demo_invalid_mode_returns_400(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    client = TestClient(create_app(path))

    response = client.post("/model-lab/demo/start", data={"mode": "invalid_mode"})
    assert response.status_code == 400


def test_start_demo_single_offer_missing_key_returns_400(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)
    client = TestClient(create_app(path))

    response = client.post("/model-lab/demo/start", data={"mode": "single_offer_eval"})
    assert response.status_code == 400


def test_start_demo_single_offer_valid_key_creates_run(tmp_path):
    path = tmp_path / "job_scout.db"
    initialize_database(path)

    prepared = {}
    def dummy_preparer(mode: str, offer_key: str | None) -> str:
        prepared["mode"] = mode
        prepared["offer_key"] = offer_key
        return "dummy-run"

    client = TestClient(
        create_app(path, demo_run_preparer=dummy_preparer, demo_runner=lambda *args: None)
    )

    response = client.post(
        "/model-lab/demo/start",
        data={"mode": "single_offer_eval", "offer_key": "Addepto|AI Engineer"},
        follow_redirects=False
    )
    assert response.status_code == 303
    assert prepared.get("mode") == "single_offer_eval"
    assert prepared.get("offer_key") == "Addepto|AI Engineer"
