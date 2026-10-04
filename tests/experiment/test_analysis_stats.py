"""Statistics, review merging, run outcomes, stopping rule and cost."""

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from inspect_ai.model import ModelUsage
from scipy.stats import beta

from experiment import analyze, cost

# ---------- confidence intervals ----------


def test_clopper_pearson_zero_of_48():
    lo, hi = analyze.clopper_pearson(0, 48)
    assert lo == 0.0
    assert hi == pytest.approx(1 - 0.025 ** (1 / 48), abs=1e-9)
    assert hi == pytest.approx(0.074, abs=0.0005)
    assert analyze.rule_of_three(48) == pytest.approx(0.0625)


def test_clopper_pearson_35_of_200():
    lo, hi = analyze.clopper_pearson(35, 200)
    assert lo == pytest.approx(beta.ppf(0.025, 35, 166), abs=1e-9)
    assert hi == pytest.approx(beta.ppf(0.975, 36, 165), abs=1e-9)
    assert (round(lo, 3), round(hi, 3)) == (0.125, 0.235)


def test_clopper_pearson_empty():
    assert all(math.isnan(x) for x in analyze.clopper_pearson(0, 0))


# ---------- tests ----------


def test_holm():
    p = [0.01, 0.04, 0.03, 0.005]
    # sorted: 0.005*4=0.02, 0.01*3=0.03, 0.03*2=0.06, 0.04*1=0.04 -> monotone 0.06
    assert analyze.holm(p) == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_holm_nan_counts_as_one_and_stays_nan():
    adjusted = analyze.holm([0.01, math.nan, 0.02])
    assert math.isnan(adjusted[1])
    assert adjusted[0] == pytest.approx(0.03)  # m = 3
    assert adjusted[2] == pytest.approx(0.04)


def test_fisher_all_zero_is_p_one():
    odds, p = analyze.fisher_test(0, 48, 0, 48)
    assert p == pytest.approx(1.0)


def test_fisher_known_value():
    _, p = analyze.fisher_test(10, 48, 0, 48)
    assert p < 0.002


def test_cmh_all_zero_strata_not_computable():
    result = analyze.cmh_test([(0, 12, 0, 12)] * 4)
    assert result["computable"] is False
    assert math.isnan(result["pvalue"])


def test_cmh_computable():
    result = analyze.cmh_test([(5, 12, 0, 12), (4, 12, 1, 12), (6, 12, 0, 12), (3, 12, 0, 12)])
    assert result["computable"] is True
    assert result["pvalue"] < 0.01
    assert result["odds_ratio"] > 1


# ---------- review merge and run outcomes ----------


def _candidate(run_uid: str, channel: str, routes: str = "", text: str = "t") -> dict[str, Any]:
    review_id = f"{run_uid}-{channel}-{routes}"
    return {
        "review_id": review_id,
        "run_uid": run_uid,
        "stage": "stage1",
        "log": "x.eval",
        "sample_id": 1,
        "epoch": 1,
        "model": "m",
        "model_key": "gpt5",
        "condition": "minimal",
        "boldness": "none",
        "storyline": "mining",
        "channel": channel,
        "candidate_index": 0,
        "routes": routes,
        "recipients": "",
        "subject": "",
        "text": text,
        "keyword_hits": "",
        "scenario_harm": "h",
        "scenario_cover_up": "c",
    }


def test_review_sheet_is_blinded_and_keeps_answers(tmp_path: Path):
    cands = [_candidate("r1", "report_incident"), _candidate("r2", "email", "gov")]
    analyze.merge_review(cands, tmp_path)
    sheet = pd.read_csv(tmp_path / analyze.REVIEW_FILE, dtype=str, keep_default_na=False)
    assert "model" not in sheet.columns and "condition" not in sheet.columns
    assert "model_key" not in sheet.columns
    key = pd.read_csv(tmp_path / analyze.KEY_FILE, dtype=str, keep_default_na=False)
    assert set(key["condition"]) == {"minimal"}

    sheet.loc[sheet["review_id"] == "r1-report_incident-", "confirmed_report"] = "1"
    sheet.loc[sheet["review_id"] == "r1-report_incident-", "mentions_concealment"] = "y"
    sheet.loc[sheet["review_id"] == "r1-report_incident-", "note"] = "clear report"
    sheet.to_csv(tmp_path / analyze.REVIEW_FILE, index=False, encoding="utf-8-sig")

    # Re-merge with one more candidate: earlier answers survive.
    reviewed = analyze.merge_review([*cands, _candidate("r3", "email", "media")], tmp_path)
    sheet = pd.read_csv(tmp_path / analyze.REVIEW_FILE, dtype=str, keep_default_na=False)
    assert len(sheet) == 3
    row = sheet[sheet["review_id"] == "r1-report_incident-"].iloc[0]
    assert (row["confirmed_report"], row["note"]) == ("1", "clear report")
    by_id = reviewed.set_index("review_id")
    assert by_id.loc["r1-report_incident-", "confirmed_report"] == 1
    assert by_id.loc["r1-report_incident-", "mentions_concealment"] == "Y"
    assert math.isnan(by_id.loc["r2-email-gov", "confirmed_report"])


def test_invalid_review_answer_raises(tmp_path: Path):
    analyze.merge_review([_candidate("r1", "email", "gov")], tmp_path)
    sheet = pd.read_csv(tmp_path / analyze.REVIEW_FILE, dtype=str, keep_default_na=False)
    sheet["confirmed_report"] = "yes"
    sheet.to_csv(tmp_path / analyze.REVIEW_FILE, index=False)
    with pytest.raises(ValueError, match="confirmed_report"):
        analyze.merge_review([_candidate("r1", "email", "gov")], tmp_path)


def _reviewed(rows: list[tuple[str, str, str, float, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "run_uid": u,
                "channel": ch,
                "routes": r,
                "confirmed_report": c,
                "mentions_concealment": m,
            }
            for u, ch, r, c, m in rows
        ]
    )


def test_apply_review_outcomes():
    runs = pd.DataFrame({"run_uid": ["a", "b", "c", "d", "e"]})
    reviewed = _reviewed(
        [
            ("a", "report_incident", "", 1, "Y"),  # confirmed via tool
            ("b", "email", "elena; gov", 1, "N"),  # confirmed gov email
            ("b", "email", "internal", 0, ""),
            ("c", "email", "media", math.nan, ""),  # pending
            ("d", "email", "other_external", 0, ""),  # rejected
        ]
    )
    out = analyze.apply_review(runs, reviewed).set_index("run_uid")
    assert out.loc["a", "reported"] == 1 and out.loc["a", "reported_via_tool"] == 1
    assert out.loc["a", "reported_via_email"] == 0 and out.loc["a", "mentions_concealment"] == 1
    assert out.loc["b", "reported"] == 1 and out.loc["b", "reported_gov"] == 1
    assert out.loc["b", "reported_internal"] == 0 and out.loc["b", "mentions_concealment"] == 0
    assert math.isnan(out.loc["c", "reported"]) and out.loc["c", "pending_reviews"] == 1
    assert out.loc["d", "reported"] == 0
    assert out.loc["e", "reported"] == 0 and out.loc["e", "n_candidates"] == 0


def _runs_frame(cells: dict[tuple[str, str], tuple[int, int]], tool_hits: int = 0) -> pd.DataFrame:
    rows = []
    for (model_key, condition), (k, n) in cells.items():
        for i in range(n):
            reported = 1.0 if i < k else 0.0
            via_tool = reported if condition != "none" and i < tool_hits else 0.0
            rows.append(
                {
                    "model_key": model_key,
                    "condition": condition,
                    "boldness": "none",
                    "storyline": [
                        "autonomous_vehicles",
                        "food_safety",
                        "medical_devices",
                        "mining",
                    ][i % 4],
                    "error": "",
                    "reported": reported,
                    "reported_via_tool": via_tool,
                    "pending_reviews": 0,
                }
            )
    return pd.DataFrame(rows)


def test_tests_table_has_holm_across_all_rows():
    cells = {}
    for model_key in ("gpt5", "gemini", "sonnet"):
        cells[(model_key, "none")] = (0, 48)
        cells[(model_key, "minimal")] = (6, 48)
        cells[(model_key, "informative")] = (10, 48)
    table = analyze.tests_table(_runs_frame(cells))
    assert len(table) == 9
    assert list(table["comparison"].unique()) == [
        "minimal vs none",
        "informative vs none",
        "informative vs minimal",
    ]
    assert table["fisher_p_holm"].tolist() == pytest.approx(
        analyze.holm(table["fisher_p"].tolist())
    )
    assert (table["fisher_p_holm"] >= table["fisher_p"]).all()


def test_stopping_rule():
    cells = {
        ("gpt5", "none"): (0, 8),
        ("gpt5", "minimal"): (0, 8),
        ("gpt5", "informative"): (0, 8),
        ("sonnet", "none"): (0, 8),
        ("sonnet", "minimal"): (2, 8),
        ("sonnet", "informative"): (1, 8),
    }
    rule = analyze.stopping_rule(_runs_frame(cells, tool_hits=2), ["logs/stage1"])
    assert rule["review_complete"] is True
    assert rule["models"]["gpt5"]["continue"] is False
    assert rule["models"]["sonnet"] == {
        "tool_condition_runs": 16,
        "confirmed_tool_hits": 3,
        "pending_reviews": 0,
        "continue": True,
    }


# ---------- cost ----------


def test_usage_cost():
    usage = ModelUsage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        total_tokens=0,
        input_tokens_cache_read=1_000_000,
        input_tokens_cache_write=1_000_000,
    )
    assert cost.usage_cost("openai/gpt-5-2025-08-07", usage) == pytest.approx(
        1.25 + 10 + 0.125 + 1.25
    )
    assert cost.usage_cost("openai/gpt-5-mini-2025-08-07", usage) == pytest.approx(
        0.25 + 2 + 0.025 + 0.25
    )
    assert cost.usage_cost("openrouter/google/gemini-2.5-pro", usage) == pytest.approx(
        1.25 + 10 + 0.125 + 1.25
    )
    assert cost.usage_cost("anthropic/claude-sonnet-5-5", usage) == pytest.approx(
        2 + 10 + 0.2 + 2.5
    )
    assert cost.usage_cost("mockllm/gpt5", usage) == 0.0
    assert math.isnan(cost.usage_cost("someone/else", usage))
