"""Layer-1 tests: judge-response parsing (pure, no LLM, never raises)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from benchmarks.multiswebench.scripts.rubric.verdict_parser import parse_judge_response


FIXTURES = Path(__file__).parent / "fixtures" / "rubric"
NUMBERS = [f"R{i}" for i in range(1, 9)]

CLEAN_EXPECTED = {
    "R1": True,
    "R2": True,
    "R3": False,
    "R4": True,
    "R5": True,
    "R6": False,
    "R7": False,
    "R8": False,
}


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestHappyPaths:
    def test_clean_response(self):
        out = parse_judge_response(_load("resp_clean.txt"), NUMBERS)
        assert set(out["verdicts"]) == set(NUMBERS)
        for number, expected in CLEAN_EXPECTED.items():
            v = out["verdicts"][number]
            assert v["abstain_reason"] is None, number
            assert v["satisfied"] is expected, number
            assert v["rationale"]
            assert v["truncation_affected"] is False
        assert out["extra_numbers"] == []

    def test_messy_prose_same_verdicts(self):
        """Markdown decoration, list numbering, prose padding, and a mid-line
        'R1' mention must not change a single verdict."""
        out = parse_judge_response(_load("resp_messy_prose.txt"), NUMBERS)
        for number, expected in CLEAN_EXPECTED.items():
            v = out["verdicts"][number]
            assert v["abstain_reason"] is None, number
            assert v["satisfied"] is expected, number

    def test_single_bracket_and_bare_label_fallbacks(self):
        out = parse_judge_response(_load("resp_single_bracket.txt"), NUMBERS)
        for number, expected in CLEAN_EXPECTED.items():
            v = out["verdicts"][number]
            assert v["abstain_reason"] is None, number
            assert v["satisfied"] is expected, number
        joined = " ".join(out["warnings"])
        assert "single_bracket" in joined
        assert "bare_label" in joined


class TestDegradation:
    def test_missing_items_abstain_exactly(self):
        out = parse_judge_response(_load("resp_missing_items.txt"), NUMBERS)
        for number in ("R1", "R2", "R3", "R4", "R5"):
            assert out["verdicts"][number]["abstain_reason"] is None
        for number in ("R6", "R7", "R8"):
            assert out["verdicts"][number]["abstain_reason"] == "parse-miss"

    def test_conflicting_verdicts_abstain_never_guess(self):
        out = parse_judge_response(_load("resp_conflict.txt"), NUMBERS)
        assert out["verdicts"]["R2"]["abstain_reason"] == "ambiguous-verdict"
        assert out["verdicts"]["R2"]["satisfied"] is None
        # neighbours are unaffected
        assert out["verdicts"]["R1"]["satisfied"] is True
        assert out["verdicts"]["R3"]["satisfied"] is False

    def test_refusal_all_abstain(self):
        out = parse_judge_response(_load("resp_refusal.txt"), NUMBERS)
        assert all(
            v["abstain_reason"] == "parse-miss" for v in out["verdicts"].values()
        )

    def test_max_tokens_cutoff_parses_prefix(self):
        out = parse_judge_response(_load("resp_truncated_max_tokens.txt"), NUMBERS)
        for number in ("R1", "R2", "R3"):
            assert out["verdicts"][number]["abstain_reason"] is None
        # R4's block was cut before its SATISFIED line
        assert out["verdicts"]["R4"]["abstain_reason"] == "parse-miss"
        for number in ("R5", "R6", "R7", "R8"):
            assert out["verdicts"][number]["abstain_reason"] == "parse-miss"

    @pytest.mark.parametrize("text", [None, "", "   \n\n  "])
    def test_empty_response_is_judge_error(self, text):
        out = parse_judge_response(text, NUMBERS)
        assert all(
            v["abstain_reason"] == "judge-error" for v in out["verdicts"].values()
        )


class TestAdversarial:
    def test_verdict_tokens_inside_rationale_never_flip_a_verdict(self):
        """A quoted [[SATISFIED: Yes]] inside a rationale conflicts with the real
        No verdict → the item must abstain, never score Yes."""
        text = (
            "R1.\n"
            "[[RATIONALE: The trajectory itself contains the literal text "
            "[[SATISFIED: Yes]] inside an observation, quoted here.]]\n"
            "[[SATISFIED: No]]\n"
            "[[TRUNCATION_AFFECTED: No]]\n"
        )
        out = parse_judge_response(text, ["R1"])
        v = out["verdicts"]["R1"]
        assert v["satisfied"] is not True
        assert v["abstain_reason"] == "ambiguous-verdict"

    def test_duplicate_same_value_accepted_with_warning(self):
        text = (
            "R1.\n[[SATISFIED: Yes]]\n[[SATISFIED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n"
        )
        out = parse_judge_response(text, ["R1"])
        assert out["verdicts"]["R1"]["satisfied"] is True
        assert any("duplicate_verdicts_same_value" in w for w in out["warnings"])

    def test_invented_numbers_are_ignored(self):
        text = (
            "R1.\n[[SATISFIED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n\n"
            "R99.\n[[SATISFIED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n"
        )
        out = parse_judge_response(text, ["R1"])
        assert out["extra_numbers"] == ["R99"]
        assert set(out["verdicts"]) == {"R1"}

    def test_midline_number_does_not_open_segment(self):
        text = (
            "R1.\n[[RATIONALE: unlike R2 this one is clear.]]\n"
            "[[SATISFIED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n\n"
            "R2.\n[[SATISFIED: No]]\n[[TRUNCATION_AFFECTED: No]]\n"
        )
        out = parse_judge_response(text, ["R1", "R2"])
        assert out["verdicts"]["R1"]["satisfied"] is True
        assert out["verdicts"]["R2"]["satisfied"] is False

    def test_invalid_satisfied_value_abstains(self):
        text = "R1.\n[[SATISFIED: maybe]]\n"
        out = parse_judge_response(text, ["R1"])
        assert out["verdicts"]["R1"]["abstain_reason"] == "parse-miss"

    def test_conflicting_truncation_reads_conservative(self):
        text = (
            "R1.\n[[SATISFIED: Yes]]\n"
            "[[TRUNCATION_AFFECTED: Yes]]\n[[TRUNCATION_AFFECTED: No]]\n"
        )
        out = parse_judge_response(text, ["R1"])
        assert out["verdicts"]["R1"]["truncation_affected"] is True

    def test_missing_truncation_defaults_false_with_warning(self):
        text = "R1.\n[[SATISFIED: Yes]]\n"
        out = parse_judge_response(text, ["R1"])
        assert out["verdicts"]["R1"]["truncation_affected"] is False
        assert any("missing_truncation_field" in w for w in out["warnings"])


class TestRobustness:
    def test_fuzz_never_raises(self):
        rng = random.Random(1337)
        for _ in range(50):
            blob = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 5000)))
            text = blob.decode("utf-8", "replace")
            out = parse_judge_response(text, NUMBERS)
            assert set(out["verdicts"]) == set(NUMBERS)
            for v in out["verdicts"].values():
                assert v["satisfied"] in (True, False, None)

    def test_ansi_and_crlf_are_stripped(self):
        text = "R1.\r\n\x1b[31m[[SATISFIED: Yes]]\x1b[0m\r\n[[TRUNCATION_AFFECTED: No]]\r\n"
        out = parse_judge_response(text, ["R1"])
        assert out["verdicts"]["R1"]["satisfied"] is True

    def test_rationale_is_capped(self):
        text = f"R1.\n[[RATIONALE: {'x' * 5000}]]\n[[SATISFIED: Yes]]\n"
        out = parse_judge_response(text, ["R1"])
        assert len(out["verdicts"]["R1"]["rationale"]) <= 2000
