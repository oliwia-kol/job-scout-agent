import json

import pytest

from job_scout.ats_scrapers import CleanJob
from job_scout.golden_dataset import (
    build_golden_quiz_pack,
    load_golden_dataset,
    verify_golden_extraction,
)
from job_scout.storage import connect, initialize_database, persist_clean_offer


def _case(number: int, split: str) -> dict:
    return {
        "case_id": f"golden-v0-{number:02d}",
        "split": split,
        "offer": {
            "offer_id": f"offer-{number}",
            "version": 1,
            "source_url": f"https://example.com/jobs/{number}",
            "content_hash": f"hash-{number}",
            "evidence": {},
        },
        "expected": {
            "responsibilities": ["Build AI tools"],
            "requirements": ["Python"],
            "conditions": {},
            "source_quotes": ["Build AI tools"],
        },
        "assessment": {
            "candidate_fit": {"score": 4, "comment": "Relevant evidence."},
            "current_cv_fit": {"score": 4, "comment": "Visible in the CV."},
            "tailored_cv_potential": {"score": 4, "comment": "Can be tailored."},
            "preference_fit": {
                "status": "insufficient_data",
                "score": None,
                "coverage": 0.4,
                "comment": "Compensation and contract are unknown."
            },
            "role_direction_fit": {"score": 5, "comment": "AI automation is core."}
        },
        "decision": {
            "label": "consider",
            "reason": "Needs a manual review.",
            "confirmed_conflicts": [],
            "unknowns": [],
            "positives": ["Relevant work"],
        },
    }


def test_golden_dataset_requires_real_contract_shape_and_frozen_validation_split(tmp_path):
    path = tmp_path / "cases.json"
    payload = {
        "dataset_version": "golden-v0",
        "split_policy": {"calibration_ratio": 0.7, "validation_ratio": 0.3},
        "cases": [
            _case(number, "calibration" if number <= 8 else "validation")
            for number in range(1, 13)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_golden_dataset(path) == payload

    payload["cases"][0]["decision"]["reason"] = ""
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="decision reason"):
        load_golden_dataset(path)


def test_golden_extraction_requires_current_hash_and_persisted_evidence(tmp_path):
    database_path = tmp_path / "offers.db"
    initialize_database(database_path)
    cases = []
    with connect(database_path) as connection:
        for number in range(1, 13):
            description = (
                "Responsibilities: Build AI tools. Requirements: Python experience. "
                "We offer remote work."
            )
            offer_id, _ = persist_clean_offer(
                connection,
                CleanJob(
                    source_id="golden-test",
                    company="Example",
                    external_id=str(number),
                    title=f"AI Engineer {number}",
                    url=f"https://example.com/jobs/{number}",
                    description=description,
                    analysis_text=description,
                    raw_sha256=f"{number:064x}",
                    extraction_method="test",
                ),
                source_url="https://example.com/careers",
            )
            content_hash = connection.execute(
                "SELECT current_content_sha256 FROM offers WHERE id = ?", (offer_id,)
            ).fetchone()[0]
            case = _case(number, "calibration" if number <= 8 else "validation")
            case["offer"].update({"offer_id": str(offer_id), "content_hash": content_hash})
            case["expected"].update(
                {
                    "responsibilities": ["Build AI tools"],
                    "requirements": ["Python experience"],
                    "conditions": {"work_model": "remote work"},
                    "source_quotes": ["Responsibilities: Build AI tools."],
                }
            )
            cases.append(case)
    dataset_path = tmp_path / "cases.json"
    dataset_path.write_text(
        json.dumps(
            {
                "dataset_version": "golden-v0",
                "split_policy": {"calibration_ratio": 0.7, "validation_ratio": 0.3},
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    assert verify_golden_extraction(dataset_path, database_path) == {
        "cases_verified": 12,
        "validation_cases": 4,
    }

    quiz = build_golden_quiz_pack(database_path, list(range(1, 13)))
    assert quiz["dataset_version_target"] == "golden-v0"
    assert [case["split"] for case in quiz["cases"]].count("calibration") == 8
    assert quiz["cases"][0]["observed_evidence"]["responsibilities"][0]["source_quote"]
    assert quiz["cases"][0]["quiz"]["decision_options"] == ["apply", "consider", "reject"]
