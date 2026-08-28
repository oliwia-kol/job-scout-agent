from job_scout.ats_scrapers import extract_meta_locations, sanitize_title


def test_sanitize_title():
    assert (
        sanitize_title("Digital Designer (Brand) Office : Barcelona · Dubai")
        == "Digital Designer (Brand)"
    )
    assert sanitize_title("Data Scientist | Remote | Poland") == "Data Scientist"
    assert (
        sanitize_title("Front-end Developer") == "Front-end Developer"
    )  # Should not split on hyphens without spaces
    assert sanitize_title("Machine Learning Engineer - Warsaw") == "Machine Learning Engineer"
    assert sanitize_title("AI Consultant • Full Time") == "AI Consultant"


def test_extract_meta_locations():
    html = """
    <html>
        <head>
            <meta property="og:locality" content="Warsaw" />
            <meta name="jobLocation" content="Remote" />
        </head>
        <body></body>
    </html>
    """
    locs = extract_meta_locations(html)
    assert locs == ["Warsaw", "Remote"]
