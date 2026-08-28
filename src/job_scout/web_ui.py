"""Shared product shell and design-system helpers for the local web app."""

from __future__ import annotations

from html import escape
from pathlib import Path

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

WEB_ROOT = Path(__file__).parent / "web_assets"
STATIC_ROOT = WEB_ROOT / "static"
TEMPLATE_ROOT = WEB_ROOT / "templates"

_templates = Environment(
    loader=FileSystemLoader(TEMPLATE_ROOT),
    autoescape=select_autoescape(("html", "xml")),
)

PRODUCT_NAV = (
    ("today", "/today", "Start"),
    ("offers", "/", "Oferty"),
    ("bielik", "/chat", "Bielik"),
    ("applications", "/applications", "Aplikacje"),
    ("cv", "/cv", "CV"),
    ("profile", "/profiles", "Profil"),
)

PRODUCT_UTILITY_NAV = (
    ("notifications", "/notifications", "Aktywność"),
    ("settings", "/settings", "Ustawienia"),
)

DEVELOPER_NAV = (
    ("dev", "/dev", "Przegląd"),
    ("models", "/lab", "Modele"),
    ("playground", "/model-lab/demo", "Model Lab / Demo"),
    ("runs", "/model-lab/runs", "Historia testów"),
    ("sources", "/monitoring", "Źródła"),
)


def layout(
    content: str,
    title: str = "AI Job Scout",
    *,
    auto_refresh: bool = False,
    shell: str = "product",
    active: str | None = None,
    page_title: str | None = None,
    eyebrow: str | None = None,
    page_actions: str = "",
) -> HTMLResponse:
    """Render one consistent, local-only product or developer shell."""
    nav_items = DEVELOPER_NAV if shell == "developer" else PRODUCT_NAV
    utility_items = () if shell == "developer" else PRODUCT_UTILITY_NAV
    template = _templates.get_template("base.html")
    html = template.render(
        title=title,
        content=Markup(content),
        auto_refresh=auto_refresh,
        shell=shell,
        active=active,
        page_title=page_title,
        eyebrow=eyebrow,
        page_actions=Markup(page_actions),
        nav_items=nav_items,
        utility_items=utility_items,
    )
    return HTMLResponse(html)


def product_nav(active: str | None = None) -> str:
    """Small reusable nav fragment for legacy content during migration."""
    return _nav(PRODUCT_NAV, active)


def developer_nav(active: str | None = None) -> str:
    return _nav(DEVELOPER_NAV, active)


def _nav(items: tuple[tuple[str, str, str], ...], active: str | None) -> str:
    links = []
    for key, href, label in items:
        current = ' aria-current="page" class="is-active"' if key == active else ""
        links.append(f'<a href="{href}"{current}>{escape(label)}</a>')
    return '<nav class="nav" aria-label="Nawigacja sekcji">' + "".join(links) + "</nav>"
