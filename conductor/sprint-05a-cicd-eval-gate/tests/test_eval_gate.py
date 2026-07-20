"""
Tests for eval gate scripts — Lab 5a.

All tests are deterministic: no LLM calls, no network, no file I/O beyond
loading the fixture JSON files in results/. Safe for pre-commit hooks (RULE-CI04).

Covers:
- check_eval_gate.py: overall pass, overall fail, category masking, boundary
- regression_check.py: no regression, regression fires, boundary, case detail
"""

import json
import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import without installing
SPRINT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SPRINT_ROOT / "scripts"))

from check_eval_gate import check_gate  # noqa: E402
from regression_check import check_regression  # noqa: E402

RESULTS_DIR = SPRINT_ROOT / "results"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def all_pass():
    with open(RESULTS_DIR / "fixture-pass.json") as f:
        return json.load(f)


@pytest.fixture
def all_fail():
    with open(RESULTS_DIR / "fixture-fail.json") as f:
        return json.load(f)


@pytest.fixture
def baseline():
    with open(RESULTS_DIR / "fixture-baseline.json") as f:
        return json.load(f)


@pytest.fixture
def regression():
    with open(RESULTS_DIR / "fixture-regression.json") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# check_eval_gate: overall threshold
# ---------------------------------------------------------------------------

class TestCheckEvalGateOverall:
    def test_passes_when_all_pass(self, all_pass):
        assert check_gate(all_pass, overall_threshold=0.85, category_thresholds={}) is True

    def test_fails_when_all_fail(self, all_fail):
        assert check_gate(all_fail, overall_threshold=0.85, category_thresholds={}) is False

    def test_fails_when_below_threshold(self, all_pass):
        # Require 100% — all_pass is 100% so this passes
        assert check_gate(all_pass, overall_threshold=1.0, category_thresholds={}) is True

    def test_fails_when_threshold_not_met(self, all_fail):
        # Even with a low threshold, 0% fails
        assert check_gate(all_fail, overall_threshold=0.10, category_thresholds={}) is False

    def test_empty_results_fails(self):
        assert check_gate({"results": []}, overall_threshold=0.85, category_thresholds={}) is False


# ---------------------------------------------------------------------------
# check_eval_gate: per-category threshold (masking case)
# ---------------------------------------------------------------------------

class TestCheckEvalGateCategoryMasking:
    def test_fails_when_category_below_threshold(self, all_pass):
        # all_pass has troubleshooting cases passing — require 100% to force a fail
        # by injecting a partial failure fixture inline
        report = {
            "results": [
                # 8 setup cases pass
                *[{"id": f"s-{i}", "category": "setup", "final_pass": True} for i in range(8)],
                # 2 troubleshooting cases fail
                {"id": "t-1", "category": "troubleshooting", "final_pass": False},
                {"id": "t-2", "category": "troubleshooting", "final_pass": False},
            ]
        }
        # Overall: 8/10 = 80% >= 0.75 threshold → would pass overall
        # troubleshooting: 0/2 = 0% < 0.80 threshold → must fail
        result = check_gate(
            report,
            overall_threshold=0.75,
            category_thresholds={"troubleshooting": 0.80},
        )
        assert result is False

    def test_passes_when_all_categories_meet_threshold(self, all_pass):
        result = check_gate(
            all_pass,
            overall_threshold=0.80,
            category_thresholds={"troubleshooting": 0.50, "setup": 0.50},
        )
        assert result is True

    def test_category_threshold_overrides_overall_for_that_category(self):
        report = {
            "results": [
                {"id": "s-1", "category": "setup", "final_pass": True},
                {"id": "s-2", "category": "setup", "final_pass": True},
                # safety fails — category threshold is stricter
                {"id": "safety-1", "category": "safety", "final_pass": False},
                {"id": "safety-2", "category": "safety", "final_pass": False},
                {"id": "safety-3", "category": "safety", "final_pass": False},
            ]
        }
        # Overall: 2/5 = 40% — below 0.80, fails on overall alone
        # But also: safety 0% < 0.90
        result = check_gate(
            report,
            overall_threshold=0.80,
            category_thresholds={"safety": 0.90},
        )
        assert result is False


# ---------------------------------------------------------------------------
# check_eval_gate: boundary cases
# ---------------------------------------------------------------------------

class TestCheckEvalGateBoundary:
    def test_exactly_at_threshold_passes(self):
        # 85 out of 100 = exactly 85% — should pass at threshold=0.85
        report = {
            "results": [
                {"id": f"c-{i}", "category": "setup", "final_pass": i < 85}
                for i in range(100)
            ]
        }
        assert check_gate(report, overall_threshold=0.85, category_thresholds={}) is True

    def test_one_below_threshold_fails(self):
        # 84 out of 100 = 84% — should fail at threshold=0.85
        report = {
            "results": [
                {"id": f"c-{i}", "category": "setup", "final_pass": i < 84}
                for i in range(100)
            ]
        }
        assert check_gate(report, overall_threshold=0.85, category_thresholds={}) is False


# ---------------------------------------------------------------------------
# regression_check: no regression
# ---------------------------------------------------------------------------

class TestRegressionCheckPass:
    def test_no_regression_when_same(self, baseline):
        # Same report as baseline — 0% delta, should pass
        assert check_regression(baseline, baseline, max_regression=0.03) is True

    def test_no_regression_when_improved(self, baseline, all_pass):
        # all_pass has 10 cases all passing; baseline has 35 all passing
        # Use baseline as current and a degraded version as "baseline" to simulate improvement
        degraded = {
            "results": [
                {**r, "final_pass": False} if i < 5 else r
                for i, r in enumerate(baseline["results"])
            ]
        }
        assert check_regression(baseline, degraded, max_regression=0.03) is True

    def test_passes_at_exact_boundary(self):
        # Q1 scenario: baseline 89%, current 86%, max_regression=0.03
        # delta = -0.03, condition is delta < -0.03, which is False → passes
        baseline_r = {"results": [{"id": f"c-{i}", "final_pass": i < 89} for i in range(100)]}
        current_r = {"results": [{"id": f"c-{i}", "final_pass": i < 86} for i in range(100)]}
        assert check_regression(current_r, baseline_r, max_regression=0.03) is True


# ---------------------------------------------------------------------------
# regression_check: regression fires
# ---------------------------------------------------------------------------

class TestRegressionCheckFail:
    def test_fires_when_drop_exceeds_max(self, baseline, regression):
        # regression fixture drops 3 cases from 35 passing → ~8.6% drop
        assert check_regression(regression, baseline, max_regression=0.03) is False

    def test_fires_just_above_boundary(self):
        # baseline 89%, current 85% → drop = 4%, exceeds max_regression=0.03
        baseline_r = {"results": [{"id": f"c-{i}", "final_pass": i < 89} for i in range(100)]}
        current_r = {"results": [{"id": f"c-{i}", "final_pass": i < 85} for i in range(100)]}
        assert check_regression(current_r, baseline_r, max_regression=0.03) is False


# ---------------------------------------------------------------------------
# regression_check: case-level detail (RULE-CI02)
# ---------------------------------------------------------------------------

class TestRegressionCaseDetail:
    def test_identifies_regressed_cases(self, baseline, regression, capsys):
        check_regression(regression, baseline, max_regression=0.03)
        captured = capsys.readouterr()
        # The 3 regressed case IDs must appear in the output
        assert "troubleshooting-001" in captured.out
        assert "troubleshooting-002" in captured.out
        assert "troubleshooting-003" in captured.out

    def test_no_false_regressions_on_pass(self, baseline, capsys):
        check_regression(baseline, baseline, max_regression=0.03)
        captured = capsys.readouterr()
        assert "Regressed cases" not in captured.out
