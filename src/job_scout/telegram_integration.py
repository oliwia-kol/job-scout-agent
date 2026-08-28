"""Small, typed Telegram notifier for genuinely high-confidence fits."""

from __future__ import annotations

from collections.abc import Callable
from html import escape

import httpx

from .ats_scrapers import CleanJob
from .domain import FitAssessment

TelegramPost = Callable[..., httpx.Response]


def send_test_message(
    *,
    token: str | None,
    chat_id: str | None,
    post: TelegramPost = httpx.post,
) -> bool:
    """Send an explicit real connectivity test; automated tests inject a mock post."""
    if not token or not chat_id:
        return False
    try:
        response = post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "[TEST] AI Job Scout: Telegram integration is reachable.",
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


def format_fit_alert(offer: CleanJob, assessment: FitAssessment) -> str:
    """Render a safe, concise HTML alert from verified model output."""
    strengths = "; ".join(assessment.strengths[:2]) or "Brak wyróżnionych atutów"
    return (
        "🚀 <b>Nowe wysokie dopasowanie</b>\n\n"
        f"<b>Stanowisko:</b> {escape(offer.title)}\n"
        f"<b>Firma:</b> {escape(offer.company)}\n"
        f"<b>Wynik:</b> {assessment.final_score:.1f}/10 "
        f"(pewność {assessment.confidence:.0%})\n"
        f"<b>Mocne strony:</b> {escape(strengths)}\n"
        f'<a href="{escape(str(offer.url), quote=True)}">Otwórz ofertę</a>'
    )


def send_fit_alert(
    offer: CleanJob,
    assessment: FitAssessment,
    *,
    token: str | None,
    chat_id: str | None,
    post: TelegramPost = httpx.post,
) -> bool:
    """Send an alert only when the assessment passes the domain safety gate."""
    if not assessment.can_alert or not token or not chat_id:
        return False
    try:
        response = post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": format_fit_alert(offer, assessment),
                "parse_mode": "HTML",
            },
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True


def send_collection_summary(
    summary: dict[str, int | str],
    *,
    token: str | None,
    chat_id: str | None,
    post: TelegramPost = httpx.post,
) -> bool:
    """Send a minimal operational Demo v2 notification without offer content."""
    if not token or not chat_id:
        return False
    text = (
        "◼ <b>AI Job Scout · Demo v2</b>\n\n"
        f"Tryb: <b>{escape(str(summary['mode']))}</b>\n"
        f"Źródła: {summary['sources_ok']}/{summary['sources_total']} OK\n"
        f"Zapisane oferty: {summary['offers_saved']}\n"
        f"Nowe wersje: {summary['new_raw_versions']}\n"
        f"Potwierdzenie niedostępności: {summary['offers_marked_unavailable']}"
    )
    try:
        response = post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return True
