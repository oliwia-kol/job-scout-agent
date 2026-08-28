from job_scout.ats_scrapers import CleanJob
from job_scout.domain import LocationEligibility
from job_scout.prefilter import evaluate_prefilter


def offer(*, title="AI Engineer", locations=None, analysis="Build LLM agents."):
    return CleanJob(
        source_id="example",
        company="Example",
        title=title,
        url="https://example.com/jobs/1",
        locations=locations or [],
        description=analysis,
        analysis_text=analysis,
        raw_sha256="a" * 64,
        extraction_method="test",
    )


def test_explicit_foreign_location_beats_generic_global_wording():
    result = evaluate_prefilter(
        offer(locations=["United States"], analysis="Remote role in our global team building AI.")
    )
    assert result.location_eligibility == LocationEligibility.INELIGIBLE
    assert result.passed is False


def test_poland_location_is_eligible():
    result = evaluate_prefilter(offer(locations=["Warsaw, Poland"]))
    assert result.location_eligibility == LocationEligibility.ELIGIBLE
    assert result.passed is True


def test_other_polish_city_is_ineligible_without_remote_from_poland():
    result = evaluate_prefilter(offer(locations=["Wroclaw, Poland"]))
    assert result.location_eligibility == LocationEligibility.INELIGIBLE
    assert result.passed is False


def test_remote_from_poland_is_eligible():
    result = evaluate_prefilter(
        offer(locations=["Remote, Poland"], analysis="Build production AI workflows.")
    )
    assert result.location_eligibility == LocationEligibility.ELIGIBLE
    assert result.passed is True


def test_unknown_location_is_retained_for_later_validation():
    result = evaluate_prefilter(offer())
    assert result.location_eligibility == LocationEligibility.UNKNOWN
    assert result.passed is True
