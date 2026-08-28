from job_scout.domain import ExtractedJobDetails
from job_scout.extractor_benchmark import extraction_quality_flags


def test_quality_flags_find_empty_and_oversized_fields():
    value = ExtractedJobDetails(
        salary="x" * 301,
        risk_signals=["y" * 401],
    )
    assert extraction_quality_flags(value) == [
        "empty_responsibilities",
        "empty_required_skills",
        "oversized_risk_signal",
        "oversized_salary_evidence",
    ]


def test_quality_flags_accept_compact_complete_extraction():
    value = ExtractedJobDetails(
        responsibilities=["Build AI systems"],
        required_skills=["Python"],
        risk_signals=["Frequent travel"],
        salary="20 000 PLN B2B",
    )
    assert extraction_quality_flags(value) == []
