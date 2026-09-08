"""Pins for the automatic difficulty banding at the end of `assay score --write`.

The load-bearing behaviour is that a tier is written only once every run is both
judged (final_score.md) and published (score_eval in result.json). Those two can
diverge, and banding on the verdict alone would take the mean over the runs that
happened to publish -- which, since a missing run can only ever have raised the
mean, ships a task labelled harder than it is.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from assay.bundle import TaskBundle
from assay.cli import _autoband_difficulty


def _run(
    bundle: Path,
    index: int,
    *,
    model: str = "opus-5",
    score: float | None = 0.30,
    judged: bool = True,
) -> None:
    run = bundle / "trajectories" / model / f"run_{index}"
    (run / "verifier").mkdir(parents=True)
    if judged:
        (run / "verifier" / "final_score.md").write_text("# score\n", encoding="utf-8")
    scores = {} if score is None else {"score_eval": score}
    (run / "result.json").write_text(
        json.dumps({"verifier_result": {"scores": scores}}), encoding="utf-8"
    )


def _bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(
        'schema_version = "1.0"\n\n[metadata]\ndifficulty = "unbanded"\n',
        encoding="utf-8",
    )
    return root


def _difficulty_of(bundle: Path) -> str:
    parsed = tomllib.loads((bundle / "task.toml").read_text(encoding="utf-8"))
    return parsed["metadata"]["difficulty"]


def test_bands_once_every_run_is_judged_and_published(tmp_path):
    bundle = _bundle(tmp_path / "b")
    for index in range(1, 9):
        _run(bundle, index)
    message = _autoband_difficulty(TaskBundle(root=bundle))
    assert "set opus-5" in message and "-> hard" in message
    assert _difficulty_of(bundle) == "hard"


def test_an_unjudged_run_blocks_banding(tmp_path):
    bundle = _bundle(tmp_path / "b")
    for index in range(1, 8):
        _run(bundle, index)
    _run(bundle, 8, judged=False)
    message = _autoband_difficulty(TaskBundle(root=bundle))
    assert "1/8 run(s) still without final_score.md" in message
    assert _difficulty_of(bundle) == "unbanded"


def test_a_judged_run_without_score_eval_blocks_banding(tmp_path):
    # The run has a verdict on disk but never published a number. Banding here
    # would take the mean over the other seven and label the task too hard.
    bundle = _bundle(tmp_path / "b")
    for index in range(1, 8):
        _run(bundle, index)
    _run(bundle, 8, score=None)
    message = _autoband_difficulty(TaskBundle(root=bundle))
    assert "1/8 run(s) judged but without score_eval" in message
    assert _difficulty_of(bundle) == "unbanded"


def test_a_missing_result_json_blocks_banding(tmp_path):
    bundle = _bundle(tmp_path / "b")
    for index in range(1, 8):
        _run(bundle, index)
    _run(bundle, 8)
    (bundle / "trajectories" / "opus-5" / "run_8" / "result.json").unlink()
    message = _autoband_difficulty(TaskBundle(root=bundle))
    assert "judged but without score_eval" in message
    assert _difficulty_of(bundle) == "unbanded"


def test_a_second_model_does_not_move_the_band(tmp_path):
    bundle = _bundle(tmp_path / "b")
    for index in range(1, 9):
        _run(bundle, index, score=0.30)
        _run(bundle, index, model="glm-5.3", score=0.95)
    message = _autoband_difficulty(TaskBundle(root=bundle))
    assert "ignored glm-5.3" in message
    assert _difficulty_of(bundle) == "hard"


def test_a_bundle_with_no_reference_run_is_refused(tmp_path):
    bundle = _bundle(tmp_path / "b")
    for index in range(1, 9):
        _run(bundle, index, model="glm-5.3")
    message = _autoband_difficulty(TaskBundle(root=bundle))
    assert message == "difficulty: not banded, no opus-5 run (found glm-5.3)"
    assert _difficulty_of(bundle) == "unbanded"


def test_a_task_with_no_runs_is_reported(tmp_path):
    bundle = _bundle(tmp_path / "b")
    assert (
        _autoband_difficulty(TaskBundle(root=bundle)) == "difficulty: no runs to band"
    )
    assert _difficulty_of(bundle) == "unbanded"
