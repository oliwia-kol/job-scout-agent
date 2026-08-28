from job_scout.ats_scrapers import build_analysis_text, extract_supplemental_info


def test_categorizes_interesting_and_standard_benefits_separately():
    text = (
        "What we offer: private medical care, Multisport and fresh fruit. "
        "Conference budget, equipment of your choice, paid birthday day off, "
        "three paid hours for volunteering and a veterinary package."
    )
    info = extract_supplemental_info(text)
    interesting = {item.category for item in info.interesting_benefits}
    standard = {item.category for item in info.standard_benefits}
    assert {"training or conference budget", "equipment choice", "additional paid leave"} <= (
        interesting
    )
    assert {"paid volunteering", "custom benefits"} <= interesting
    assert {"medical care", "sport card", "office snacks"} <= standard


def test_extracts_work_conditions_and_compensation():
    info = extract_supplemental_info(
        "Fully remote with flexible working hours. 18 000 – 25 000 PLN B2B and annual bonus."
    )
    assert {item.category for item in info.work_conditions} >= {"remote", "flexible hours"}
    assert {item.category for item in info.compensation} >= {"salary", "B2B", "bonus"}


def test_offer_section_is_not_sent_to_cv_fit_analysis():
    text = (
        "About the role Build production LLM systems. Requirements Python and RAG. "
        "What we offer private medical care, Multisport, birthday day off and fruit."
    )
    analysis, _ = build_analysis_text(text)
    assert "Build production LLM systems" in analysis
    assert "private medical care" not in analysis
