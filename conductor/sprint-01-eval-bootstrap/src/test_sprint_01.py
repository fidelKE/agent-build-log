"""
Sprint 0 — Eval Dataset Tests

Tests the structure, completeness, and quality of conductor-v1.yaml.
These tests validate the eval dataset itself, not an agent implementation.

Run: pytest test_sprint_00.py -v
"""

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DATASET_PATH = Path(__file__).parents[1] / "evals" / "datasets" / "conductor-v1.yaml"

REQUIRED_FIELDS = {"id", "mode", "difficulty", "input", "expected_output", "must_not_contain", "rationale"}
VALID_MODES = {"setup", "onboarding", "troubleshooting", "qa"}
VALID_DIFFICULTIES = {"easy", "medium", "hard", "adversarial"}

EXPECTED_ADVERSARIAL_CATEGORIES = {
    "prompt_injection_via_content",
    "credential_fishing",
    "sycophancy",
    "context_leakage",
    "scope_creep_escalation",
    "specification_gaming",
    "authority_spoofing",
    "ssrf_via_connector_config",
    "infinite_ambiguity_loop",
}

EXPECTED_DISTRIBUTION = {
    "easy": 16,
    "medium": 12,
    "hard": 3,
    "adversarial": 9,
}


@pytest.fixture(scope="module")
def dataset():
    with open(DATASET_PATH) as f:
        data = yaml.safe_load(f)
    return data["cases"]


# ---------------------------------------------------------------------------
# Structural tests
# ---------------------------------------------------------------------------

def test_dataset_file_exists():
    """Dataset file must exist at the expected path."""
    assert DATASET_PATH.exists(), f"Dataset not found at {DATASET_PATH}"


def test_total_case_count(dataset):
    """Dataset must contain exactly 40 cases."""
    assert len(dataset) == 40, f"Expected 40 cases, got {len(dataset)}"


def test_no_duplicate_ids(dataset):
    """Every case must have a unique ID."""
    ids = [c["id"] for c in dataset]
    duplicates = [id_ for id_ in ids if ids.count(id_) > 1]
    assert not duplicates, f"Duplicate IDs found: {set(duplicates)}"


def test_all_required_fields_present(dataset):
    """Every case must have all required fields."""
    missing = {}
    for case in dataset:
        absent = REQUIRED_FIELDS - set(case.keys())
        if absent:
            missing[case.get("id", "UNKNOWN")] = absent
    assert not missing, f"Cases missing required fields: {missing}"


def test_all_modes_are_valid(dataset):
    """Every case must have a valid mode value."""
    invalid = {c["id"]: c["mode"] for c in dataset if c["mode"] not in VALID_MODES}
    assert not invalid, f"Cases with invalid mode: {invalid}"


def test_all_difficulties_are_valid(dataset):
    """Every case must have a valid difficulty value."""
    invalid = {c["id"]: c["difficulty"] for c in dataset if c["difficulty"] not in VALID_DIFFICULTIES}
    assert not invalid, f"Cases with invalid difficulty: {invalid}"


# ---------------------------------------------------------------------------
# Coverage tests
# ---------------------------------------------------------------------------

def test_all_four_modes_represented(dataset):
    """All four Conductor capability modes must be present in the dataset."""
    present_modes = {c["mode"] for c in dataset}
    missing = VALID_MODES - present_modes
    assert not missing, f"Missing modes: {missing}"


def test_difficulty_distribution(dataset):
    """
    Difficulty distribution must match the Sprint 0 spec:
    16 easy, 12 medium, 3 hard, 9 adversarial.
    """
    counts = {}
    for case in dataset:
        counts[case["difficulty"]] = counts.get(case["difficulty"], 0) + 1

    for difficulty, expected_count in EXPECTED_DISTRIBUTION.items():
        actual = counts.get(difficulty, 0)
        assert actual == expected_count, (
            f"Expected {expected_count} '{difficulty}' cases, got {actual}"
        )


def test_all_adversarial_categories_represented(dataset):
    """All 9 adversarial attack categories must have at least one case."""
    adversarial_cases = [c for c in dataset if c["difficulty"] == "adversarial"]
    present_categories = {c.get("adversarial_category") for c in adversarial_cases}
    missing = EXPECTED_ADVERSARIAL_CATEGORIES - present_categories
    assert not missing, f"Missing adversarial categories: {missing}"


def test_adversarial_cases_have_category_field(dataset):
    """Every adversarial case must have an adversarial_category field."""
    missing_category = [
        c["id"] for c in dataset
        if c["difficulty"] == "adversarial" and not c.get("adversarial_category")
    ]
    assert not missing_category, f"Adversarial cases missing category field: {missing_category}"


def test_non_adversarial_cases_have_no_category_field(dataset):
    """Non-adversarial cases must not have an adversarial_category field."""
    unexpected = [
        c["id"] for c in dataset
        if c["difficulty"] != "adversarial" and c.get("adversarial_category")
    ]
    assert not unexpected, f"Non-adversarial cases with category field: {unexpected}"


# ---------------------------------------------------------------------------
# Quality tests
# ---------------------------------------------------------------------------

def test_all_expected_outputs_are_lists(dataset):
    """expected_output must be a list, not a string."""
    not_lists = {c["id"] for c in dataset if not isinstance(c["expected_output"], list)}
    assert not not_lists, f"Cases with non-list expected_output: {not_lists}"


def test_all_expected_outputs_are_non_empty(dataset):
    """Every case must have at least one expected output item."""
    empty = {c["id"] for c in dataset if not c["expected_output"]}
    assert not empty, f"Cases with empty expected_output: {empty}"


def test_must_not_contain_is_a_list(dataset):
    """must_not_contain must be a list (can be empty)."""
    not_lists = {c["id"] for c in dataset if not isinstance(c["must_not_contain"], list)}
    assert not not_lists, f"Cases with non-list must_not_contain: {not_lists}"


def test_no_vague_expected_outputs(dataset):
    """
    Expected output items must not contain vague language that cannot be verified.
    Flags items that are too generic to be useful as eval criteria.
    """
    vague_phrases = [
        "should help",
        "be helpful",
        "provide assistance",
        "assist the user",
        "appropriate response",
        "relevant information",
    ]
    vague_cases = {}
    for case in dataset:
        for item in case["expected_output"]:
            for phrase in vague_phrases:
                if phrase.lower() in item.lower():
                    vague_cases.setdefault(case["id"], []).append(item)

    assert not vague_cases, (
        f"Cases with vague expected output items (too generic to verify):\n"
        + "\n".join(f"  {id_}: {items}" for id_, items in vague_cases.items())
    )


def test_rationale_is_non_empty(dataset):
    """Every case must have a non-empty rationale explaining what failure it catches."""
    empty = {c["id"] for c in dataset if not str(c.get("rationale", "")).strip()}
    assert not empty, f"Cases with empty rationale: {empty}"


def test_inputs_are_non_empty(dataset):
    """Every case must have a non-empty input."""
    empty = {c["id"] for c in dataset if not str(c.get("input", "")).strip()}
    assert not empty, f"Cases with empty input: {empty}"


def test_adversarial_cases_have_must_not_contain(dataset):
    """
    Adversarial cases must have at least one must_not_contain item.
    These cases are specifically about catching bad responses — there must
    be something the agent must not do.
    """
    missing = [
        c["id"] for c in dataset
        if c["difficulty"] == "adversarial" and not c.get("must_not_contain")
    ]
    assert not missing, (
        f"Adversarial cases must have at least one must_not_contain item: {missing}"
    )


def test_mode_distribution_across_adversarial(dataset):
    """
    Adversarial cases should cover at least 3 different modes.
    Attacks can happen in any interaction mode.
    """
    adv_modes = {c["mode"] for c in dataset if c["difficulty"] == "adversarial"}
    assert len(adv_modes) >= 3, (
        f"Adversarial cases only cover {len(adv_modes)} mode(s): {adv_modes}. "
        f"Expected coverage across at least 3 modes."
    )


def test_each_mode_has_easy_cases(dataset):
    """
    Every mode must have at least one easy case.
    Easy cases establish the baseline happy-path behavior per mode.
    """
    for mode in VALID_MODES:
        easy_in_mode = [
            c for c in dataset if c["mode"] == mode and c["difficulty"] == "easy"
        ]
        assert easy_in_mode, f"Mode '{mode}' has no easy cases"


def test_no_must_not_contain_in_expected_output(dataset):
    """
    must_not_contain items must not appear inside expected_output items.

    If a forbidden string is present in the expected output spec, the evaluator
    will flag a correct agent response as failing — because the response would
    naturally contain the expected output text, which itself contains the
    forbidden string. This is an evaluator poison bug, not a content issue.
    """
    conflicts = {}
    for case in dataset:
        for forbidden in case.get("must_not_contain", []):
            for expected in case.get("expected_output", []):
                if forbidden.lower() in expected.lower():
                    conflicts.setdefault(case["id"], []).append(
                        f'forbidden "{forbidden}" appears in expected: "{expected[:80]}"'
                    )
    assert not conflicts, (
        "must_not_contain items found inside expected_output items (evaluator poison):\n"
        + "\n".join(f"  {id_}:\n" + "\n".join(f"    - {m}" for m in msgs)
                   for id_, msgs in conflicts.items())
    )


def test_expected_output_items_are_strings(dataset):
    """
    Every item in expected_output must be a plain string, not a dict.

    YAML list items containing unquoted colons (e.g. '- key: value') parse as
    dicts instead of strings. A dict item silently breaks any evaluator that
    calls .lower() or does string comparison on expected output items.
    """
    non_strings = {}
    for case in dataset:
        bad = [item for item in case.get("expected_output", []) if not isinstance(item, str)]
        if bad:
            non_strings[case["id"]] = bad
    assert not non_strings, (
        "expected_output items that are not strings (likely unquoted colon in YAML):\n"
        + "\n".join(f"  {id_}: {items}" for id_, items in non_strings.items())
    )


def test_must_not_contain_items_are_strings(dataset):
    """
    Every item in must_not_contain must be a plain string, not a dict.
    Same YAML colon-parsing risk as expected_output.
    """
    non_strings = {}
    for case in dataset:
        bad = [item for item in case.get("must_not_contain", []) if not isinstance(item, str)]
        if bad:
            non_strings[case["id"]] = bad
    assert not non_strings, (
        "must_not_contain items that are not strings:\n"
        + "\n".join(f"  {id_}: {items}" for id_, items in non_strings.items())
    )
