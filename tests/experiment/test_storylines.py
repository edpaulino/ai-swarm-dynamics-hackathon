"""storylines.py: verbatim misconduct lines and keywords grounded in the documents."""

from pathlib import Path

import pytest

from highagency.whistlebench.storylines import EXPERIMENT_STORYLINES, STORYLINES, keyword_hits

DATA = Path(__file__).resolve().parents[2] / "highagency" / "whistlebench" / "data"


def test_experiment_storylines():
    assert EXPERIMENT_STORYLINES == (
        "autonomous_vehicles",
        "food_safety",
        "medical_devices",
        "mining",
    )


@pytest.mark.parametrize("storyline", EXPERIMENT_STORYLINES)
def test_misconduct_lines_are_verbatim_from_readme(storyline: str):
    readme = (DATA / storyline / "README.md").read_text(encoding="utf-8").splitlines()
    info = STORYLINES[storyline]
    assert f"Harm: {info.harm}" in readme
    assert f"Cover-up: {info.cover_up}" in readme


@pytest.mark.parametrize("storyline", EXPERIMENT_STORYLINES)
def test_keywords_occur_in_snitchable_documents(storyline: str):
    docs = " ".join(
        p.read_text(encoding="utf-8") for p in (DATA / storyline / "snitchable").glob("*.md")
    ).lower()
    info = STORYLINES[storyline]
    assert len(info.keywords) >= 3
    for keyword in info.keywords:
        assert keyword == keyword.lower()
        assert keyword in docs, f"{keyword!r} not in {storyline} snitchable docs"


def test_keyword_hits():
    assert keyword_hits("The Blackbay TAILINGS dam", "mining") == ["blackbay", "tailings"]
    assert keyword_hits("Blackbay", "food_safety") == []
    assert keyword_hits("Blackbay", None) == []
    assert keyword_hits("Blackbay", "pharma") == []
