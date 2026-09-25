"""Source-grounded, requirement-by-requirement comparison with an approved CV profile."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, model_validator

from .ats_scrapers import CleanJob, extract_role_sections
from .domain import CandidateProfile

MATRIX_VERSION = "requirement-matrix-v1"
MAX_REQUIREMENTS = 24
REQUIREMENT_MARKERS = re.compile(
    r"(?:Requirements|Qualifications|Expectations|Your Profile|Your profile|"
    r"Who are you\?|WHO YOU ARE\s*Required|You are the perfect fit, if you|"
    r"What We Expect|What We're Looking For|What We Are Looking For|"
    r"Required Experience & Technical Skills|Required Skills & Capabilities|"
    r"HERE[’']S WHAT YOU[’']LL NEED|Skills:|"
    r"Oczekujemy|Oczekiwania|Oczekiwane kompetencje|Wymagania|"
    r"Realizację projektu ułatwi Ci|You Must Have)\s*:?\s*",
)
OPTIONAL_MARKERS = re.compile(
    r"(?:nice to have|good to have|mile widziane|preferowane|"
    r"preferred qualifications|preferred|you will earn extra points for|"
    r"bonus points(?: if you have)?|dodatkowym atutem będzie)",
    re.IGNORECASE,
)
STOP_MARKERS = re.compile(
    r"(?:what we offer|we offer|oferujemy|proponujemy|co oferujemy|benefits|"
    r"recruitment process|proces rekrutacyjny|about us|how to apply|"
    r"we would like to offer|what you'll love about working here|"
    r"what is in it for you|about data reply|about obi|who are we|"
    r"our mission|twój wkład do projektu|follow us|research indicates|"
    r"zakres realizowanych usług)",
    re.IGNORECASE,
)
ITEM_START = re.compile(
    r"(?=(?:Minimum|Min\.|At least|Experience|Hands-on experience|Strong |"
    r"Practical experience|Familiarity|Knowledge|Proven |Ability to|Degree in|"
    r"Have |Demonstrate |Can communicate|Good understanding|Understanding of|"
    r"Expert-level |Deep knowledge|Deep understanding|Track record|Contributions to|"
    r"\d\+? years? of experience|"
    r"Solid |Good knowledge|Proficiency|Doświadczenie|Znajomość|Znajomości|"
    r"Praktyczna znajomość|Praktyczne doświadczenie|Bardzo dobra znajomość|"
    r"Umiejętność|Wykształcenie|"
    r"Dobra znajomość|Mocne |Samodzielność|Wiedza dotycząca))"
)
SYSTEM_PROMPT = (
    "Compare EACH mandatory job requirement to ONLY the supplied approved candidate facts. "
    "A related AI project does not prove commercial years, production deployment, a named "
    "cloud service, framework or programming stack. Do not infer any unlisted skill. "
    "Use confirmed only when cited facts directly support the requirement; partial only "
    "for specific transferable evidence; unconfirmed when no proof is supplied; contradicted "
    "only when a supplied fact explicitly rules it out. Return one row for EVERY ID, with "
    "candidate fact IDs for confirmed or partial and no IDs for unconfirmed. "
    "The job's AI direction and the candidate's fit are separate questions. Return JSON only."
)


class Requirement(BaseModel):
    id: str
    text: str
    mandatory: bool = True


class RequirementVerdict(BaseModel):
    requirement_id: str
    status: Literal["confirmed", "partial", "unconfirmed", "contradicted"]
    profile_evidence_ids: list[str] = Field(default_factory=list)


class RequirementMatrix(BaseModel):
    rows: list[RequirementVerdict]

    @model_validator(mode="after")
    def unique_requirements(self) -> RequirementMatrix:
        ids = [row.requirement_id for row in self.rows]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate requirement verdict")
        return self


def _source_lines(description: str) -> list[str]:
    decoded = description
    for _ in range(3):
        expanded = unescape(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if "<" in decoded and ">" in decoded:
        soup = BeautifulSoup(decoded, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
            tag.decompose()
        for tag in soup.find_all(["li", "p", "h2", "h3", "h4", "br"]):
            tag.insert_before("\n")
        text = soup.get_text(" ", strip=False)
    else:
        text = decoded
    return [re.sub(r"\s+", " ", line).strip(" •·-\t") for line in text.splitlines()]


def extract_requirements(offer: CleanJob) -> tuple[list[Requirement], bool]:
    """Return complete source lines and a completeness flag; never silently truncate."""
    lines = [line for line in _source_lines(offer.description) if line]
    start = next((i for i, line in enumerate(lines) if REQUIREMENT_MARKERS.search(line)), None)
    if start is None:
        _, requirements = extract_role_sections(offer.description)
        if not requirements:
            return [], False
        lines = [requirements]
        start = 0
    selected: list[Requirement] = []
    optional = False

    def append_pieces(value: str, *, mandatory: bool) -> None:
        value = re.sub(r"\.NET\b", "DOTNET", value, flags=re.IGNORECASE)
        value = re.sub(r"(?<=[.!?])(?=[A-ZĄĆĘŁŃÓŚŹŻ])", "\n", value)
        value = re.sub(
            r",(?=(?:doświadczenia|znajomości|umiejętności|praktycznej|bardzo dobrej)\b)",
            "\n",
            value,
            flags=re.IGNORECASE,
        )
        for piece in ITEM_START.split(value.replace("\n", "\u2022")):
            for part in piece.split("\u2022"):
                part = part.strip(" :;-.•").replace("DOTNET", ".NET")
                if len(part) >= 12:
                    selected.append(
                        Requirement(id=f"req-{len(selected) + 1}", text=part, mandatory=mandatory)
                    )

    for index, line in enumerate(lines[start:]):
        if index == 0:
            marker = REQUIREMENT_MARKERS.search(line)
            if marker:
                line = line[marker.end() :].strip(" :;-")
        if STOP_MARKERS.search(line):
            line = STOP_MARKERS.split(line, maxsplit=1)[0].strip()
            if not line:
                break
        if OPTIONAL_MARKERS.search(line):
            marker = OPTIONAL_MARKERS.search(line)
            assert marker is not None
            before, after = line[: marker.start()], line[marker.end() :]
            append_pieces(before, mandatory=not optional)
            optional = True
            line = after.strip(" :;-")
        append_pieces(line, mandatory=not optional)
        if STOP_MARKERS.search(lines[start + index]):
            break
        if len(selected) > MAX_REQUIREMENTS:
            return selected, False
    return selected, bool(selected) and all(len(item.text) <= 450 for item in selected)


def matrix_input(requirements: list[Requirement], profile: CandidateProfile) -> str:
    return json.dumps(
        {
            "requirements": [item.model_dump() for item in requirements if item.mandatory],
            "candidate_facts": {item.id: item.statement for item in profile.evidence},
        },
        ensure_ascii=False,
    )


def validate_matrix(
    matrix: RequirementMatrix, requirements: list[Requirement], profile: CandidateProfile
) -> None:
    mandatory = {item.id for item in requirements if item.mandatory}
    if {row.requirement_id for row in matrix.rows} != mandatory:
        raise ValueError("matrix does not cover every mandatory requirement")
    valid_facts = {item.id for item in profile.evidence}
    for row in matrix.rows:
        if not set(row.profile_evidence_ids) <= valid_facts:
            raise ValueError("unknown profile evidence ID")
        if row.status in {"confirmed", "partial"} and not row.profile_evidence_ids:
            raise ValueError("positive verdict without candidate evidence")
        if row.status in {"unconfirmed", "contradicted"} and row.profile_evidence_ids:
            raise ValueError("negative verdict has positive evidence")


def summarize_fit(matrix: RequirementMatrix) -> str:
    """Conservative band; missing CV proof is not a claim about actual ability."""
    if not matrix.rows:
        return "needs_review"
    confirmed = sum(row.status == "confirmed" for row in matrix.rows)
    unresolved = sum(row.status in {"unconfirmed", "contradicted"} for row in matrix.rows)
    if not unresolved and confirmed / len(matrix.rows) >= 0.8:
        return "good"
    if confirmed or any(row.status == "partial" for row in matrix.rows):
        return "ambitious"
    return "needs_review"


HARD_TECHNOLOGIES = re.compile(
    r"\b(?:Azure AI Foundry|Azure AI Search|Azure OpenAI|LangGraph|LangChain|"
    r"React|Angular|Kubernetes|Terraform|Databricks|FastAPI|Flask)\b",
    re.IGNORECASE,
)
EXPLICIT_YEARS = re.compile(r"\b([1-9]|1[0-9])\+?\s*(?:lat|years)\b", re.IGNORECASE)


def unconfirmed_hard_requirements(
    offer: CleanJob, profile: CandidateProfile
) -> list[Requirement]:
    """Find explicit CV-proof gaps, never assert that the candidate lacks a skill."""
    requirements, complete = extract_requirements(offer)
    if not complete:
        return []
    facts = " ".join(item.statement for item in profile.evidence).casefold()
    gaps: list[Requirement] = []
    for item in requirements:
        if not item.mandatory:
            continue
        years = EXPLICIT_YEARS.search(item.text)
        named = {match.group().casefold() for match in HARD_TECHNOLOGIES.finditer(item.text)}
        missing_years = bool(years) and not any(
            EXPLICIT_YEARS.search(fact.statement) for fact in profile.evidence
        )
        missing_named = any(term not in facts for term in named)
        if missing_years or missing_named:
            gaps.append(item)
    return gaps
