"""Official career-site adapters and deterministic clean JSON extraction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from abc import ABC, abstractmethod
from html import unescape
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, HttpUrl

from .sources import SourceConfig

USER_AGENT = "AI-Job-Scout/0.1 (private local career monitor)"
JOB_MARKERS = (
    "/job/",
    "/jobs/",
    "/position/",
    "/offer/",
    "/oferta/",
    "/oferty-pracy/",
    "/ogloszenie",
    "offer.aspx",
)
REJECT_MARKERS = (
    "privacy",
    "policy",
    "cookie",
    "login",
    "benefits",
    "culture",
    "contact",
    "/locations/",
)
ATS_HOSTS = (
    "ashbyhq.com",
    "greenhouse.io",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "eightfold.ai",
    "explore.jobs.netflix.net",
    "ecruiter.pl",
    "erecruiter.pl",
    "lever.co",
)


class SourceError(RuntimeError):
    """Error included in source health reporting."""


class DiscoveredJob(BaseModel):
    source_id: str
    company: str
    external_id: str | None = None
    title: str
    url: HttpUrl
    location_hint: str | None = None
    detail_api_url: HttpUrl | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CleanJob(BaseModel):
    source_id: str
    company: str
    external_id: str | None = None
    title: str
    url: HttpUrl
    locations: list[str] = Field(default_factory=list)
    description: str
    analysis_text: str
    removed_characters: int = 0
    supplemental_info: SupplementalInfo = Field(default_factory=lambda: SupplementalInfo())
    employment_type: str | None = None
    published_at: str | None = None
    raw_sha256: str
    extraction_method: str
    role_direction: str | None = None
    role_reason: str | None = None
    raw_payload: str = Field(default="", exclude=True, repr=False)
    raw_content_type: str = Field(default="text/html", exclude=True)


class CategorizedItem(BaseModel):
    category: str
    evidence: str


class SupplementalInfo(BaseModel):
    work_conditions: list[CategorizedItem] = Field(default_factory=list)
    compensation: list[CategorizedItem] = Field(default_factory=list)
    interesting_benefits: list[CategorizedItem] = Field(default_factory=list)
    standard_benefits: list[CategorizedItem] = Field(default_factory=list)
    travel_requirements: list[CategorizedItem] = Field(default_factory=list)


class HttpClient:
    def __init__(self, timeout: float = 25.0, retries: int = 2) -> None:
        self.retries = retries
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9"},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < self.retries:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                response.raise_for_status()
                return response
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise SourceError(f"{method} {url} failed: {last_error}") from last_error

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


class SourceAdapter(ABC):
    def __init__(self, source: SourceConfig, http: HttpClient) -> None:
        self.source = source
        self.http = http

    @abstractmethod
    async def discover(self) -> list[DiscoveredJob]: ...


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def html_to_text(html: str) -> str:
    """Decode nested HTML entities before stripping markup.

    Some ATS APIs return a description as literal HTML while others wrap the same HTML
    in one or more entity-encoding layers. Parsing before decoding would turn
    ``&lt;p&gt;`` into visible ``<p>`` text after a later unescape.
    """
    decoded = html
    for _ in range(3):
        expanded = unescape(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()
    return compact_text(soup.get_text(" ", strip=True))


def has_html_markup(value: str) -> bool:
    """Recognize real tags after decoding without treating ordinary comparison text as HTML."""
    decoded = value
    for _ in range(3):
        expanded = unescape(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    return bool(re.search(r"</?[a-z][^>]*>", decoded, flags=re.IGNORECASE))


def normalize_clean_job(offer: CleanJob) -> CleanJob:
    """Defensively normalize a stored/imported offer before it reaches SQLite or an LLM."""
    if not has_html_markup(offer.description) and not has_html_markup(offer.analysis_text):
        return offer
    description = html_to_text(offer.description)
    analysis_text, removed_characters = build_analysis_text(description)
    supplemental_info = extract_supplemental_info(description)
    if (
        description == offer.description
        and analysis_text == offer.analysis_text
        and supplemental_info == offer.supplemental_info
        and removed_characters == offer.removed_characters
    ):
        return offer
    return offer.model_copy(
        update={
            "description": description,
            "analysis_text": analysis_text,
            "removed_characters": removed_characters,
            "supplemental_info": supplemental_info,
        }
    )


def sanitize_title(title: str) -> str:
    separators = [" | ", " · ", " • ", " - ", " Office :", " Location:", " Remote:"]
    for sep in separators:
        if sep in title:
            title = title.split(sep)[0].strip()
    return title


def extract_meta_locations(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    locs = []
    og_loc = soup.find("meta", property="og:locality")
    if og_loc and og_loc.get("content"):
        locs.append(str(og_loc["content"]))
    og_region = soup.find("meta", property="og:region")
    if og_region and og_region.get("content"):
        locs.append(str(og_region["content"]))
    for meta_name in ["location", "jobLocation", "address"]:
        meta = soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            locs.append(str(meta["content"]))
    return list(dict.fromkeys(filter(None, locs)))


def extract_page_title_and_text(html: str, fallback_title: str) -> tuple[str, str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = compact_text(heading.get_text(" ", strip=True)) if heading else fallback_title
    title = sanitize_title(title)
    locations = extract_meta_locations(html)
    container = soup.find("main") or soup.find("article") or soup.body or soup
    if container:
        for tag in container.find_all(
            ["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]
        ):
            tag.decompose()
        text = compact_text(container.get_text(" ", strip=True))
    else:
        text = ""
    return title, text, locations


PRIMARY_ROLE_SECTION_MARKERS = (
    "the work",
    "about the role",
    "job description",
    "description we are",
    "your mission",
    "your future role:",
    "tasks:",
    "what you will do",
    "what you'll do",
    "key responsibilities",
    "zadania na stanowisku",
    "zakres obowiązków",
    "zakres prac:",
    "twoje zadania",
    "obowiązki",
)
FALLBACK_ROLE_SECTION_MARKERS = ("your role", "in this role", "the role", "responsibilities")
END_MARKERS = (
    "equal opportunity",
    "equal-opportunity",
    "diversity and inclusion",
    "privacy notice",
    "candidate privacy",
    "process of personal data",
    "consent to the processing",
)
ANALYSIS_STOP_MARKERS = (
    "what we offer",
    "what we provide",
    "we offer",
    "our offer for you",
    "basic benefits",
    "how we work",
    "what it's like working",
    "recruitment process",
    "our recruitment process",
    "proces rekrutacyjny",
    "oferujemy",
)
RESPONSIBILITY_SECTION_MARKERS = (
    "the work",
    "you will:",
    "about the role",
    "job description",
    "your mission",
    "your future role:",
    "tasks:",
    "your tasks",
    "in this position, you will",
    "in this position you will",
    "what you will do",
    "what you'll do",
    "what you will be doing",
    "what you'll be doing",
    "you will be:",
    "key responsibilities",
    "responsibilities",
    "your responsibilities",
    "zadania na stanowisku",
    "zakres obowiązków",
    "zakres prac:",
    "twoje zadania",
    "obowiązki",
)
REQUIREMENT_SECTION_MARKERS = (
    "what you’ll need to succeed in this role",
    "what you'll need to succeed in this role",
    "what you’ll need to succeed",
    "what you'll need to succeed",
    "requirements",
    "you must have",
    "must-have tech stack",
    "must have tech stack",
    "experience and skills you need",
    "your profile:",
    "your profile",
    "who is this role for?",
    "what we're looking for:",
    "what we’re looking for:",
    "apply if you have:",
    "what we expect",
    "wymagania",
)

STANDARD_BENEFIT_PATTERNS = {
    "medical care": r"private medical|medical (?:care|package)|opieka medyczna|pakiet medyczny",
    "sport card": r"multisport|sport card|karta sportowa",
    "life insurance": r"life insurance|ubezpieczenie (?:na życie|zdrowotne)",
    "language classes": r"language classes|english lessons|language lessons|lekcje językowe",
    "office snacks": r"fresh fruit|fruit.?snacks|free coffee|owocowe|snacks",
}
INTERESTING_BENEFIT_PATTERNS = {
    "training or conference budget": (
        r"training budget|conference budget|learning allowance|development stipend"
    ),
    "premium AI tools": (
        r"premium ai development suite|chatgpt.*claude.*gemini|github copilot.*cursor"
    ),
    "equipment choice": r"equipment of your choice|high-end equipment|high standard equipment",
    "additional paid leave": r"birthday day off|additional day off|paid time off|extra paid leave",
    "paid volunteering": r"paid hours? for volunteering|paid volunteering",
    "coworking stipend": r"co-?working stipend|coworking (?:budget|allowance)",
    "travel or offsite stipend": r"social travel|travel stipend|annual company offsite",
    "equity": r"stock options?|equity package|employee shares?",
    "wellbeing support": r"psychological support|mindfulness sessions|coaching",
    "custom benefits": r"veterinary package|lunch pass|massages|individual benefits package",
}
WORK_CONDITION_PATTERNS = {
    "remote": r"fully remote|remote-first|remote work|work from home|praca zdalna",
    "hybrid": r"hybrid work|hybrid working|praca hybrydowa|home/office working",
    "flexible hours": r"flexible working hours|flexible work schedules|choose your working hours",
    "office requirement": r"\b\d+\s+days? (?:per week|a week|in (?:the )?office)|office days?",
    "work permit": r"work permit (?:is|are) required|right to work",
}
COMPENSATION_PATTERNS = {
    "salary": r"\b\d{1,3}(?:[ .]\d{3})?\s*(?:-|–)\s*\d{1,3}(?:[ .]\d{3})?\s*(?:PLN|zł)",
    "B2B": r"\bB2B\b",
    "employment contract": r"employment contract|umowa o pracę",
    "bonus": r"bonus|premia (?:kwartalna|roczna)",
}
TRAVEL_PATTERNS = {
    "frequent travel": r"travel (?:regularly|frequently)|up to \d+%.*travel",
    "business trips": r"business trips?|podróże służbowe|delegacje",
}


def evidence_window(text: str, match: re.Match[str], radius: int = 90) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return compact_text(text[start:end]).strip(" ,;:-")


def extract_items(text: str, patterns: dict[str, str]) -> list[CategorizedItem]:
    items = []
    for category, pattern in patterns.items():
        if match := re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            items.append(CategorizedItem(category=category, evidence=evidence_window(text, match)))
    return items


def extract_supplemental_info(description: str) -> SupplementalInfo:
    return SupplementalInfo(
        work_conditions=extract_items(description, WORK_CONDITION_PATTERNS),
        compensation=extract_items(description, COMPENSATION_PATTERNS),
        interesting_benefits=extract_items(description, INTERESTING_BENEFIT_PATTERNS),
        standard_benefits=extract_items(description, STANDARD_BENEFIT_PATTERNS),
        travel_requirements=extract_items(description, TRAVEL_PATTERNS),
    )


def build_analysis_text(description: str) -> tuple[str, int]:
    """Drop company boilerplate while retaining role evidence and location text."""
    responsibilities, requirements = extract_role_sections(description)
    if responsibilities or requirements:
        parts = []
        if responsibilities:
            parts.append(f"ROLE RESPONSIBILITIES: {responsibilities}")
        if requirements:
            parts.append(f"ROLE REQUIREMENTS: {requirements}")
        focused = compact_text(" ".join(parts))
        return focused, max(0, len(description) - len(focused))

    lowered = description.casefold()
    start = 0
    for marker in PRIMARY_ROLE_SECTION_MARKERS:
        if (position := lowered.find(marker)) >= 0:
            start = position
            break
    else:
        starts = [lowered.find(marker) for marker in FALLBACK_ROLE_SECTION_MARKERS]
        starts = [position for position in starts if position >= 0]
        start = min(starts) if starts else 0
    focused = description[start:]
    focused_lower = focused.casefold()
    ends = [focused_lower.find(marker) for marker in END_MARKERS]
    ends = [position for position in ends if position > 50]
    if ends:
        focused = focused[: min(ends)]

    focused_lower = focused.casefold()
    stops = [focused_lower.find(marker) for marker in ANALYSIS_STOP_MARKERS]
    stops = [position for position in stops if position > 50]
    if stops:
        focused = focused[: min(stops)]

    # Repeated employer/benefit blocks commonly appear after the actual requirements.
    trailing_markers = ("annual company offsite", "learning & development", "social travel")
    focused_lower = focused.casefold()
    trailing = [focused_lower.find(marker) for marker in trailing_markers]
    trailing = [position for position in trailing if position > 600]
    if trailing:
        tail = focused[min(trailing) :]
        location_position = tail.casefold().rfind("location")
        location_tail = tail[location_position:] if location_position >= 0 else ""
        focused = focused[: min(trailing)] + " " + location_tail

    focused = compact_text(focused)
    if len(focused) < 50:
        focused = description
    return focused, max(0, len(description) - len(focused))


def _first_marker_position(
    text: str, markers: tuple[str, ...], *, start: int = 0
) -> tuple[int, str] | None:
    lowered = text.casefold()
    found = []
    for marker in markers:
        position = lowered.find(marker, start)
        while position >= 0 and marker == "requirements" and (
            not text[position].isupper()
            or (position > 0 and text[position - 1].isalpha())
        ):
            position = lowered.find(marker, position + len(marker))
        if position >= 0:
            found.append((position, marker))
    return min(found, key=lambda item: (item[0], -len(item[1])), default=None)


def extract_role_sections(description: str) -> tuple[str, str]:
    """Extract explicitly labelled duties and requirements without an LLM."""
    plain = html_to_text(description) if "<" in description and ">" in description else description
    lowered = plain.casefold()
    responsibility_start = _first_marker_position(plain, RESPONSIBILITY_SECTION_MARKERS)
    requirement_start = _first_marker_position(plain, REQUIREMENT_SECTION_MARKERS)

    responsibilities = ""
    if responsibility_start:
        start, marker = responsibility_start
        content_start = start + len(marker)
        end_candidates = (
            [requirement_start[0]]
            if requirement_start and requirement_start[0] > content_start
            else []
        )
        end_candidates.extend(
            position
            for candidate in ANALYSIS_STOP_MARKERS
            if (position := lowered.find(candidate, content_start)) >= 0
        )
        end = min(end_candidates, default=len(plain))
        responsibilities = compact_text(plain[content_start:end]).strip(" :;-")

    requirements = ""
    if requirement_start:
        start, marker = requirement_start
        content_start = start + len(marker)
        end_candidates = [
            position
            for candidate in ANALYSIS_STOP_MARKERS + END_MARKERS
            if (position := lowered.find(candidate, content_start)) >= 0
        ]
        if responsibility_start and responsibility_start[0] > content_start:
            end_candidates.append(responsibility_start[0])
        end = min(end_candidates, default=len(plain))
        requirements = compact_text(plain[content_start:end]).strip(" :;-")

    return responsibilities, requirements


def value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return compact_text(" ".join(value_to_text(item) for item in value))
    if isinstance(value, dict):
        return compact_text(" ".join(value_to_text(item) for item in value.values()))
    text = unescape(str(value))
    return html_to_text(text) if "<" in text and ">" in text else compact_text(text)


def deduplicate(jobs: list[DiscoveredJob]) -> list[DiscoveredJob]:
    unique: dict[str, DiscoveredJob] = {}
    for job in jobs:
        unique[job.external_id or str(job.url).rstrip("/")] = job
    return list(unique.values())


def host_allowed(job_url: str, career_url: str) -> bool:
    host = urlparse(job_url).netloc.casefold()
    career_host = urlparse(career_url).netloc.casefold()
    return (
        host == career_host
        or host.endswith(f".{career_host}")
        or any(host == allowed or host.endswith(f".{allowed}") for allowed in ATS_HOSTS)
    )


def looks_like_job(url: str, title: str, career_url: str) -> bool:
    lowered_url = url.casefold()
    lowered_title = title.casefold()
    if not host_allowed(url, career_url) or any(x in lowered_url for x in REJECT_MARKERS):
        return False
    if len(title) < 4 or len(title) > 180:
        return False
    if lowered_url.rstrip("/") == career_url.casefold().rstrip("/"):
        return False
    roles = ("engineer", "scientist", "developer", "manager", "analyst", "specialist")
    role_title = any(marker in lowered_title for marker in roles)
    parsed = urlparse(url)
    career_path = urlparse(career_url).path.rstrip("/").casefold()
    path = parsed.path.casefold()
    child_career_page = ("/careers/" in path or "/career/" in path) and (
        parsed.path.rstrip("/").casefold() != career_path
    )
    ats_host = any(
        parsed.netloc.casefold() == allowed or parsed.netloc.casefold().endswith(f".{allowed}")
        for allowed in ATS_HOSTS
    )
    return any(marker in lowered_url for marker in JOB_MARKERS) or (
        role_title and (child_career_page or ats_host)
    )


class SmartRecruitersAdapter(SourceAdapter):
    async def discover(self) -> list[DiscoveredJob]:
        company_id = self.source.options.get("company_id", self.source.company.replace(" ", ""))
        endpoint = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
        jobs: list[DiscoveredJob] = []
        offset = 0
        while True:
            response = await self.http.get(endpoint, params={"limit": 100, "offset": offset})
            content = response.json().get("content", [])
            for item in content:
                job_id = str(item.get("id", ""))
                location = item.get("location") or {}
                jobs.append(
                    DiscoveredJob(
                        source_id=self.source.id,
                        company=self.source.company,
                        external_id=job_id or None,
                        title=item.get("name") or "Untitled role",
                        url=f"https://jobs.smartrecruiters.com/{company_id}/{job_id}",
                        location_hint=", ".join(
                            filter(None, [location.get("city"), location.get("country")])
                        )
                        or None,
                        detail_api_url=f"{endpoint}/{job_id}",
                    )
                )
            if len(content) < 100:
                break
            offset += 100
        return deduplicate(jobs)


class GreenhouseAdapter(SourceAdapter):
    async def discover(self) -> list[DiscoveredJob]:
        board = (
            self.source.options.get("board_token")
            or urlparse(str(self.source.career_url)).path.rstrip("/").split("/")[-1]
        )
        endpoint = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        response = await self.http.get(endpoint, params={"content": "true"})
        jobs = []
        for item in response.json().get("jobs", []):
            location = item.get("location") or {}
            jobs.append(
                DiscoveredJob(
                    source_id=self.source.id,
                    company=self.source.company,
                    external_id=str(item.get("id")) if item.get("id") is not None else None,
                    title=item.get("title") or "Untitled role",
                    url=item.get("absolute_url"),
                    location_hint=location.get("name"),
                    detail_api_url=f"{endpoint}/{item.get('id')}",
                    metadata={"api_payload": item},
                )
            )
        return deduplicate(jobs)


class AshbyAdapter(SourceAdapter):
    async def discover(self) -> list[DiscoveredJob]:
        board = (
            self.source.options.get("board_name")
            or urlparse(str(self.source.career_url)).path.strip("/").split("/")[-1]
        )
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        response = await self.http.get(endpoint)
        jobs = []
        for item in response.json().get("jobs", []):
            job_url = item.get("jobUrl") or item.get("applyUrl")
            if job_url:
                jobs.append(
                    DiscoveredJob(
                        source_id=self.source.id,
                        company=self.source.company,
                        external_id=item.get("id"),
                        title=item.get("title") or "Untitled role",
                        url=job_url,
                        location_hint=item.get("location"),
                        metadata={"api_payload": item},
                    )
                )
        return deduplicate(jobs)


class LeverAdapter(SourceAdapter):
    """Read the public JSON endpoint used by Lever-hosted career pages."""

    async def discover(self) -> list[DiscoveredJob]:
        site = (
            self.source.options.get("site")
            or urlparse(str(self.source.career_url)).path.strip("/").split("/")[0]
        )
        endpoint = f"https://api.lever.co/v0/postings/{site}"
        response = await self.http.get(endpoint, params={"mode": "json"})
        jobs = []
        for item in response.json():
            job_url = item.get("hostedUrl") or item.get("applyUrl")
            if not job_url:
                continue
            categories = item.get("categories") or {}
            jobs.append(
                DiscoveredJob(
                    source_id=self.source.id,
                    company=self.source.company,
                    external_id=item.get("id"),
                    title=item.get("text") or "Untitled role",
                    url=job_url,
                    location_hint=value_to_text(categories.get("location")) or None,
                    metadata={"api_payload": item},
                )
            )
        return deduplicate(jobs)


class EightfoldAdapter(SourceAdapter):
    async def discover(self) -> list[DiscoveredJob]:
        domain = self.source.options.get("domain", "netflix.com")
        parsed = urlparse(str(self.source.career_url))
        endpoint = f"{parsed.scheme}://{parsed.netloc}/api/apply/v2/jobs"
        response = await self.http.get(endpoint, params={"domain": domain})
        jobs = []
        for item in response.json().get("positions", []):
            job_id = str(item.get("id", ""))
            if job_id:
                jobs.append(
                    DiscoveredJob(
                        source_id=self.source.id,
                        company=self.source.company,
                        external_id=job_id,
                        title=item.get("name") or item.get("title") or "Untitled role",
                        url=f"{parsed.scheme}://{parsed.netloc}/careers/job/{job_id}",
                        location_hint=value_to_text(item.get("location")) or None,
                        detail_api_url=f"{endpoint}/{job_id}?domain={domain}",
                        metadata={"api_payload": item},
                    )
                )
        return deduplicate(jobs)


class HtmlAdapter(SourceAdapter):
    async def discover(self) -> list[DiscoveredJob]:
        response = await self.http.get(str(self.source.career_url))
        return extract_job_links(response.text, str(response.url), self.source)


async def render_html(url: str, wait_ms: int = 2000, *, headless: bool = True) -> tuple[str, str]:
    """Render a dynamic official career page with local headless Chromium."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(wait_ms)
            html = await page.content()
            if "Performing security verification" in html and wait_ms < 15_000:
                await page.wait_for_timeout(8_000)
                html = await page.content()
            return html, page.url
        finally:
            await browser.close()


class BrowserAdapter(SourceAdapter):
    async def discover(self) -> list[DiscoveredJob]:
        html, final_url = await render_html(
            str(self.source.career_url),
            int(self.source.options.get("wait_ms", 2500)),
            headless=bool(self.source.options.get("headless", True)),
        )
        jobs = extract_job_links(html, final_url, self.source)
        for job in jobs:
            job.metadata["browser_required"] = True
            job.metadata["browser_headless"] = bool(self.source.options.get("headless", True))
        return jobs


class JustJoinAdapter(SourceAdapter):
    """Discover public AI/ML listings using the site's numbered HTML pages."""

    async def discover(self) -> list[DiscoveredJob]:
        base = str(self.source.career_url).split("?", 1)[0]
        max_pages = max(1, int(self.source.options.get("max_pages", 100)))
        total_pages = max_pages
        jobs: dict[str, DiscoveredJob] = {}
        for page_number in range(1, max_pages + 1):
            response = await self.http.get(f"{base}?{urlencode({'page': page_number})}")
            if page_number == 1 and "max_pages" not in self.source.options:
                pages = [
                    int(value) for value in re.findall(r"(?:[?&]|&amp;)page=(\d+)", response.text)
                ]
                total_pages = min(max(pages, default=1), max_pages)
            soup = BeautifulSoup(response.text, "html.parser")
            anchors = soup.select('a.offer_list_offer_title_link[href*="/job-offer/"]')
            if not anchors:
                raise SourceError(f"Just Join IT page {page_number} has no job cards")
            added = 0
            for anchor in anchors:
                url = str(anchor.get("href", "")).split("?", 1)[0]
                if not url.startswith("https://justjoin.it/job-offer/") or url in jobs:
                    continue
                card = anchor.find_parent("div", class_=re.compile(r"mui-hj05nv"))
                card_paragraphs = card.find_all("p") if card else []
                company_tag = card_paragraphs[0] if card_paragraphs else None
                company = compact_text(company_tag.get_text(" ", strip=True)) if company_tag else ""
                if not company:
                    raise SourceError(f"Just Join IT card lacks employer: {url}")
                slug = url.rstrip("/").rsplit("/", 1)[-1]
                jobs[url] = DiscoveredJob(
                    source_id=self.source.id,
                    company=company,
                    external_id=slug,
                    title=compact_text(anchor.get_text(" ", strip=True)),
                    url=url,
                    location_hint=(
                        compact_text(card_paragraphs[1].get_text(" ", strip=True))
                        if len(card_paragraphs) > 1
                        else None
                    ),
                    metadata={"justjoin": True},
                )
                added += 1
            if page_number > 1 and added == 0:
                raise SourceError(f"Just Join IT page {page_number} repeats earlier results")
            if page_number >= total_pages:
                break
        return list(jobs.values())


class EmbeddedERecruiterAdapter(SourceAdapter):
    """Read eRecruiter offers embedded in server-rendered application state."""

    async def discover(self) -> list[DiscoveredJob]:
        response = await self.http.get(str(self.source.career_url))
        normalized = response.text.replace("\\u0026", "&").replace('\\"', '"')
        pattern = re.compile(
            r'"url":"(https://skk\.erecruiter\.pl/Offer\.aspx\?[^"\\]+)"'
            r'.*?"title":"(.*?)"',
            re.DOTALL,
        )
        jobs = []
        for url, raw_title in pattern.findall(normalized):
            match = re.search(r"[?&]oid=(\d+)", url)
            jobs.append(
                DiscoveredJob(
                    source_id=self.source.id,
                    company=self.source.company,
                    external_id=match.group(1) if match else None,
                    title=compact_text(raw_title),
                    url=url,
                )
            )
        return deduplicate(jobs)


class EpamAdapter(SourceAdapter):
    """Extract vacancy records embedded by the EPAM Next.js careers application."""

    async def discover(self) -> list[DiscoveredJob]:
        response = await self.http.get(str(self.source.career_url))
        pattern = re.compile(r'"seo":\{\s*"url":"([^"]*/vacancy/[^"]+)",\s*"title":"([^"]+)"')
        jobs = []
        for path, seo_title in pattern.findall(response.text):
            title = seo_title.replace(r"\u0026", "&")
            title = re.sub(
                r"^(Careers for|Vacancy in .*? for|Work in .*? for)\s+",
                "",
                title,
                flags=re.IGNORECASE,
            )
            title = re.sub(r"\s*\|\s*(EPAM|Top Projects At EPAM|Remote Work With EPAM)$", "", title)
            jobs.append(
                DiscoveredJob(
                    source_id=self.source.id,
                    company=self.source.company,
                    title=compact_text(title),
                    url=urljoin(str(response.url), path),
                )
            )
        return deduplicate(jobs)


def extract_job_links(html: str, base_url: str, source: SourceConfig) -> list[DiscoveredJob]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[DiscoveredJob] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "null")
        except json.JSONDecodeError:
            continue
        records = payload if isinstance(payload, list) else [payload]
        for record in records:
            if not isinstance(record, dict) or record.get("@type") != "JobPosting":
                continue
            jobs.append(
                DiscoveredJob(
                    source_id=source.id,
                    company=source.company,
                    external_id=str(record.get("identifier") or "") or None,
                    title=compact_text(str(record.get("title") or "Untitled role")),
                    url=record.get("url") or base_url,
                    metadata={"json_ld": record},
                )
            )
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"]).split("#", 1)[0]
        title = compact_text(anchor.get_text(" ", strip=True))
        if looks_like_job(url, title, str(source.career_url)):
            jobs.append(
                DiscoveredJob(
                    source_id=source.id,
                    company=source.company,
                    title=title,
                    url=url,
                )
            )
    return deduplicate(jobs)


def find_job_posting(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        if payload.get("@type") == "JobPosting":
            return payload
        for value in payload.values():
            if found := find_job_posting(value):
                return found
    elif isinstance(payload, list):
        for value in payload:
            if found := find_job_posting(value):
                return found
    return None


def json_ld_locations(record: dict[str, Any]) -> list[str]:
    locations: list[str] = []
    raw_locations = record.get("jobLocation") or []
    if isinstance(raw_locations, dict):
        raw_locations = [raw_locations]
    for raw in raw_locations:
        address = raw.get("address") if isinstance(raw, dict) else None
        if isinstance(address, dict):
            text = ", ".join(
                str(address[key])
                for key in ("addressLocality", "addressRegion", "addressCountry")
                if address.get(key)
            )
            if text:
                locations.append(text)
    if record.get("applicantLocationRequirements"):
        locations.append(value_to_text(record["applicantLocationRequirements"]))
    if record.get("jobLocationType") == "TELECOMMUTE":
        locations.append("Remote")
    return list(dict.fromkeys(filter(None, locations)))


async def clean_job(job: DiscoveredJob, http: HttpClient) -> CleanJob:
    payload = job.metadata.get("api_payload") or job.metadata.get("json_ld")
    method = "embedded_api"
    raw = json.dumps(payload, ensure_ascii=False) if payload else ""
    if job.detail_api_url:
        response = await http.get(str(job.detail_api_url))
        payload, raw, method = response.json(), response.text, "detail_api"
    elif not payload:
        if job.metadata.get("browser_required"):
            raw, _ = await render_html(
                str(job.url), 1200, headless=bool(job.metadata.get("browser_headless", True))
            )
            method = "browser_html"
        else:
            response = await http.get(str(job.url))
            raw, method = response.text, "html"
        soup = BeautifulSoup(raw, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                found = find_job_posting(json.loads(script.string or "null"))
            except json.JSONDecodeError:
                continue
            if found:
                payload, method = found, "json_ld"
                break
    record = find_job_posting(payload) if payload else None
    if record:
        title = value_to_text(record.get("title")) or job.title
        description = value_to_text(record.get("description"))
        organization = record.get("hiringOrganization")
        company = (
            value_to_text(organization.get("name"))
            if job.metadata.get("justjoin") and isinstance(organization, dict)
            else job.company
        ) or job.company
        locations = json_ld_locations(record)
        employment_type = value_to_text(record.get("employmentType")) or None
        published_at = record.get("datePosted")
    elif isinstance(payload, dict):
        title = value_to_text(payload.get("name") or payload.get("title")) or job.title
        description = value_to_text(
            payload.get("jobAd")
            or payload.get("job_description")
            or payload.get("description")
            or payload.get("content")
            or payload.get("descriptionHtml")
        )
        location = payload.get("location") or payload.get("locations") or job.location_hint
        locations = [value_to_text(location)] if location else []
        employment_type = value_to_text(payload.get("typeOfEmployment")) or None
        published_at = payload.get("releasedDate") or payload.get("publishedAt")
    else:
        title, description, meta_locs = extract_page_title_and_text(raw, job.title)
        locations = meta_locs or ([job.location_hint] if job.location_hint else [])
        employment_type = published_at = None
    if not record:
        company = job.company
    if not description:
        raise SourceError(f"empty cleaned description for {job.url}")
    blocked_markers = ("performing security verification", "enable javascript and cookies")
    if any(marker in description.casefold() for marker in blocked_markers):
        raise SourceError(f"anti-bot page returned instead of job description for {job.url}")
    analysis_text, removed_characters = build_analysis_text(description)
    supplemental_info = extract_supplemental_info(description)
    return normalize_clean_job(
        CleanJob(
            source_id=job.source_id,
            company=company,
            external_id=job.external_id,
            title=title,
            url=job.url,
            locations=list(dict.fromkeys(filter(None, locations))),
            description=description,
            analysis_text=analysis_text,
            removed_characters=removed_characters,
            supplemental_info=supplemental_info,
            employment_type=employment_type,
            published_at=str(published_at) if published_at else None,
            raw_sha256=hashlib.sha256(raw.encode()).hexdigest(),
            extraction_method=method,
            raw_payload=raw,
            raw_content_type=(
                "application/json" if method in {"detail_api", "embedded_api"} else "text/html"
            ),
        )
    )


ADAPTERS: dict[str, type[SourceAdapter]] = {
    "ashby": AshbyAdapter,
    "eightfold": EightfoldAdapter,
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "smartrecruiters": SmartRecruitersAdapter,
    "html": HtmlAdapter,
    "custom": HtmlAdapter,
    "browser": BrowserAdapter,
    "embedded_erecruiter": EmbeddedERecruiterAdapter,
    "epam": EpamAdapter,
    "justjoin": JustJoinAdapter,
}


def adapter_for(source: SourceConfig, http: HttpClient) -> SourceAdapter:
    try:
        return ADAPTERS[source.adapter](source, http)
    except KeyError as exc:
        raise SourceError(f"unsupported adapter: {source.adapter}") from exc
