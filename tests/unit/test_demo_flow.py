from pathlib import Path

from job_scout.demo_flow import load_extractions

ROOT = Path(__file__).parents[2]


def test_loads_all_frozen_qwen_extractions():
    extractions = load_extractions(ROOT / "data/demo/extractor-benchmark-qwen-v3.json")
    assert len(extractions) == 10
    assert ("Addepto", "AI Engineer") in extractions
