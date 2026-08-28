"""Grounded, reviewable HTML CV tailoring and application packages."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

from .local_llm import LocalLlmClient
from .storage import connect, get_offer, get_user_profile, initialize_database

MAX_HTML_BYTES = 2 * 1024 * 1024
TAILORING_PROMPT_VERSION = "cv-tailoring-qwen-en-v4"
CV_NARRATIVE_INSTRUCTION = (
    "Use confident but calibrated ownership. A personal project may be described as "
    "the candidate's work when the evidence shows that she directs the product, "
    "defines workflows or evaluation, sets acceptance criteria, and validates the "
    "result. Do not add an AI-assistance disclaimer unless it is explicitly requested "
    "or materially relevant in the supplied evidence. Distinguish technology used by "
    "the system from the candidate's demonstrated depth in that technology. Describe "
    "ongoing personal R&D as such; do not imply production deployment, commercial "
    "scale, or expert-level implementation without evidence. Prefer natural, flowing "
    "sentences over defensive caveats or keyword-stuffed claims. For profile summary "
    "edits, preserve the positioning 'physics-trained Applied AI professional'. Keep "
    "curiosity evidence-based by connecting it to looking under the hood, questioning "
    "assumptions, or testing system behavior. Present LLM APIs and local inference as "
    "complementary delivery options; do not position local LLMs as the specialization. "
    "Keep model validation as a differentiating discipline, not the target identity. "
    "When editing validation experience, use domain-specific language for conceptual "
    "soundness, model replication, data verification, statistical testing, critical "
    "interpretation, conclusions, and recommendations; avoid vague phrases such as "
    "'reviewing data' or 'sound basis'. Keep methodology in the scope-of-validation "
    "claim rather than repeating it in the quantitative-analysis claim. In Skills, "
    "place Programming & Data before LLMs & Applied AI and Automation; list coding "
    "agents lower as tools that support the engineering workflow. Use Certificates "
    "& Training for mixed course and certificate credentials, preserve exact verified "
    "credential names, and do not imply that course completion proves production-level "
    "expertise. "
)
FORBIDDEN_TAGS = {"script", "iframe", "object", "embed", "form", "input", "button"}
ALLOWED_INLINE_TAGS = {"strong", "em"}
CERTIFICATION_LIST_TAGS = {"div", "strong", "em"}
MAX_SKILLS_GROWTH_RATIO = 1.2
MAX_SKILLS_EXTRA_CHARS = 80
EDITABLE_SELECTORS = {
    "highlights": ".highlights-bar .hl-value",
    "experience": ".left-col .section .item-desc",
    "profile": ".profile-text",
    "skills": ".skill-items",
}
PdfRenderer = Callable[[str, Path], Awaitable[None]]


class CvTailoringError(ValueError):
    """CV input, evidence, review state, or artifact is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuggestedCvChange(_StrictModel):
    section_id: str = Field(min_length=1)
    anchor_sha256: str = Field(min_length=64, max_length=64)
    current_text: str = Field(min_length=1)
    proposed_html: str = Field(min_length=1, max_length=1600)
    intent: str = Field(min_length=1, max_length=240)
    profile_evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    offer_evidence_ids: list[str] = Field(min_length=1, max_length=6)
    risk_note: str | None = Field(default=None, max_length=240)


class CvSuggestionBatch(_StrictModel):
    suggestions: list[SuggestedCvChange] = Field(max_length=12)


class CvTailoringResponder(Protocol):
    async def respond(self, context: dict) -> CvSuggestionBatch: ...


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_text(content: str) -> str:
    return _sha256_bytes(content.encode("utf-8"))


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:120] or "cv.html"


def _section_title(node: Tag) -> str:
    title = node.select_one(".section-title, h1, h2, h3")
    return title.get_text(" ", strip=True) if title else ""


def _normal_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _certification_html(node: Tag) -> str:
    if "cert-item" not in set(node.get("class") or []):
        raise CvTailoringError("certification item disappeared")
    return str(node).strip()


def _certification_items_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".cert-item")
    if not items:
        raise CvTailoringError("certification list cannot be empty")
    for tag in soup.find_all(True):
        if tag.name not in CERTIFICATION_LIST_TAGS:
            raise CvTailoringError("certification list uses unsupported HTML")
        if tag.name == "div":
            classes = set(tag.get("class") or [])
            if not classes <= {"cert-item", "cert-name", "cert-org"}:
                raise CvTailoringError("certification list uses unsupported HTML")
        tag.attrs = {"class": tag.get("class")} if tag.get("class") else {}
    return [_certification_html(item) for item in items]


def sanitize_master_html(content: bytes) -> tuple[str, list[dict]]:
    if not content or len(content) > MAX_HTML_BYTES:
        raise CvTailoringError("HTML CV must be non-empty and no larger than 2 MB")
    try:
        html = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CvTailoringError("HTML CV must use UTF-8") from exc
    soup = BeautifulSoup(html, "html.parser")
    if soup.html is None or soup.body is None:
        raise CvTailoringError("HTML CV requires html and body elements")
    for tag in list(soup.find_all(FORBIDDEN_TAGS)):
        tag.decompose()
    for meta in list(soup.find_all("meta")):
        if str(meta.get("http-equiv") or "").casefold() == "refresh":
            meta.decompose()
    for link in list(soup.find_all("link")):
        link.decompose()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            name = str(attribute).casefold()
            if name.startswith("on") or name in {"formaction", "srcdoc"}:
                del tag.attrs[attribute]
        for attribute in ("src", "href", "poster"):
            value = tag.get(attribute)
            if isinstance(value, str) and re.match(r"^(?:https?:)?//", value.strip(), re.I):
                del tag.attrs[attribute]
    for style in soup.find_all("style"):
        text = style.string or style.get_text()
        if re.search(r"url\s*\(\s*['\"]?(?:https?:)?//", text, re.I):
            style.string = re.sub(
                r"url\s*\(\s*(['\"]?)(?:https?:)?//.*?\1\s*\)", "none", text,
                flags=re.I,
            )

    sections: list[dict] = []
    counts: dict[str, int] = {}
    for section_kind, selector in EDITABLE_SELECTORS.items():
        for node in soup.select(selector):
            if not isinstance(node, Tag):
                continue
            counts[section_kind] = counts.get(section_kind, 0) + 1
            index = counts[section_kind]
            anchor = f"{section_kind}-{index}"
            node["data-cv-anchor"] = anchor
            current_html = node.decode_contents().strip()
            current_text = node.get_text(" ", strip=True)
            if not current_text:
                continue
            heading_node = node.find_parent("section")
            heading = ""
            if heading_node:
                heading = _section_title(heading_node)
            sections.append(
                {
                    "anchor": anchor,
                    "section_kind": section_kind,
                    "heading": heading or section_kind.title(),
                    "block_index": index,
                    "current_html": current_html,
                    "current_text": current_text,
                    "content_sha256": _sha256_text(current_html),
                    "editable": True,
                }
            )
    for node in soup.select("section"):
        if not isinstance(node, Tag) or not node.select(".cert-item"):
            continue
        if _section_title(node).casefold() != "certifications":
            continue
        counts["certifications"] = counts.get("certifications", 0) + 1
        anchor = f"certifications-{counts['certifications']}"
        node["data-cv-anchor"] = anchor
        items = [_certification_html(item) for item in node.select(".cert-item")]
        current_html = "".join(items)
        current_text = " | ".join(
            item.get_text(" ", strip=True) for item in node.select(".cert-item")
        )
        sections.append(
            {
                "anchor": anchor,
                "section_kind": "certifications",
                "heading": "Certifications",
                "block_index": counts["certifications"],
                "current_html": current_html,
                "current_text": current_text,
                "content_sha256": _sha256_text(current_html),
                "editable": True,
            }
        )
    if not sections or not {"profile", "experience", "skills"} <= {
        item["section_kind"] for item in sections
    }:
        raise CvTailoringError("HTML CV does not contain the required editable sections")
    return str(soup), sections


def import_master_cv(
    database_path: Path,
    *,
    storage_root: Path,
    profile_id: str,
    filename: str,
    content: bytes,
) -> str:
    initialize_database(database_path)
    if not get_user_profile(database_path, profile_id):
        raise CvTailoringError("profile not found")
    safe_html, sections = sanitize_master_html(content)
    document_id = f"cv-{uuid.uuid4().hex}"
    document_root = storage_root / document_id
    document_root.mkdir(parents=True, exist_ok=False)
    original_path = document_root / _safe_filename(filename)
    safe_path = document_root / "master.safe.html"
    original_path.write_bytes(content)
    safe_path.write_text(safe_html, encoding="utf-8")
    timestamp = _timestamp()
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT document_id FROM cv_documents WHERE profile_id = ?
                AND kind = 'master' AND status != 'superseded'""",
                (profile_id,),
            ).fetchone()
            if existing:
                raise CvTailoringError("profile already has an active master CV")
            connection.execute(
                """INSERT INTO cv_documents (
                document_id, profile_id, kind, language, original_filename, stored_path,
                safe_path, sha256, status, created_at)
                VALUES (?, ?, 'master', 'en', ?, ?, ?, ?, 'draft', ?)""",
                (
                    document_id, profile_id, filename, str(original_path), str(safe_path),
                    _sha256_bytes(content), timestamp,
                ),
            )
            for item in sections:
                connection.execute(
                    """INSERT INTO cv_sections (
                    section_id, document_id, section_kind, heading, anchor, block_index,
                    current_html, current_text, content_sha256, editable, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        f"{document_id}:{item['anchor']}", document_id,
                        item["section_kind"], item["heading"], item["anchor"],
                        item["block_index"], item["current_html"], item["current_text"],
                        item["content_sha256"], timestamp,
                    ),
                )
    except Exception:
        shutil.rmtree(document_root, ignore_errors=True)
        raise
    return document_id


def replace_master_cv(
    database_path: Path,
    *,
    storage_root: Path,
    profile_id: str,
    filename: str,
    content: bytes,
) -> str:
    """Create a new master version while preserving the previous one as history."""
    initialize_database(database_path)
    if not get_user_profile(database_path, profile_id):
        raise CvTailoringError("profile not found")
    safe_html, sections = sanitize_master_html(content)
    document_id = f"cv-{uuid.uuid4().hex}"
    document_root = storage_root / document_id
    document_root.mkdir(parents=True, exist_ok=False)
    original_path = document_root / _safe_filename(filename)
    safe_path = document_root / "master.safe.html"
    original_path.write_bytes(content)
    safe_path.write_text(safe_html, encoding="utf-8")
    timestamp = _timestamp()
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """SELECT document_id FROM cv_documents WHERE profile_id = ?
                AND kind = 'master' AND status != 'superseded'""",
                (profile_id,),
            ).fetchone()
            if not previous:
                raise CvTailoringError("active master CV not found")
            previous_id = previous["document_id"]
            connection.execute(
                "UPDATE cv_documents SET status = 'superseded' WHERE document_id = ?",
                (previous_id,),
            )
            connection.execute(
                """INSERT INTO cv_documents (
                document_id, profile_id, parent_document_id, kind, language,
                original_filename, stored_path, safe_path, sha256, status, created_at)
                VALUES (?, ?, ?, 'master', 'en', ?, ?, ?, ?, 'draft', ?)""",
                (
                    document_id, profile_id, previous_id, filename, str(original_path),
                    str(safe_path), _sha256_bytes(content), timestamp,
                ),
            )
            for item in sections:
                connection.execute(
                    """INSERT INTO cv_sections (
                    section_id, document_id, section_kind, heading, anchor, block_index,
                    current_html, current_text, content_sha256, editable, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (
                        f"{document_id}:{item['anchor']}", document_id,
                        item["section_kind"], item["heading"], item["anchor"],
                        item["block_index"], item["current_html"], item["current_text"],
                        item["content_sha256"], timestamp,
                    ),
                )
    except Exception:
        shutil.rmtree(document_root, ignore_errors=True)
        raise
    return document_id


def approve_master_mapping(database_path: Path, document_id: str) -> None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM cv_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if not row or row["status"] != "draft":
            raise CvTailoringError("draft master CV not found")
        connection.execute(
            "UPDATE cv_documents SET status = 'mapped', approved_at = ? WHERE document_id = ?",
            (_timestamp(), document_id),
        )


def get_cv_document(database_path: Path, document_id: str) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM cv_documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if not row:
            return None
        sections = connection.execute(
            "SELECT * FROM cv_sections WHERE document_id = ? ORDER BY section_kind, block_index",
            (document_id,),
        ).fetchall()
    return {**dict(row), "sections": [dict(item) for item in sections]}


def get_active_master_cv(database_path: Path, profile_id: str) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """SELECT document_id FROM cv_documents WHERE profile_id = ? AND kind = 'master'
            AND status != 'superseded' ORDER BY created_at DESC LIMIT 1""",
            (profile_id,),
        ).fetchone()
    return get_cv_document(database_path, row["document_id"]) if row else None


def _offer_hash(offer: dict) -> str:
    return str(offer.get("current_content_sha256") or _sha256_text(
        json.dumps(offer.get("offer") or {}, ensure_ascii=False, sort_keys=True)
    ))


def start_tailoring_session(
    database_path: Path, *, profile_id: str, offer_id: int, document_id: str
) -> str:
    profile = get_user_profile(database_path, profile_id)
    offer = get_offer(database_path, offer_id)
    document = get_cv_document(database_path, document_id)
    if not profile or profile["status"] != "ready":
        raise CvTailoringError("ready profile is required")
    if not offer:
        raise CvTailoringError("offer not found")
    if not document or document["profile_id"] != profile_id or document["status"] != "mapped":
        raise CvTailoringError("approved master CV is required")
    session_id = f"tailor-{uuid.uuid4().hex}"
    timestamp = _timestamp()
    with connect(database_path) as connection:
        connection.execute(
            """INSERT INTO cv_tailoring_sessions (
            session_id, profile_id, profile_version, offer_id, offer_content_sha256,
            document_id, prompt_version, model_config_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '{}', 'draft', ?, ?)""",
            (
                session_id, profile_id, int(profile["current_version"]), offer_id,
                _offer_hash(offer), document_id, TAILORING_PROMPT_VERSION,
                timestamp, timestamp,
            ),
        )
        connection.execute(
            "UPDATE offers SET application_status = 'applying' WHERE id = ?", (offer_id,)
        )
        _record_event(
            connection, offer_id=offer_id, session_id=session_id,
            event_type="tailoring_started", detail={"document_id": document_id},
        )
    return session_id


def _record_event(
    connection, *, offer_id: int, event_type: str, detail: dict,
    session_id: str | None = None, package_id: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO application_events (
        event_id, offer_id, session_id, package_id, event_type, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            f"app-event-{uuid.uuid4().hex}", offer_id, session_id, package_id,
            event_type, json.dumps(detail, ensure_ascii=False, sort_keys=True), _timestamp(),
        ),
    )


def get_tailoring_session(database_path: Path, session_id: str) -> dict:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM cv_tailoring_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise CvTailoringError("tailoring session not found")
        suggestions = connection.execute(
            """SELECT s.*, c.anchor, c.section_kind, c.heading FROM cv_suggestions s
            JOIN cv_sections c ON c.section_id = s.section_id
            WHERE s.session_id = ? ORDER BY c.section_kind, c.block_index""",
            (session_id,),
        ).fetchall()
    return {**dict(row), "suggestions": [dict(item) for item in suggestions]}


def _mark_session_stale(database_path: Path, session_id: str, reason: str) -> None:
    timestamp = _timestamp()
    with connect(database_path) as connection:
        connection.execute(
            """UPDATE cv_tailoring_sessions SET status = 'stale', error = ?, updated_at = ?
            WHERE session_id = ? AND status != 'packaged'""",
            (reason, timestamp, session_id),
        )
        connection.execute(
            """UPDATE cv_suggestions SET status = 'stale'
            WHERE session_id = ? AND status = 'proposed'""",
            (session_id,),
        )


def build_tailoring_context(database_path: Path, session_id: str) -> dict:
    session = get_tailoring_session(database_path, session_id)
    profile = get_user_profile(database_path, session["profile_id"])
    offer = get_offer(database_path, int(session["offer_id"]))
    document = get_cv_document(database_path, session["document_id"])
    if not profile or not offer or not document:
        _mark_session_stale(database_path, session_id, "tailoring inputs are missing")
        raise CvTailoringError("tailoring inputs are missing")
    if int(profile["current_version"]) != int(session["profile_version"]):
        _mark_session_stale(database_path, session_id, "profile changed")
        raise CvTailoringError("profile changed; start a new tailoring session")
    if _offer_hash(offer) != session["offer_content_sha256"]:
        _mark_session_stale(database_path, session_id, "offer changed")
        raise CvTailoringError("offer changed; start a new tailoring session")
    if document["status"] != "mapped":
        _mark_session_stale(database_path, session_id, "master CV changed")
        raise CvTailoringError("master CV changed; start a new tailoring session")
    with connect(database_path) as connection:
        facts = connection.execute(
            """SELECT fact_id, category, value_json, source_quote FROM profile_facts
            WHERE profile_id = ? AND status = 'approved' AND usable_for_cv = 1""",
            (session["profile_id"],),
        ).fetchall()
        knowledge = connection.execute(
            """SELECT entry_id, category, statement_original, canonical_english, evidence_json
            FROM career_knowledge_entries WHERE profile_id = ? AND status = 'approved'""",
            (session["profile_id"],),
        ).fetchall()
        evidence = connection.execute(
            """SELECT evidence_id, section_kind, value_json, source_quote
            FROM offer_section_evidence WHERE offer_id = ? AND content_sha256 = ?""",
            (session["offer_id"], session["offer_content_sha256"]),
        ).fetchall()
    profile_evidence = [
        {**dict(item), "value": json.loads(item["value_json"])} for item in facts
    ]
    context = {
        "session_id": session_id,
        "cv_language": "en",
        "editable_sections": [
            {
                "section_id": item["section_id"], "section_kind": item["section_kind"],
                "heading": item["heading"], "current_text": item["current_text"],
                "current_html": item["current_html"], "anchor_sha256": item["content_sha256"],
            }
            for item in document["sections"] if item["editable"]
        ],
        "profile_evidence": profile_evidence,
        "cv_assets": [
            item for item in profile_evidence
            if item["category"] in {
                "certification",
                "project",
                "skill",
                "achievement",
                "experience_bullet",
                "profile_summary_variant",
                "keyword",
            }
        ],
        "career_knowledge": [dict(item) for item in knowledge],
        "offer": {
            "company": offer["company"], "title": offer["title"],
            "url": offer["job_url"], "content_sha256": session["offer_content_sha256"],
        },
        "offer_evidence": [
            {**dict(item), "value": json.loads(item["value_json"])} for item in evidence
        ],
    }
    return context


def sanitize_inline_html(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in list(soup.find_all(True)):
        if tag.name not in ALLOWED_INLINE_TAGS:
            tag.unwrap()
        else:
            tag.attrs.clear()
    result = "".join(str(item) for item in soup.contents).strip()
    text = BeautifulSoup(result, "html.parser").get_text(" ", strip=True)
    if not text:
        raise CvTailoringError("suggested text cannot be empty")
    return result


def sanitize_suggestion_html(section_kind: str, value: str) -> str:
    if section_kind == "certifications":
        return "".join(_certification_items_from_html(value))
    return sanitize_inline_html(value)


def _certification_asset_text(asset: dict) -> str:
    value = asset.get("value")
    if not isinstance(value, dict) or not value.get("name"):
        return ""
    return " ".join(
        str(item).strip()
        for item in (value.get("name"), value.get("issuer"))
        if str(item or "").strip()
    )


def _validate_certification_selection(
    current_html: str, proposed_html: str, cv_assets: list[dict] | None = None
) -> None:
    current_items = _certification_items_from_html(current_html)
    proposed_items = _certification_items_from_html(proposed_html)
    current_pool = {
        _normal_text(BeautifulSoup(item, "html.parser").get_text(" ", strip=True))
        for item in current_items
    }
    for asset in cv_assets or []:
        if asset.get("category") == "certification":
            text = _certification_asset_text(asset)
            if text:
                current_pool.add(_normal_text(text))
    proposed_pool = {
        _normal_text(BeautifulSoup(item, "html.parser").get_text(" ", strip=True))
        for item in proposed_items
    }
    if len(proposed_pool) != len(proposed_items) or proposed_pool - current_pool:
        raise CvTailoringError("certification suggestions must use only the existing pool")


def validate_suggestion(context: dict, suggestion: SuggestedCvChange) -> None:
    sections = {item["section_id"]: item for item in context["editable_sections"]}
    section = sections.get(suggestion.section_id)
    if not section:
        raise CvTailoringError("suggestion targets an unknown or protected CV block")
    if suggestion.anchor_sha256 != section["anchor_sha256"]:
        raise CvTailoringError("suggestion anchor is stale")
    if suggestion.current_text != section["current_text"]:
        raise CvTailoringError("suggestion current text is not exact")
    profile_ids = {item["fact_id"] for item in context["profile_evidence"]} | {
        item["entry_id"] for item in context["career_knowledge"]
    }
    offer_ids = {item["evidence_id"] for item in context["offer_evidence"]}
    if set(suggestion.profile_evidence_ids) - profile_ids:
        raise CvTailoringError("suggestion uses unapproved profile evidence")
    if set(suggestion.offer_evidence_ids) - offer_ids:
        raise CvTailoringError("suggestion uses invalid offer evidence")
    if section.get("section_kind") == "certifications":
        _validate_certification_selection(
            section["current_html"], suggestion.proposed_html, context.get("cv_assets", [])
        )
        return
    proposed_soup = BeautifulSoup(
        sanitize_inline_html(suggestion.proposed_html), "html.parser"
    )
    proposed = proposed_soup.get_text(" ", strip=True)
    if section.get("section_kind") == "skills":
        allowed = max(
            int(len(section["current_text"]) * MAX_SKILLS_GROWTH_RATIO),
            len(section["current_text"]) + MAX_SKILLS_EXTRA_CHARS,
        )
        if len(proposed) > allowed:
            raise CvTailoringError("skills suggestion is too long for the CV layout")
    evidence_text = " ".join(
        [section["current_text"]]
        + [json.dumps(item, ensure_ascii=False) for item in context["profile_evidence"]]
        + [json.dumps(item, ensure_ascii=False) for item in context["career_knowledge"]]
    )
    new_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", proposed)) - set(
        re.findall(r"\b\d+(?:[.,]\d+)?%?\b", evidence_text)
    )
    if new_numbers:
        raise CvTailoringError("suggestion introduces unsupported numbers")


class QwenCvTailoringResponder:
    def __init__(self, client: LocalLlmClient, model: str) -> None:
        self.client = client
        self.model = model

    async def respond(self, context: dict) -> CvSuggestionBatch:
        response = await self.client.structured_completion(
            model=self.model,
            system_prompt=(
                "You propose concise English CV edits for one job. Never change employers, "
                "job titles, dates, education, publications, contact details, GDPR/RODO clauses, "
                "or factual metrics. Reframe editable descriptions only from supplied CV text "
                "and approved profile evidence. Keep skills compact: do not expand the visual "
                "size of the skills block just to add keywords. Certifications are selectable "
                "from the current master CV or approved cv_assets only: for "
                "section_kind=certifications you may remove, add, swap, or reorder cert-item "
                "blocks, but never rewrite, rename, or invent a certification. Use approved "
                "project, skill, achievement, bullet, summary, and keyword cv_assets only when "
                "they are relevant to the offer and do not expand the layout. Every change must "
                "cite at least one exact offer evidence_id. Do not invent skills, scope, "
                "outcomes, seniority, or numbers. "
                + CV_NARRATIVE_INSTRUCTION
                + "Return no more than eight high-value changes. proposed_html may contain only "
                "plain text, <strong>, and <em>, except certification suggestions which must "
                "return only existing <div class=\"cert-item\"> blocks."
            ),
            user_prompt=json.dumps(context, ensure_ascii=False, sort_keys=True),
            schema=CvSuggestionBatch,
            schema_name=TAILORING_PROMPT_VERSION,
            seed=42,
            temperature=0,
            max_tokens=1800,
            strict_schema=True,
            instruction_language="en",
            validate=lambda batch: [
                validate_suggestion(context, item) for item in batch.suggestions
            ],
        )
        return response.value


def persist_suggestion_batch(
    database_path: Path, *, session_id: str, batch: CvSuggestionBatch
) -> None:
    context = build_tailoring_context(database_path, session_id)
    timestamp = _timestamp()
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        session = connection.execute(
            "SELECT status FROM cv_tailoring_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not session or session["status"] not in {"draft", "failed"}:
            raise CvTailoringError("session is not ready for suggestions")
        for suggestion in batch.suggestions:
            validate_suggestion(context, suggestion)
            section = next(
                item for item in context["editable_sections"]
                if item["section_id"] == suggestion.section_id
            )
            connection.execute(
                """INSERT INTO cv_suggestions (
                suggestion_id, session_id, section_id, anchor_sha256, current_html,
                current_text, proposed_html, intent, profile_evidence_ids_json,
                offer_evidence_ids_json, risk_note, created_at)
                SELECT ?, ?, section_id, ?, current_html, current_text, ?, ?, ?, ?, ?, ?
                FROM cv_sections WHERE section_id = ?""",
                (
                    f"cv-suggestion-{uuid.uuid4().hex}", session_id,
                    suggestion.anchor_sha256,
                    sanitize_suggestion_html(section["section_kind"], suggestion.proposed_html),
                    suggestion.intent,
                    json.dumps(suggestion.profile_evidence_ids, ensure_ascii=False),
                    json.dumps(suggestion.offer_evidence_ids, ensure_ascii=False),
                    suggestion.risk_note, timestamp, section["section_id"],
                ),
            )
        connection.execute(
            """UPDATE cv_tailoring_sessions SET status = 'review', error = NULL,
            model_config_json = ?, updated_at = ? WHERE session_id = ?""",
            (json.dumps({"model": "qwen", "seed": 42, "temperature": 0}), timestamp, session_id),
        )


def review_suggestion(
    database_path: Path, *, suggestion_id: str, decision: str, edited_html: str | None = None
) -> None:
    if decision not in {"accepted", "rejected"}:
        raise CvTailoringError("decision must be accepted or rejected")
    with connect(database_path) as connection:
        initial = connection.execute(
            """SELECT s.*, c.section_kind FROM cv_suggestions s
            JOIN cv_sections c ON c.section_id = s.section_id
            WHERE s.suggestion_id = ?""",
            (suggestion_id,),
        ).fetchone()
    if not initial:
        raise CvTailoringError("reviewable suggestion not found")
    reviewed_html = (
        sanitize_suggestion_html(initial["section_kind"], edited_html) if edited_html else None
    )
    if decision == "accepted" and reviewed_html and initial["section_kind"] == "certifications":
        _validate_certification_selection(initial["current_html"], reviewed_html)
    if decision == "accepted" and reviewed_html:
        reviewed_text = BeautifulSoup(reviewed_html, "html.parser").get_text(" ", strip=True)
        known_text = f"{initial['current_text']} {initial['proposed_html']}"
        new_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", reviewed_text)) - set(
            re.findall(r"\b\d+(?:[.,]\d+)?%?\b", known_text)
        )
        if new_numbers:
            raise CvTailoringError("manual edit introduces unsupported numbers")
    with connect(database_path) as connection:
        row = connection.execute(
            """SELECT s.*, c.content_sha256 FROM cv_suggestions s JOIN cv_sections c
            ON c.section_id = s.section_id WHERE suggestion_id = ?""",
            (suggestion_id,),
        ).fetchone()
        if not row or row["status"] not in {"proposed", "accepted", "rejected"}:
            raise CvTailoringError("reviewable suggestion not found")
        if row["anchor_sha256"] != row["content_sha256"]:
            raise CvTailoringError("suggestion is stale")
        connection.execute(
            """UPDATE cv_suggestions SET status = ?, edited_html = ?, reviewed_at = ?
            WHERE suggestion_id = ?""",
            (
                decision,
                reviewed_html if decision == "accepted" else None,
                _timestamp(),
                suggestion_id,
            ),
        )
        remaining = connection.execute(
            """SELECT COUNT(*) FROM cv_suggestions WHERE session_id = ?
            AND status = 'proposed'""",
            (row["session_id"],),
        ).fetchone()[0]
        if remaining == 0:
            connection.execute(
                """UPDATE cv_tailoring_sessions SET status = 'ready', updated_at = ?
                WHERE session_id = ?""",
                (_timestamp(), row["session_id"]),
            )


def compose_tailored_html(database_path: Path, session_id: str) -> str:
    session = get_tailoring_session(database_path, session_id)
    document = get_cv_document(database_path, session["document_id"])
    if not document:
        raise CvTailoringError("master CV not found")
    soup = BeautifulSoup(Path(document["safe_path"]).read_text(encoding="utf-8"), "html.parser")
    sections = {item["section_id"]: item for item in document["sections"]}
    for suggestion in session["suggestions"]:
        if suggestion["status"] != "accepted":
            continue
        section = sections.get(suggestion["section_id"])
        if not section or section["content_sha256"] != suggestion["anchor_sha256"]:
            raise CvTailoringError("accepted suggestion is stale")
        node = soup.select_one(f'[data-cv-anchor="{section["anchor"]}"]')
        if not node:
            raise CvTailoringError("CV anchor disappeared")
        replacement = BeautifulSoup(
            suggestion["edited_html"] or suggestion["proposed_html"], "html.parser"
        )
        if section["section_kind"] == "certifications":
            proposed_items = _certification_items_from_html(
                suggestion["edited_html"] or suggestion["proposed_html"]
            )
            for item in list(node.select(".cert-item")):
                item.decompose()
            insertion_point = node.select_one(".cert-footnote")
            for item_html in proposed_items:
                item = BeautifulSoup(item_html, "html.parser").select_one(".cert-item")
                if not item:
                    raise CvTailoringError("accepted certification suggestion is invalid")
                if insertion_point:
                    insertion_point.insert_before(item)
                else:
                    node.append(item)
            continue
        node.clear()
        for child in list(replacement.contents):
            node.append(child)
    return str(soup)


def application_note(database_path: Path, session_id: str) -> str:
    session = get_tailoring_session(database_path, session_id)
    offer = get_offer(database_path, int(session["offer_id"]))
    if not offer:
        raise CvTailoringError("offer not found")
    accepted = [item for item in session["suggestions"] if item["status"] == "accepted"]
    lines = [
        f"# {offer['company']} — {offer['title']}", "",
        f"Oferta: {offer['job_url']}", "",
        "## Argumenty użyte w CV", "",
    ]
    for item in accepted:
        text = BeautifulSoup(
            item["edited_html"] or item["proposed_html"], "html.parser"
        ).get_text(" ", strip=True)
        lines.append(f"- {text}")
    if not accepted:
        lines.append("- Brak zaakceptowanych zmian; użyto mastera bez modyfikacji.")
    lines.extend(
        [
            "",
            "## Do sprawdzenia przed wysłaniem",
            "",
            "- Widełki i warunki umowy.",
            "- Aktualność oferty i wymagany język aplikacji.",
            "- Pola dodatkowe formularza ATS.",
            "",
        ]
    )
    return "\n".join(lines)


def prepare_print_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.head is None:
        raise CvTailoringError("tailored HTML has no head")
    style = soup.new_tag("style")
    style["data-cv-print-contract"] = "a4-v1"
    style.string = """
    @page { size: A4; margin: 0; }
    @media print {
      html, body { width: 210mm !important; margin: 0 !important; padding: 0 !important; }
      .resume-page {
        width: 210mm !important; min-height: 0 !important; height: auto !important;
        margin: 0 !important; padding: 11mm 10mm 7mm 10mm !important;
        box-sizing: border-box !important;
      }
      .main-content { gap: 13px !important; }
      .section { margin-bottom: 12px !important; }
      .timeline-item { margin-bottom: 10px !important; }
      #education-section, #research-item {
        break-before: auto !important; page-break-before: auto !important;
        margin-top: 0 !important; padding-top: 0 !important;
      }
      .gdpr { margin-top: 12px !important; padding-top: 8px !important; }
    }
    """
    soup.head.append(style)
    return str(soup)


async def render_pdf_with_playwright(html: str, output_path: Path) -> None:
    from playwright.async_api import async_playwright

    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.route(
            re.compile(r"^https?://"),
            lambda route: route.abort(),
        )
        await page.set_content(html, wait_until="load")
        await page.pdf(
            path=str(output_path),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        await browser.close()


def validate_pdf_artifact(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size < 10_000:
        raise CvTailoringError("PDF export is missing or unexpectedly small")
    reader = PdfReader(path)
    page_count = len(reader.pages)
    if page_count < 1 or page_count > 2:
        raise CvTailoringError(f"PDF must have one or two pages, got {page_count}")
    page_sizes = [
        (round(float(page.mediabox.width), 1), round(float(page.mediabox.height), 1))
        for page in reader.pages
    ]
    if any(abs(width - 595.3) > 2 or abs(height - 841.9) > 2 for width, height in page_sizes):
        raise CvTailoringError("PDF pages must use A4 dimensions")
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    if len(text) < 500:
        raise CvTailoringError("PDF does not contain enough selectable text")
    return {
        "page_count": page_count,
        "page_sizes": page_sizes,
        "selectable_characters": len(text),
        "a4": True,
    }


async def build_application_package(
    database_path: Path,
    *,
    session_id: str,
    storage_root: Path,
    renderer: PdfRenderer = render_pdf_with_playwright,
) -> str:
    session = get_tailoring_session(database_path, session_id)
    if session["status"] not in {"ready", "review"}:
        raise CvTailoringError("tailoring session is not ready for packaging")
    if any(item["status"] == "proposed" for item in session["suggestions"]):
        raise CvTailoringError("review every suggestion before packaging")
    # Re-check the immutable input snapshots immediately before creating an artifact.
    # A session prepared from an older profile or offer must never silently ship.
    build_tailoring_context(database_path, session_id)
    offer = get_offer(database_path, int(session["offer_id"]))
    if not offer:
        raise CvTailoringError("offer not found")
    html = prepare_print_html(compose_tailored_html(database_path, session_id))
    if re.search(r"(?:src|href)\s*=\s*['\"](?:https?:)?//", html, re.I):
        raise CvTailoringError("tailored HTML contains an external resource")
    package_id = f"application-package-{uuid.uuid4().hex}"
    package_root = storage_root / package_id
    package_root.mkdir(parents=True, exist_ok=False)
    stem = _safe_filename(
        f"Candidate_CV_{offer['company']}_{offer['title']}_{datetime.now().date().isoformat()}"
    )
    html_path = package_root / f"{stem}.html"
    pdf_path = package_root / f"{stem}.pdf"
    note_path = package_root / f"{stem}_notatka.md"
    html_path.write_text(html, encoding="utf-8")
    note_path.write_text(application_note(database_path, session_id), encoding="utf-8")
    try:
        await renderer(html, pdf_path)
        validation = validate_pdf_artifact(pdf_path)
        timestamp = _timestamp()
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO application_packages (
                package_id, session_id, offer_id, html_path, html_sha256, pdf_path,
                pdf_sha256, note_path, note_sha256, page_count, validation_json,
                status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)""",
                (
                    package_id, session_id, session["offer_id"], str(html_path),
                    _sha256_bytes(html_path.read_bytes()), str(pdf_path),
                    _sha256_bytes(pdf_path.read_bytes()), str(note_path),
                    _sha256_bytes(note_path.read_bytes()), validation["page_count"],
                    json.dumps(validation, ensure_ascii=False, sort_keys=True), timestamp,
                ),
            )
            connection.execute(
                """UPDATE cv_tailoring_sessions SET status = 'packaged', updated_at = ?
                WHERE session_id = ?""",
                (timestamp, session_id),
            )
            _record_event(
                connection, offer_id=int(session["offer_id"]), session_id=session_id,
                package_id=package_id, event_type="package_created",
                detail={"pdf_sha256": _sha256_bytes(pdf_path.read_bytes())},
            )
    except Exception as exc:
        shutil.rmtree(package_root, ignore_errors=True)
        if isinstance(exc, CvTailoringError):
            raise
        raise CvTailoringError(f"PDF export failed: {exc}") from exc
    return package_id


def get_application_package(database_path: Path, package_id: str) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM application_packages WHERE package_id = ?", (package_id,)
        ).fetchone()
    return dict(row) if row else None


def record_package_event(
    database_path: Path, *, package_id: str, event_type: str, detail: dict | None = None
) -> None:
    if event_type not in {"pdf_downloaded", "offer_opened", "applied"}:
        raise CvTailoringError("invalid package event")
    package = get_application_package(database_path, package_id)
    if not package:
        raise CvTailoringError("application package not found")
    with connect(database_path) as connection:
        _record_event(
            connection, offer_id=int(package["offer_id"]),
            session_id=package["session_id"], package_id=package_id,
            event_type=event_type, detail=detail or {},
        )
        if event_type == "applied":
            connection.execute(
                "UPDATE offers SET application_status = 'applied' WHERE id = ?",
                (package["offer_id"],),
            )
