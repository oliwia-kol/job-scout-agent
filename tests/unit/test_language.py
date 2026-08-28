import json
from pathlib import Path

import pytest

from job_scout.language import (
    CanonicalEnglishTranslation,
    CanonicalEnglishTranslationDraft,
    TranslationQuoteDraft,
    TranslationQuoteMap,
    _canonicalize_translation,
    detect_language,
    validate_canonical_translation,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Wymagania na stanowisku: doświadczenie w Python oraz praca z zespołem.",
            "pl",
        ),
        (
            "Role requirements include Python experience and work with the product team.",
            "en",
        ),
        (
            "Wymagania: doświadczenie w Python. You will work with the product team.",
            "mixed",
        ),
        ("Python SQL API", "unknown"),
    ],
)
def test_detects_polish_english_mixed_and_unknown(text, expected):
    assert detect_language(text).language == expected


def valid_translation():
    source = "Wymagane 3 lata Python. Praca nie wymaga 2 dni w biurze."
    original_quote = "Wymagane 3 lata Python."
    return source, CanonicalEnglishTranslation(
        translated_text=(
            "3 years of Python are required. The role does not require 2 office days."
        ),
        quote_map=[
            TranslationQuoteMap(
                original_quote=original_quote,
                translated_quote="3 years of Python are required.",
                source_start=0,
                source_end=len(original_quote),
            )
        ],
    )


def test_translation_preserves_spans_numbers_technologies_and_negation():
    source, translation = valid_translation()
    validate_canonical_translation(source, translation)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            {
                "translated_text": "Python is required.",
                "quote_map": [
                    TranslationQuoteMap(
                        original_quote="Wymagane 3 lata Python.",
                        translated_quote="Python is required.",
                        source_start=0,
                        source_end=23,
                    )
                ],
            },
            "critical facts",
        ),
        (
            {
                "translated_text": (
                    "3 years of Python are required. The role requires 2 office days."
                )
            },
            "negation",
        ),
        (
            {
                "quote_map": [
                    TranslationQuoteMap(
                        original_quote="Nieprawidłowy cytat",
                        translated_quote="3 years of Python are required.",
                        source_start=0,
                        source_end=21,
                    )
                ]
            },
            "original text span",
        ),
    ],
)
def test_translation_validator_rejects_fact_loss(change, match):
    source, translation = valid_translation()
    with pytest.raises(ValueError, match=match):
        validate_canonical_translation(source, translation.model_copy(update=change))


def test_translation_draft_gets_exact_source_offsets():
    source = "Rola w Warszawie. Nie wymagamy Kubernetes."
    canonical = _canonicalize_translation(
        source,
        CanonicalEnglishTranslationDraft(
            translated_text="A role in Warsaw. We do not require Kubernetes.",
            quote_map=[
                TranslationQuoteDraft(
                    original_quote="Nie wymagamy Kubernetes.",
                    translated_quote="We do not require Kubernetes.",
                )
            ],
        ),
    )
    assert source[canonical.quote_map[0].source_start : canonical.quote_map[0].source_end] == (
        "Nie wymagamy Kubernetes."
    )


def test_controlled_language_pair_dataset_preserves_declared_facts():
    path = Path("config/demo/offers-language-pairs-v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["kind"] == "synthetic-controlled"
    assert len(payload["pairs"]) >= 4
    for pair in payload["pairs"]:
        assert detect_language(pair["source_text"]).language == pair["source_language"]
        for fact in pair["must_preserve"]:
            assert fact.casefold() in pair["canonical_english"].casefold()
