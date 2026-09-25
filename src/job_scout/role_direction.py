"""Conservative, explainable role gate before profile scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass

ROLE_RULES_VERSION = "dominant-duties-v11"

SOFTWARE_TITLE = re.compile(
    r"\b(full[ -]?stack|front[ -]?end|back[ -]?end|software engineer|"
    r"software developer|devops|cloud infrastructure|platform infrastructure|"
    r"quality engineer|qe lead|test automation engineer)\b",
    re.IGNORECASE,
)
SOFTWARE_DUTIES = re.compile(
    r"\b(front[ -]?end|back[ -]?end|full[ -]?stack|React|Angular|"
    r"microservices|CI/CD|Kubernetes|Terraform|infrastructure as code|"
    r"automated tests?|test automation|testów (integracyjnych|regresyjnych)|"
    r"software deployments?)\b",
    re.IGNORECASE,
)
APPLIED_DUTIES = re.compile(
    r"\b(ai agents?|agentic|agenci|agentów|RAG|LLMs?|GenAI|"
    r"generative ai|automatyzacj[aeęiy]|automation workflows?|"
    r"integracj[aeęiy]|integrat(?:e|ion|ing) AI|"
    r"integrat(?:e|ing) language models into|Copilot Studio|"
    r"business process|procesów biznesowych)\b",
    re.IGNORECASE,
)
CLEAR_APPLIED_DUTIES = re.compile(
    r"\b(?:AI agents?|agentic workflows?|multi-agent workflows?|"
    r"agentic AI(?: systems| solutions)?|systemów agentowych|"
    r"RAG pipelines?|LLM-based experiences|LLM-based capabilities|tool use|"
    r"orchestration layers|integrat(?:e|ing|ion) AI (?:with|into)|"
    r"integrat(?:e|ing) language models into|AI automation workflows?)\b",
    re.IGNORECASE,
)
PLATFORM_DUTIES = re.compile(
    r"\b(?:platform(?:y|a)?|infrastruktur\w*|infrastructure|DevOps|MLOps|"
    r"Terraform|Kubernetes|AKS|monitoring|observability|CI/CD)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoleDirection:
    category: str  # target, software, review
    reason: str


def has_clear_applied_duties(description: str) -> bool:
    duties = re.split(
        r"ROLE REQUIREMENTS:|Wymagania\b|Requirements\b",
        description,
        maxsplit=1,
        flags=re.I,
    )[0]
    return bool(CLEAR_APPLIED_DUTIES.search(duties[:6000]))


def classify_role(title: str, description: str) -> RoleDirection:
    """Hide only a clear software focus; uncertain descriptions stay reviewable."""
    duties = re.split(
        r"ROLE REQUIREMENTS:|Wymagania\b|Requirements\b", description, maxsplit=1, flags=re.I
    )[0]
    duties = duties[:6000]
    software = list(SOFTWARE_DUTIES.finditer(duties))
    applied = list(APPLIED_DUTIES.finditer(duties))
    title_match = SOFTWARE_TITLE.search(title)
    if re.search(r"\b(?:manual|qa)\s+(?:ai\s+)?tester\b", title, re.I):
        return RoleDirection("software", "Dominująca rola: ręczne testowanie oprogramowania")
    if re.search(r"\b(?:qe lead|quality engineer|test automation engineer)\b", title, re.I):
        return RoleDirection("software", "Dominująca rola: klasyczne testowanie oprogramowania")
    if (
        re.search(r"\b(?:administrat\w*|administrator)\b", title, re.I)
        and re.search(r"\bplatform\w*\b", title, re.I)
        and re.search(r"\bKubernetes\b", duties, re.I)
        and re.search(r"\b(?:utrzymani\w*|administracj\w*|maintenance)\b", duties, re.I)
    ):
        return RoleDirection("software", "Dominująca rola: administracja platformą AI")
    if (
        re.search(r"\bplatform(?:y|a)?\b", f"{title} {duties}", re.I)
        and len(PLATFORM_DUTIES.findall(duties)) >= 3
        and re.search(r"\b(?:Terraform|Infrastructure as Code|IaC)\b", description, re.I)
        and re.search(r"\b(?:Kubernetes|AKS)\b", description, re.I)
        and re.search(r"\b(?:DevOps|CI/CD)\b", description, re.I)
    ):
        return RoleDirection("software", "Dominująca rola: infrastruktura platformy AI")
    if (
        title_match
        and re.search(r"front[ -]?end", title, re.I)
        and len(software) >= len(applied)
        and len(software) >= 2
    ):
        return RoleDirection("software", "Przeważają obowiązki frontendowe")
    if (
        title_match
        and re.search(r"full[ -]?stack", title, re.I)
        and re.search(r"agentów programistycznych", duties, re.I)
        and len(software) >= 2
    ):
        return RoleDirection("software", "Dominujący rozwój full-stack z narzędziami AI")
    if re.search(
        r"\b(?:responsibilities of a full[ -]?stack developer|"
        r"role is primarily full[ -]?stack|full[ -]?stack developer)\b",
        duties,
        re.I,
    ):
        return RoleDirection("software", "Dominujące obowiązki full-stack")
    if title_match and (
        len(applied) <= 1 or len(software) >= len(applied) * 2 + 2
    ):
        return RoleDirection("software", f"Dominująca rola software: {title_match.group(0)}")
    if len(software) >= 3 and len(software) >= len(applied) * 2 + 2:
        return RoleDirection("software", f"Przeważają obowiązki software: {software[0].group(0)}")
    if applied:
        return RoleDirection("target", f"Obowiązki AI: {applied[0].group(0)}")
    return RoleDirection("review", "Brak jednoznacznego opisu dominujących obowiązków")
