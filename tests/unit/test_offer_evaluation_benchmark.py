from job_scout.ats_scrapers import CategorizedItem, CleanJob, SupplementalInfo
from job_scout.domain import CandidateEvidence, CandidateProfile
from job_scout.offer_evaluation_benchmark import (
    _deterministic_section_extraction,
    _evidence_catalog,
)


def test_labelled_sections_feed_bielik_without_an_extra_model_call():
    offer = CleanJob(
        source_id="example",
        company="Example",
        external_id="42",
        title="Applied AI Engineer",
        url="https://example.com/jobs/42",
        locations=["Poland"],
        description="Build AI products.",
        analysis_text=(
            "ROLE RESPONSIBILITIES: Build evaluated AI products. Work with product teams. "
            "ROLE REQUIREMENTS: Python experience. Structured output knowledge."
        ),
        supplemental_info=SupplementalInfo(
            work_conditions=[
                CategorizedItem(category="remote", evidence="Remote from Poland")
            ]
        ),
        raw_sha256="a" * 64,
        extraction_method="html",
    )

    extracted = _deterministic_section_extraction(offer)

    assert extracted.responsibilities == [
        "Build evaluated AI products.",
        "Work with product teams.",
    ]
    assert extracted.required_skills == [
        "Python experience.",
        "Structured output knowledge.",
    ]
    assert extracted.work_mode == "remote"

    profile = CandidateProfile(
        profile_id="test",
        version=1,
        target_roles=["Applied AI Engineer"],
        evidence=[
            CandidateEvidence(
                id="prototype",
                statement="Built a local AI prototype.",
                source="user",
            )
        ],
        work_preferences=["Remote from Poland"],
        location_rule="Remote from Poland or hybrid Warsaw.",
    )
    catalog = _evidence_catalog(offer, extracted, profile)

    assert catalog["job-1"] == (
        "offer.analysis_text",
        "Build evaluated AI products.",
    )
    assert catalog["prototype"] == (
        "profile:prototype",
        "Built a local AI prototype.",
    )
