from job_scout.ats_scrapers import build_analysis_text, extract_role_sections


def test_removes_company_intro_before_role_section():
    description = (
        "About ElevenLabs ElevenLabs is an AI research company. " * 20
        + "About the role Build and evaluate agentic systems. Requirements Python and LLMs. "
        + "Equal opportunity employer boilerplate."
    )
    analysis, removed = build_analysis_text(description)
    assert analysis.startswith("ROLE RESPONSIBILITIES:")
    assert "Build and evaluate agentic systems" in analysis
    assert "About ElevenLabs" not in analysis
    assert "Equal opportunity" not in analysis
    assert removed > 500


def test_extracts_responsibilities_and_requirements_around_interleaved_benefits():
    description = (
        "Company introduction. Discover our perks and benefits: medical care. "
        "In this position, you will: Build LLM systems. Deploy them to production. "
        "What you'll need to succeed in this role: Python. FastAPI. "
        "What we offer private medical care."
    )
    responsibilities, requirements = extract_role_sections(description)
    assert responsibilities == "Build LLM systems. Deploy them to production."
    assert requirements == "Python. FastAPI."

    analysis, _ = build_analysis_text(description)
    assert analysis.startswith("ROLE RESPONSIBILITIES: Build LLM systems")
    assert "ROLE REQUIREMENTS: Python. FastAPI." in analysis
    assert "medical care" not in analysis


def test_uses_earliest_explicit_responsibility_marker():
    description = (
        "Responsibilities Build models. Requirements Python. "
        "Later in this role you receive benefits."
    )
    responsibilities, requirements = extract_role_sections(description)
    assert responsibilities == "Build models."
    assert requirements == "Python. Later in this role you receive benefits."
