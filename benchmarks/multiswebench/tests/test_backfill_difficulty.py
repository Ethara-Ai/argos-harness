"""Pins for the difficulty backfill.

Difficulty is the reference model's pass rate, so it is written after the
rollout rather than at conversion. The load-bearing behaviours are that the
band comes from the mean of the reference model's own runs and no other
model's, that a bundle whose runs did not all score is reported instead of
silently banded from the survivors, and that preflight writes nothing.
"""

from __future__ import annotations

import json
import tomllib
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pytest

from benchmarks.multiswebench.scripts.harbor.backfill_difficulty import (
    REFERENCE_MODEL,
    TARGET_MIX,
    backfill_bundle,
    mix_report,
)


def _bundle(
    root: Path,
    rates: Sequence[float | None],
    difficulty: str = "unbanded",
    model: str = REFERENCE_MODEL,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(
        f'schema_version = "1.0"\n\n[metadata]\ndifficulty = "{difficulty}"\n',
        encoding="utf-8",
    )
    for index, rate in enumerate(rates, 1):
        run = root / "trajectories" / model / f"run_{index}"
        run.mkdir(parents=True)
        scores = {} if rate is None else {"score_eval": rate}
        (run / "result.json").write_text(
            json.dumps({"verifier_result": {"scores": scores}}), encoding="utf-8"
        )
    return root


def _difficulty_of(bundle: Path) -> str:
    parsed = tomllib.loads((bundle / "task.toml").read_text(encoding="utf-8"))
    return parsed["metadata"].get("difficulty", "")


@pytest.mark.parametrize(
    ("pass_rate", "expected"),
    [
        (0.0, "expert"),
        (0.2499, "expert"),
        (0.25, "hard"),
        (0.3749, "hard"),
        (0.375, "medium"),
        (0.4999, "medium"),
        (0.50, "easy"),
        (0.8749, "easy"),
        (0.875, "trivial"),
        (1.0, "trivial"),
    ],
)
def test_single_run_bands_on_that_run(tmp_path, pass_rate, expected):
    bundle = _bundle(tmp_path / "b", [pass_rate])
    result = backfill_bundle(bundle)
    assert result.ok
    assert result.difficulty == expected
    assert _difficulty_of(bundle) == expected


def test_band_is_the_mean_not_the_best_attempt(tmp_path):
    # Seven near-failures and one solve average to 0.1588, so the run set bands
    # expert. The best attempt alone would have said trivial.
    bundle = _bundle(tmp_path / "b", [0.05] * 7 + [0.92])
    result = backfill_bundle(bundle)
    assert result.difficulty == "expert"
    assert "mean=0.1588" in result.message
    assert "n=8" in result.message


def test_only_the_reference_model_moves_the_band(tmp_path):
    # A second model's runs ride along in the same bundle for comparison. They
    # must not pull the shipped tier, which is opus-5's alone.
    bundle = _bundle(tmp_path / "b", [0.40] * 4)
    _bundle(bundle, [0.05] * 4, model="glm-5.3")
    result = backfill_bundle(bundle)
    assert result.difficulty == "medium"
    assert "ignored glm-5.3" in result.message
    assert _difficulty_of(bundle) == "medium"


def test_a_non_reference_model_can_band_on_request(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.40] * 4)
    _bundle(bundle, [0.05] * 4, model="glm-5.3")
    result = backfill_bundle(bundle, model="glm-5.3")
    assert result.difficulty == "expert"
    assert _difficulty_of(bundle) == "expert"


def test_bundle_without_a_reference_run_is_refused(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.40] * 4, model="glm-5.3")
    result = backfill_bundle(bundle)
    assert not result.ok
    assert result.difficulty is None
    assert result.message == "no opus-5 run, found glm-5.3"
    assert _difficulty_of(bundle) == "unbanded"


def test_partial_run_set_is_flagged_not_silently_banded(tmp_path):
    # A missing run can only ever have raised the maximum, so banding from the
    # survivors labels the task harder than it is. It must not pass unnoticed.
    bundle = _bundle(tmp_path / "b", [0.10, 0.20, 0.30, None, None, None, None, None])
    result = backfill_bundle(bundle)
    assert not result.ok
    assert "PARTIAL 3/8 runs scored" in result.message
    assert result.difficulty == "expert"


def test_complete_run_set_is_not_flagged(tmp_path):
    result = backfill_bundle(_bundle(tmp_path / "b", [0.10] * 8))
    assert result.ok
    assert "PARTIAL" not in result.message


def test_preflight_writes_nothing(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.40])
    result = backfill_bundle(bundle, preflight=True)
    assert result.difficulty == "medium"
    assert "would set" in result.message and "-> medium" in result.message
    assert _difficulty_of(bundle) == "unbanded"


def test_preflight_reports_the_band_and_its_mean(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.40], difficulty="expert")
    message = backfill_bundle(bundle, preflight=True).message
    assert "opus-5: mean=0.4000 n=1 -> medium" in message


def test_rewrite_is_idempotent(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.30])
    assert backfill_bundle(bundle).message.startswith("set ")
    assert backfill_bundle(bundle).message.startswith("already ")
    assert _difficulty_of(bundle) == "hard"


def test_rewrite_keeps_the_rest_of_the_file(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.30])
    backfill_bundle(bundle)
    parsed = tomllib.loads((bundle / "task.toml").read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "1.0"
    assert parsed["metadata"]["difficulty"] == "hard"


def test_rewrite_drops_legacy_per_model_keys(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.30])
    toml = bundle / "task.toml"
    toml.write_text(
        'schema_version = "1.0"\n\n[metadata]\n'
        'difficulty_opus5 = "easy"\ndifficulty_glm53 = "expert"\n'
        'category = "bug_fixing"\n',
        encoding="utf-8",
    )
    assert backfill_bundle(bundle).ok
    parsed = tomllib.loads(toml.read_text(encoding="utf-8"))
    assert parsed["metadata"] == {"difficulty": "hard", "category": "bug_fixing"}


def test_bundle_without_any_difficulty_key_still_bands(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.30])
    toml = bundle / "task.toml"
    toml.write_text(
        'schema_version = "1.0"\n\n[metadata]\ncategory = "bug_fixing"\n',
        encoding="utf-8",
    )
    assert backfill_bundle(bundle).ok
    parsed = tomllib.loads(toml.read_text(encoding="utf-8"))
    assert parsed["metadata"]["difficulty"] == "hard"
    assert parsed["metadata"]["category"] == "bug_fixing"


@pytest.mark.parametrize("expect", [1, 8])
def test_expect_runs_refuses_a_mismatched_protocol(tmp_path, expect):
    bundle = _bundle(tmp_path / "b", [0.40] * 4)
    result = backfill_bundle(bundle, expect_runs=expect)
    assert not result.ok
    assert result.difficulty is None
    assert f"expected {expect} scored opus-5 runs, got 4" in result.message
    assert _difficulty_of(bundle) == "unbanded"


def test_expect_runs_admits_the_declared_protocol(tmp_path):
    assert backfill_bundle(_bundle(tmp_path / "a", [0.40]), expect_runs=1).ok
    assert backfill_bundle(_bundle(tmp_path / "b", [0.40] * 8), expect_runs=8).ok


def test_unscored_bundle_is_left_unbanded(tmp_path):
    bundle = _bundle(tmp_path / "b", [None, None])
    result = backfill_bundle(bundle)
    assert not result.ok
    assert result.difficulty is None
    assert result.message == "no scored opus-5 run (2 result.json found)"
    assert _difficulty_of(bundle) == "unbanded"


def test_bundle_without_task_toml_is_reported(tmp_path):
    bundle = _bundle(tmp_path / "b", [0.40])
    (bundle / "task.toml").unlink()
    result = backfill_bundle(bundle)
    assert not result.ok
    assert result.message == "no task.toml"


def test_mix_report_accepts_the_target_distribution():
    counts = Counter({"easy": 5, "medium": 15, "hard": 20, "expert": 10})
    within, lines = mix_report(counts)
    assert within
    assert not any("off" in line for line in lines)


def test_mix_report_flags_an_expert_heavy_batch():
    # Every task here is banded correctly; only the composition is wrong, which
    # per-task checks cannot see.
    counts = Counter({"easy": 9, "medium": 2, "hard": 3, "expert": 36})
    within, lines = mix_report(counts)
    assert not within
    assert any("expert" in line and "off" in line for line in lines)


def test_mix_report_flags_any_trivial_task():
    counts = Counter({"trivial": 5, "easy": 5, "medium": 15, "hard": 20, "expert": 10})
    within, _ = mix_report(counts)
    assert not within
    assert TARGET_MIX["trivial"] == 0.0


def test_mix_report_handles_an_empty_batch():
    within, lines = mix_report(Counter())
    assert not within
    assert len(lines) == len(TARGET_MIX) + 1
