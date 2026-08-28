from pathlib import Path

from job_scout.sources import load_sources


def test_source_registry_has_the_approved_companies():
    sources = load_sources(Path("config/sources.json"))
    assert {source.company for source in sources} == {
        "ElevenLabs",
        "Netflix",
        "Revolut",
        "InPost",
        "T-Mobile",
        "Xebia",
        "Lingaro",
        "NASK",
        "COI",
        "OPI PIB",
        "PwC",
        "Sigmoidal",
        "Addepto",
        "deepsense.ai",
        "Tooploox",
        "EPAM",
        "GlobalLogic",
        "Google",
        "Microsoft",
        "Amazon/AWS",
        "Cisco/Splunk",
        "Allegro",
        "Docplanner",
        "airSlate",
        "Zowie",
        "Sii",
        "SoftServe",
        "VeloBank",
    }


def test_sigmoidal_empty_state_is_explicitly_documented():
    sources = load_sources(Path("config/sources.json"))
    sigmoidal = next(source for source in sources if source.id == "sigmoidal")
    assert sigmoidal.options["empty_is_valid"] is True
    assert "No open roles" in sigmoidal.options["empty_evidence"]
