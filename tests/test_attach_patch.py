"""attach-patch backfills agent.patch from inference output without touching
result.json or anything else in the bundle."""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.multiswebench.scripts.rubric.attach_patch import (
    attach_run_base,
    main,
    patch_from_run_dir,
)


PATCH = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-1\n+2\n"


def _inference_run(base: Path, n: int, patch: str | None) -> None:
    rd = base / f"run_{n}"
    rd.mkdir(parents=True)
    if patch is None:
        (rd / "output.jsonl").write_text("")
        return
    rec = {"instance_id": "x", "test_result": {"git_patch": patch, "uuid": "u"}}
    (rd / "output.jsonl").write_text(json.dumps(rec) + "\n")


def _bundle_run(bundle: Path, alias: str, n: int) -> Path:
    rd = bundle / "trajectories" / alias / f"run_{n}"
    (rd / "artifacts").mkdir(parents=True)
    (rd / "artifacts" / "manifest.json").write_text("[]")
    (rd / "result.json").write_text(json.dumps({"id": "keep", "verifier_result": {}}))
    return rd


def test_patch_from_run_dir(tmp_path: Path):
    _inference_run(tmp_path, 1, PATCH)
    _inference_run(tmp_path, 2, None)
    assert patch_from_run_dir(tmp_path / "run_1") == PATCH
    assert patch_from_run_dir(tmp_path / "run_2") is None
    assert patch_from_run_dir(tmp_path / "run_9") is None


def test_attach_writes_only_patch_and_manifest(tmp_path: Path):
    run_base = tmp_path / "eval" / "claude-opus-5"
    _inference_run(run_base, 1, PATCH)
    _inference_run(run_base, 2, "")
    bundle = tmp_path / "bundles" / "1234"
    r1 = _bundle_run(bundle, "opus-5", 1)
    r2 = _bundle_run(bundle, "opus-5", 2)
    before = (r1 / "result.json").read_bytes()

    assert attach_run_base(run_base, bundle, "claude-opus-5") == 2
    assert (r1 / "artifacts" / "agent.patch").read_text() == PATCH
    assert not (r2 / "artifacts" / "agent.patch").exists()
    assert (
        json.loads((r1 / "artifacts" / "manifest.json").read_text())[0]["status"]
        == "present"
    )
    assert (
        json.loads((r2 / "artifacts" / "manifest.json").read_text())[0]["status"]
        == "empty"
    )
    assert (r1 / "result.json").read_bytes() == before


def test_main_exit_codes(tmp_path: Path):
    run_base = tmp_path / "eval" / "claude-opus-5"
    _inference_run(run_base, 1, PATCH)
    dest = tmp_path / "bundles"
    assert main(run_base, dest, "missing") == 1
    _bundle_run(dest / "1234", "opus-5", 3)  # run_3 exists, run_1 does not
    assert main(run_base, dest, "1234") == 2
    _bundle_run(dest / "1234", "opus-5", 1)
    assert main(run_base, dest, "1234") == 0
