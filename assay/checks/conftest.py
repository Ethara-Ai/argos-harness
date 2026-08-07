"""Parametrisation for the deterministic channel.

Point at a corpus with ``--assay-delivery`` and narrow with ``--assay-task`` /
``--assay-model``. Every recorded run under the selection becomes one parameter,
so a failure names the exact run it came from.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from assay.bundle import RunBundle, TaskBundle
from assay.deterministic import evaluate
from assay.truth import Truth


DEFAULT_DELIVERY = Path(
    os.environ.get(
        "ASSAY_DELIVERY",
        str(Path(__file__).resolve().parents[2] / "aurora" / "delivery"),
    )
)


def pytest_addoption(parser):
    g = parser.getgroup("assay")
    g.addoption(
        "--assay-delivery",
        default=str(os.environ.get("ASSAY_DELIVERY", DEFAULT_DELIVERY)),
        help="root containing dataset/, each task carrying its own trajectories/",
    )
    g.addoption(
        "--assay-task",
        default=os.environ.get("ASSAY_TASK", ""),
        help="restrict to one task uuid",
    )
    g.addoption(
        "--assay-model",
        default=os.environ.get("ASSAY_MODEL", ""),
        help="restrict to one model directory name",
    )
    g.addoption(
        "--assay-require-verifier",
        action="store_true",
        help="fail rather than skip when a task has no verifier artifacts",
    )


def _discover(config) -> list[tuple[TaskBundle, RunBundle]]:
    root = Path(config.getoption("--assay-delivery"))
    want_task = config.getoption("--assay-task")
    want_model = config.getoption("--assay-model")
    pairs: list[tuple[TaskBundle, RunBundle]] = []
    dataset = root / "dataset"
    if not dataset.is_dir():
        return pairs
    for task_dir in sorted(dataset.iterdir()):
        if not task_dir.is_dir():
            continue
        if want_task and task_dir.name != want_task:
            continue
        task = TaskBundle(task_dir)
        for run_dir in sorted(task.trajectories_dir.glob("*/run_*")):
            if want_model and run_dir.parent.name != want_model:
                continue
            pairs.append((task, RunBundle(run_dir)))
    return pairs


def pytest_generate_tests(metafunc):
    if "assayed" not in metafunc.fixturenames:
        return
    pairs = _discover(metafunc.config)
    require = metafunc.config.getoption("--assay-require-verifier")
    params, ids = [], []
    for task, run in pairs:
        if not task.has_process_verifier and not require:
            continue
        params.append((task, run))
        ids.append(f"{task.uuid[:8]}-{run.model}-{run.run_id}")
    if not params:
        pytest.skip("no runs with a process verifier under the selected corpus")
    metafunc.parametrize("assayed", params, ids=ids)


@pytest.fixture
def report(assayed):
    task, run = assayed
    truth = Truth.load(task.truth_path) if task.truth_path.is_file() else None
    return evaluate(task, run, truth)
