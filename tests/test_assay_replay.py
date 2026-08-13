"""THE milo-format acceptance test: replay the reference corpus through the
vendored assay and require byte-equivalent regeneration.

The corpus at milo-bench-samples/ was produced by a newer assay than the copy
we vendored; six drift patches were applied. This test is what proves them
right: for each covered bundle we rebuild a verdict store from the corpus's own
recorded verdicts.jsonl, run `assay score --write` on a copy, and diff every
process.json (modulo version.scorer — our scorer stamp legitimately differs),
final_score.md, verdicts.jsonl and result.json against the originals.

Covered cases: scored, voided (C1-no-upstream-content-fetched,
C3-no-graded-test-write) and unverifiable (B1-scored-status).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = Path("/Users/anzar/Desktop/ori/milo-bench-samples")

BUNDLES = {
    "016372a9-f7b9-4e69-919c-15c286423dc9": "scored (go-gorm, incl. 2-judge council runs)",
    "b7419275-8184-4485-9e8e-1404f64c1c38": "unverifiable via B1-scored-status",
    "16c78a6b-1674-4c30-9f56-445c9e34389a": "voided via C1-no-upstream-content-fetched",
    "3af7770f-59fd-4d93-ab3d-9ff8d9d5372b": "voided via C3-no-graded-test-write",
}

pytestmark = pytest.mark.skipif(
    not CORPUS.exists(), reason="reference corpus not present"
)


def _flat(d, p=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flat(v, f"{p}.{k}"))
    elif isinstance(d, list) and d and isinstance(d[0], dict):
        out[p + "[len]"] = len(d)
        for i, v in enumerate(d):
            out.update(_flat(v, f"{p}[{i}]"))
    else:
        out[p] = d
    return out


def _replay(uuid: str, tmp_path: Path) -> Path:
    delivery = tmp_path / "delivery"
    store = delivery / "verdicts" / uuid
    store.mkdir(parents=True)
    shutil.copytree(CORPUS / uuid, delivery / uuid)
    for v in (delivery / uuid).glob("trajectories/*/run_*/verifier/verdicts.jsonl"):
        model = v.parent.parent.parent.name
        run = v.parent.parent.name
        shutil.copy(v, store / f"{model}__{run}.jsonl")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "assay",
            "--delivery",
            str(delivery),
            "score",
            "--task",
            uuid,
            "--verdicts",
            str(delivery / "verdicts"),
            "--write",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert "REFUSE" not in proc.stdout, proc.stdout  # fingerprint gate must accept
    assert "wrote process artifacts" in proc.stdout, proc.stdout + proc.stderr
    return delivery / uuid


@pytest.mark.parametrize("uuid", sorted(BUNDLES), ids=lambda u: u[:8])
def test_corpus_replay_regenerates_identically(uuid: str, tmp_path: Path):
    replayed = _replay(uuid, tmp_path)
    corpus_runs = sorted((CORPUS / uuid / "trajectories").glob("*/run_*"))
    assert len(corpus_runs) == 9
    for run_dir in corpus_runs:
        rel = run_dir.relative_to(CORPUS / uuid)
        ours = replayed / rel

        old = _flat(json.loads((run_dir / "verifier" / "process.json").read_text()))
        new = _flat(json.loads((ours / "verifier" / "process.json").read_text()))
        diffs = [
            k
            for k in set(old) | set(new)
            if old.get(k) != new.get(k) and k != ".version.scorer"
        ]
        assert diffs == [], f"{rel}: {sorted(diffs)[:6]}"

        assert (run_dir / "verifier" / "final_score.md").read_text() == (
            ours / "verifier" / "final_score.md"
        ).read_text(), rel
        assert (run_dir / "verifier" / "verdicts.jsonl").read_bytes() == (
            ours / "verifier" / "verdicts.jsonl"
        ).read_bytes(), rel
        assert json.loads((run_dir / "result.json").read_text()) == json.loads(
            (ours / "result.json").read_text()
        ), rel


def test_bundle_fingerprint_matches_corpus_recordings():
    """The drift-patched fingerprint recipe must accept every corpus verdict."""
    sys.path.insert(0, str(REPO_ROOT))
    from assay.bundle import TaskBundle
    from assay.fingerprint import bundle_fingerprint

    for uuid in BUNDLES:
        task = TaskBundle(CORPUS / uuid)
        recorded = {
            json.loads(line)["bundle_fingerprint"]
            for v in (CORPUS / uuid).glob(
                "trajectories/*/run_*/verifier/verdicts.jsonl"
            )
            for line in v.read_text().splitlines()
        }
        assert recorded == {bundle_fingerprint(task)}, uuid
