import httpx

from job_scout.ats_scrapers import CleanJob
from job_scout.domain import FitAssessment, LocationEligibility
from job_scout.telegram_integration import (
    format_fit_alert,
    send_collection_summary,
    send_fit_alert,
    send_test_message,
)


def offer():
    return CleanJob(
        source_id="test",
        company="AI <Company>",
        title="AI Engineer",
        url="https://example.com/jobs/1?a=1&b=2",
        description="Test",
        analysis_text="Test",
        raw_sha256="a" * 64,
        extraction_method="test",
    )


def assessment(**overrides):
    values = {
        "opportunity_score": 9,
        "cv_fit_score": 8,
        "work_conditions_score": 8,
        "final_score": 8.4,
        "confidence": 0.9,
        "location_eligibility": LocationEligibility.ELIGIBLE,
        "applied_ai_score": 8,
        "ai_automation_score": 8,
        "ai_evaluation_score": 8,
        "data_ml_score": 8,
        "ai_research_score": 8,
        "strengths": ["Strong Python"],
        "recommendation": "Apply",
    }
    values.update(overrides)
    return FitAssessment(**values)


def test_alert_is_sent_only_for_the_domain_alert_gate():
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", args[0]))

    assert send_fit_alert(offer(), assessment(), token="token", chat_id="chat", post=post)
    assert len(calls) == 1
    assert "AI &lt;Company&gt;" in calls[0][1]["json"]["text"]
    assert not send_fit_alert(
        offer(), assessment(final_score=7.9), token="token", chat_id="chat", post=post
    )
    assert len(calls) == 1


def test_alert_format_escapes_offer_values():
    message = format_fit_alert(offer(), assessment())
    assert "AI &lt;Company&gt;" in message
    assert "a=1&amp;b=2" in message


def test_test_message_is_a_real_send_message_request_when_configured():
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", args[0]))

    assert send_test_message(token="token", chat_id="chat", post=post)
    assert calls[0][1]["json"]["text"].startswith("[TEST]")


def test_demo_v2_summary_is_sent_without_offer_contents():
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", args[0]))

    assert send_collection_summary(
        {
            "mode": "sample",
            "sources_ok": 18,
            "sources_total": 18,
            "offers_saved": 13,
            "new_raw_versions": 12,
            "offers_marked_unavailable": 4,
        },
        token="token",
        chat_id="chat",
        post=post,
    )
    message = calls[0][1]["json"]["text"]
    assert "Demo v2" in message
    assert "13" in message
    assert "Stanowisko" not in message
