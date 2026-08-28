from job_scout.title_filter import is_target_title, match_title


def test_accepts_target_ai_titles():
    accepted = [
        "Senior AI Engineer",
        "Applied Scientist",
        "Machine Learning Engineer",
        "Data Scientist",
        "LLM Evaluation Engineer",
        "Research Engineer, Generative AI",
        "AI Automation Engineer",
    ]
    assert all(is_target_title(title) for title in accepted)


def test_rejects_non_target_roles_at_ai_company():
    rejected = ["Social Media Manager", "Legal Counsel", "Partnerships Manager"]
    assert all(not is_target_title(title) for title in rejected)


def test_returns_readable_match_labels():
    assert "AI" in match_title("AI Product Manager")
