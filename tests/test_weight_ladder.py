"""Every deterministic check weight sits on the {1, 3, 5} ladder.

TL rule: all weights in delivered artifacts lie in {-5, -3, -1, 1, 3, 5}.
Rubric items already enforce this via lint's ALLOWED_WEIGHTS; these tests pin
the deterministic channel to the positive half of the same ladder — declared
table, engine constants, discount rule, and what evaluate() actually emits.
Weight 0 is not a weight: it marks a row excluded from scoring (the E1 row a
scored E2 requirement already entails).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay.bundle import RunBundle, TaskBundle
from assay.deterministic import (
    DISCOUNT_STEP,
    GENERIC_FAMILIES,
    REQUIREMENT_WEIGHT,
    SITE_WEIGHT,
    Gate,
    evaluate,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "milo_bundles"
UUIDS = [
    "9a959823-e9ff-418e-a2aa-297a9a0735fa",  # tortoise 538
    "150c282e-330b-492e-bcaf-1017bbfff2e8",  # tortoise 943
]

LADDER = {1, 3, 5}


def test_declared_weights_are_on_the_ladder():
    off = {
        cid: f.weight for cid, f in GENERIC_FAMILIES.items() if f.weight not in LADDER
    }
    assert off == {}
    # the change re-weighted, it did not re-gate
    assert sum(1 for f in GENERIC_FAMILIES.values() if f.gate is Gate.HARD) == 14
    assert sum(1 for f in GENERIC_FAMILIES.values() if f.gate is Gate.SOFT) == 14


def test_ladder_constants():
    assert REQUIREMENT_WEIGHT == 5
    assert SITE_WEIGHT == 3
    # discounting steps one rung down the ladder; a multiply-and-round discount
    # would land between rungs (round(5 * 0.5) == 2)
    assert DISCOUNT_STEP == {5: 3, 3: 1}
    src = (REPO_ROOT / "assay" / "deterministic.py").read_text(encoding="utf-8")
    assert "REDUNDANCY_DISCOUNT" not in src


@pytest.mark.parametrize("uuid", UUIDS, ids=["538", "943"])
def test_evaluate_emits_only_ladder_weights(uuid: str):
    task = TaskBundle(FIXTURES / uuid)
    run = RunBundle(FIXTURES / uuid / "trajectories" / "opus-4.8" / "run_1")
    report = evaluate(task, run)
    emitted = {c.weight for c in report.results}
    assert emitted <= LADDER | {0}, emitted


@pytest.mark.parametrize("uuid", UUIDS, ids=["538", "943"])
def test_fixture_process_json_weights_are_on_the_ladder(uuid: str):
    doc = json.loads(
        (
            FIXTURES
            / uuid
            / "trajectories"
            / "opus-4.8"
            / "run_1"
            / "verifier"
            / "process.json"
        ).read_text()
    )
    weights = {c["weight"] for c in doc["detail"]["deterministic"]["checks"]}
    assert weights <= LADDER | {0}, weights
