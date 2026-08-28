"""Notion-backed operational store for the current end-to-end demo."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

import httpx

from .ats_scrapers import CleanJob
from .domain import FitAssessment

NOTION_VERSION = "2026-03-11"

REQUIRED_PROPERTY_TYPES = {
    "Stanowisko": "title",
    "Firma": "rich_text",
    "Źródło": "rich_text",
    "URL": "url",
    "Offer_key": "rich_text",
    "Content_hash": "rich_text",
    "Data": "date",
    "Tryb_pracy": "select",
    "Umowa": "select",
    "Status_Scrapera": "select",
    "Pipeline_status": "select",
    "Alert_status": "select",
    "Alert_key": "rich_text",
    "Kluczowe_wymagania": "rich_text",
    "Ocena_AI": "number",
    "Decyzja_AI": "select",
    "Uzasadnienie": "rich_text",
}
REQUIRED_SELECT_OPTIONS = {
    "Pipeline_status": {"Processing", "Completed", "Failed"},
    "Alert_status": {"Pending", "Sent", "Failed", "Not eligible"},
}


class NotionStoreError(RuntimeError):
    """A Notion request failed or the configured data source is incompatible."""


@dataclass(frozen=True)
class NotionPage:
    page_id: str
    created: bool
    content_hash: str | None = None
    pipeline_status: str | None = None
    alert_key: str | None = None
    alert_status: str | None = None


class NotionOffersStore:
    """Use Notion as the operational store for the no-panel demo phase."""

    def __init__(
        self,
        token: str,
        database_id: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.database_id = database_id
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=30)
        self.client.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )
        self.data_source_id: str | None = None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> NotionOffersStore:
        self.ensure_ready()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: object) -> dict:
        try:
            response = self.client.request(method, f"https://api.notion.com/v1{path}", **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotionStoreError(f"Notion {method} {path} failed: {exc}") from exc

    def ensure_ready(self) -> str:
        if self.data_source_id:
            return self.data_source_id
        database = self._request("GET", f"/databases/{self.database_id}")
        data_sources = database.get("data_sources") or []
        if not data_sources or not data_sources[0].get("id"):
            raise NotionStoreError("Notion database has no writable data source")
        self.data_source_id = str(data_sources[0]["id"])
        schema = self._request("GET", f"/data_sources/{self.data_source_id}")
        properties = schema.get("properties") or {}
        missing = sorted(set(REQUIRED_PROPERTY_TYPES) - set(properties))
        wrong_types = sorted(
            name
            for name, expected_type in REQUIRED_PROPERTY_TYPES.items()
            if name in properties and properties[name].get("type") != expected_type
        )
        missing_options = sorted(
            name
            for name, required_options in REQUIRED_SELECT_OPTIONS.items()
            if not required_options
            <= {
                option.get("name")
                for option in properties.get(name, {}).get("select", {}).get("options", [])
            }
        )
        problems = []
        if missing:
            problems.append(f"missing properties: {', '.join(missing)}")
        if wrong_types:
            problems.append(f"wrong property types: {', '.join(wrong_types)}")
        if missing_options:
            problems.append(f"missing select options: {', '.join(missing_options)}")
        if problems:
            raise NotionStoreError("Notion data source is incompatible; " + "; ".join(problems))
        return self.data_source_id

    @staticmethod
    def offer_key(offer: CleanJob) -> str:
        stable_part = offer.external_id or str(offer.url).rstrip("/")
        return f"{offer.source_id}:{stable_part}"

    @staticmethod
    def content_hash(offer: CleanJob) -> str:
        """Fingerprint model-relevant, normalized facts instead of volatile HTML."""

        def normalized(value: str | None) -> str:
            return " ".join((value or "").split())

        supplemental = {
            name: [
                {
                    "category": normalized(item.get("category")),
                    "evidence": normalized(item.get("evidence")),
                }
                for item in values
            ]
            for name, values in offer.supplemental_info.model_dump(mode="json").items()
        }
        payload = {
            "title": normalized(offer.title),
            "analysis_text": normalized(offer.analysis_text),
            "locations": [normalized(location) for location in offer.locations],
            "employment_type": normalized(offer.employment_type),
            "supplemental_info": supplemental,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()

    @staticmethod
    def alert_key(offer: CleanJob, content_hash: str) -> str:
        return f"{NotionOffersStore.offer_key(offer)}:{content_hash}"

    @staticmethod
    def _text(value: str) -> list[dict]:
        return [{"type": "text", "text": {"content": value[:2000]}}] if value else []

    @staticmethod
    def _property_text(property_data: dict | None) -> str | None:
        if not property_data:
            return None
        values = property_data.get("rich_text") or property_data.get("title") or []
        text = "".join(
            item.get("plain_text") or item.get("text", {}).get("content", "")
            for item in values
        )
        return text or None

    @staticmethod
    def _select_value(property_data: dict | None) -> str | None:
        value = (property_data or {}).get("select")
        return value.get("name") if value else None

    @staticmethod
    def _mode(offer: CleanJob) -> str:
        evidence = [item.evidence for item in offer.supplemental_info.work_conditions]
        haystack = " ".join([*offer.locations, offer.analysis_text, *evidence]).casefold()
        if "hybrid" in haystack or "hybryd" in haystack:
            return "Hybryda"
        if "remote" in haystack or "zdal" in haystack:
            return "Zdalnie"
        if "office" in haystack or "biuro" in haystack or "on-site" in haystack:
            return "Biuro"
        return "Nie podano"

    @staticmethod
    def _contract(offer: CleanJob) -> str:
        value = (offer.employment_type or "").casefold()
        if "b2b" in value and "uop" in value:
            return "B2B / UoP"
        if "b2b" in value:
            return "B2B"
        if "uop" in value or "employment" in value:
            return "UoP"
        return "Nie podano"

    def _offer_properties(
        self,
        offer: CleanJob,
        *,
        content_hash: str,
        pipeline_status: str,
        alert_status: str,
    ) -> dict:
        return {
            "Stanowisko": {"title": self._text(offer.title)},
            "Firma": {"rich_text": self._text(offer.company)},
            "Źródło": {"rich_text": self._text(offer.source_id)},
            "URL": {"url": str(offer.url)},
            "Offer_key": {"rich_text": self._text(self.offer_key(offer))},
            "Content_hash": {"rich_text": self._text(content_hash)},
            "Data": {"date": {"start": datetime.now(UTC).date().isoformat()}},
            "Tryb_pracy": {"select": {"name": self._mode(offer)}},
            "Umowa": {"select": {"name": self._contract(offer)}},
            "Status_Scrapera": {"select": {"name": "Aktywna"}},
            "Pipeline_status": {"select": {"name": pipeline_status}},
            "Alert_status": {"select": {"name": alert_status}},
            "Alert_key": {"rich_text": []},
            "Kluczowe_wymagania": {"rich_text": self._text(offer.analysis_text[:2000])},
        }

    def lookup_offer(self, offer: CleanJob) -> NotionPage | None:
        data_source_id = self.ensure_ready()
        result = self._request(
            "POST",
            f"/data_sources/{data_source_id}/query",
            json={
                "page_size": 2,
                "filter": {
                    "property": "Offer_key",
                    "rich_text": {"equals": self.offer_key(offer)},
                },
            },
        )
        results = result.get("results") or []
        if len(results) > 1:
            raise NotionStoreError(
                f"duplicate Offer_key in Notion: {self.offer_key(offer)}; resolve manually"
            )
        if not results:
            return None
        page = results[0]
        properties = page.get("properties") or {}
        return NotionPage(
            page_id=str(page["id"]),
            created=False,
            content_hash=self._property_text(properties.get("Content_hash")),
            pipeline_status=self._select_value(properties.get("Pipeline_status")),
            alert_key=self._property_text(properties.get("Alert_key")),
            alert_status=self._select_value(properties.get("Alert_status")),
        )

    @staticmethod
    def is_current(page: NotionPage | None, content_hash: str) -> bool:
        return bool(
            page
            and page.content_hash == content_hash
            and page.pipeline_status == "Completed"
        )

    def upsert_offer(
        self,
        offer: CleanJob,
        *,
        content_hash: str,
        existing_page: NotionPage | None = None,
    ) -> NotionPage:
        page = existing_page if existing_page is not None else self.lookup_offer(offer)
        properties = self._offer_properties(
            offer,
            content_hash=content_hash,
            pipeline_status="Processing",
            alert_status="Pending",
        )
        if page:
            self._request("PATCH", f"/pages/{page.page_id}", json={"properties": properties})
            return NotionPage(page_id=page.page_id, created=False, content_hash=content_hash)
        result = self._request(
            "POST",
            "/pages",
            json={
                "parent": {"type": "data_source_id", "data_source_id": self.ensure_ready()},
                "properties": properties,
            },
        )
        return NotionPage(page_id=str(result["id"]), created=True, content_hash=content_hash)

    def mark_failed(self, page_id: str, error: Exception | str) -> None:
        self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={
                "properties": {
                    "Pipeline_status": {"select": {"name": "Failed"}},
                    "Uzasadnienie": {"rich_text": self._text(str(error))},
                }
            },
        )

    def save_assessment(self, page_id: str, assessment: FitAssessment) -> None:
        decision = "Pasuje" if assessment.final_score >= 7 else "Odrzucona"
        explanation = " ".join(
            [assessment.recommendation, *assessment.strengths[:2], *assessment.gaps[:2]]
        )
        alert_status = "Pending" if assessment.can_alert else "Not eligible"
        self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={
                "properties": {
                    "Pipeline_status": {"select": {"name": "Completed"}},
                    "Alert_status": {"select": {"name": alert_status}},
                    "Ocena_AI": {"number": assessment.final_score},
                    "Decyzja_AI": {"select": {"name": decision}},
                    "Uzasadnienie": {"rich_text": self._text(explanation)},
                }
            },
        )

    def mark_alert(self, page_id: str, *, alert_key: str, sent: bool) -> None:
        self._request(
            "PATCH",
            f"/pages/{page_id}",
            json={
                "properties": {
                    "Alert_status": {"select": {"name": "Sent" if sent else "Failed"}},
                    "Alert_key": {"rich_text": self._text(alert_key)},
                }
            },
        )
