"""The converter ships the agent's final diff with every run.

Delivered bundles carried an empty ``artifacts/`` and no ``git_patch`` field
anywhere (assay/bundle.py), so nothing downstream could read what the agent
actually changed. ``write_agent_patch`` lands the inference record's exact
patch bytes at ``artifacts/agent.patch`` and says so in the manifest; an empty
patch writes no file and the manifest keeps the historical ``empty`` shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmarks.multiswebench.scripts.harbor.converter import (
    AGENT_PATCH_NAME,
    write_agent_patch,
)


PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def _manifest(traj_dir: Path) -> list[dict]:
    return json.loads((traj_dir / "artifacts" / "manifest.json").read_text())


def test_patch_written_verbatim_and_declared(tmp_path: Path):
    manifest = write_agent_patch(tmp_path, PATCH)
    assert (tmp_path / "artifacts" / AGENT_PATCH_NAME).read_bytes() == PATCH.encode()
    assert manifest == _manifest(tmp_path)
    assert manifest[0]["status"] == "present"
    assert manifest[0]["files"] == [AGENT_PATCH_NAME]
    assert manifest[0]["destination"] == "artifacts"


def test_empty_patch_writes_no_file_and_keeps_legacy_manifest(tmp_path: Path):
    manifest = write_agent_patch(tmp_path, "")
    assert not (tmp_path / "artifacts" / AGENT_PATCH_NAME).exists()
    assert manifest == [
        {
            "source": "/logs/artifacts",
            "destination": "artifacts",
            "type": "directory",
            "status": "empty",
        }
    ]
    assert manifest == _manifest(tmp_path)


def test_rewrite_with_empty_patch_removes_stale_file(tmp_path: Path):
    write_agent_patch(tmp_path, PATCH)
    write_agent_patch(tmp_path, "")
    assert not (tmp_path / "artifacts" / AGENT_PATCH_NAME).exists()
    assert _manifest(tmp_path)[0]["status"] == "empty"


def test_non_ascii_patch_round_trips(tmp_path: Path):
    text = PATCH + "+# ünïcode ✓\n"
    write_agent_patch(tmp_path, text)
    assert (tmp_path / "artifacts" / AGENT_PATCH_NAME).read_text(
        encoding="utf-8"
    ) == text
