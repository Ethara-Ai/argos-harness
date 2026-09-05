"""Backfill ``artifacts/agent.patch`` into exported bundles from inference output.

The Harbor package never carried the agent's diff and neither did the flat
bundle, so bundles exported before the converter learned to ship it have
nothing a quality or scope verifier can read. The only durable copy is the
inference record itself: ``run_N/output.jsonl`` -> ``test_result.git_patch``.

This step copies exactly those bytes into
``<dest>/<uuid>/trajectories/<alias>/run_N/artifacts/agent.patch`` and rewrites
the artifacts manifest. It deliberately does NOT re-run the converter, which
rebuilds ``result.json`` from scratch and would clobber every assay-owned score
already published there.
"""

from __future__ import annotations

import sys
from pathlib import Path

from benchmarks.multiswebench.scripts.harbor.converter import (
    read_jsonl,
    write_agent_patch,
)
from benchmarks.multiswebench.scripts.rubric.export_bundle import model_alias


def patch_from_run_dir(run_dir: Path) -> str | None:
    """The recorded ``git_patch`` of a run, or None when the run has no record."""
    records = read_jsonl(Path(run_dir) / "output.jsonl")
    if not records:
        return None
    return str((records[0].get("test_result") or {}).get("git_patch") or "")


def attach_run_base(run_base: Path, bundle: Path, model_dir_name: str) -> int:
    """Attach every ``run_N`` under ``run_base`` to the bundle. Returns runs written."""
    alias = model_alias(model_dir_name)
    written = 0
    for run_dir in sorted(Path(run_base).glob("run_*")):
        patch = patch_from_run_dir(run_dir)
        if patch is None:
            print(f"attach-patch: {run_dir.name}: no output.jsonl record, skipped")
            continue
        traj_dir = Path(bundle) / "trajectories" / alias / run_dir.name
        if not traj_dir.is_dir():
            print(f"attach-patch: {run_dir.name}: no {traj_dir}, skipped")
            continue
        write_agent_patch(traj_dir, patch)
        state = "present" if patch else "empty"
        print(f"attach-patch: {alias}/{run_dir.name}: agent.patch {state}")
        written += 1
    return written


def main(run_base: Path, dest: Path, task: str, model: str | None = None) -> int:
    run_base = Path(run_base)
    bundle = Path(dest) / task
    if not bundle.is_dir():
        print(f"attach-patch: no bundle at {bundle}", file=sys.stderr)
        return 1
    model_dir_name = model or run_base.name
    if not attach_run_base(run_base, bundle, model_dir_name):
        print("attach-patch: nothing written", file=sys.stderr)
        return 2
    return 0
