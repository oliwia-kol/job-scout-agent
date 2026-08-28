from job_scout.domain import JobOffer, LocationEligibility
from job_scout.location import classify_location


def offer(*, locations=None, remote_from_poland=None):
    return JobOffer(
        company="Example",
        title="Applied AI Engineer",
        job_url="https://example.com/jobs/1",
        description="Build AI products",
        locations=locations or [],
        remote_from_poland=remote_from_poland,
    )


def test_remote_from_poland_is_eligible():
    assert classify_location(offer(remote_from_poland=True)) == LocationEligibility.ELIGIBLE


def test_warsaw_is_eligible():
    assert classify_location(offer(locations=["Warsaw, Poland"])) == LocationEligibility.ELIGIBLE


def test_unspecified_remote_region_is_unknown():
    assert classify_location(offer(locations=["Remote, Europe"])) == LocationEligibility.UNKNOWN


def test_known_location_outside_scope_is_ineligible():
    assert classify_location(offer(locations=["Berlin, Germany"])) == LocationEligibility.INELIGIBLE


def test_missing_location_is_unknown():
    assert classify_location(offer()) == LocationEligibility.UNKNOWN
