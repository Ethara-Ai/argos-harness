"""THE milo-format acceptance test: replay the reference corpus through the
vendored assay and require byte-equivalent regeneration.

The corpus at milo-bench-samples/ was produced by a newer assay than the copy
we vendored; six drift patches were applied. This test is what proves them
right: for each covered bundle we rebuild a verdict store from the corpus's own
recorded verdicts.jsonl, run `assay score --write` on a copy, and diff every
process.json (modulo version.scorer — our scorer stamp legitimately differs),
final_score.md, verdicts.jsonl and result.json against the originals.

The weight-ladder change (2026-08-13) moved the det check weights off the
corpus's 8/2/6 onto the {1,3,5} ladder, so every det-DERIVED number diverges by
design; those paths are exempted below and replaced by the ladder invariant.
Check ids, gates, verdicts, details and the whole rubric/outcome side stay
byte-pinned to the corpus.

Covered cases: scored, voided (C1-no-upstream-content-fetched,
C3-no-graded-test-write) and unverifiable (B1-scored-status).
"""

from __future__ import annotations

import json
import re
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

# Values the weight-ladder change legitimately moves (all downstream of the det
# soft score). Everything outside these paths must replay byte-identically.
CHANGED = re.compile(
    r"^\.version\.scorer$|"
    r"^\.process\.(deterministic|score)$|"
    r"^\.composition\.(process|process_stratified|score_eval|score_rl)$|"
    r"^\.rl\.score_rl$|"
    r"^\.detail\.deterministic\.soft_score$|"
    r"^\.detail\.deterministic\.checks\[\d+\]\.weight$"
)

# result.json score fields downstream of the det channel; the harness-owned
# score/score_binary/score_continuous_v2 and the judge-owned score_rubric,
# score_outcome must still match the corpus exactly.
DET_DERIVED_SCORES = ("score_deterministic", "score_process", "score_eval", "score_rl")


def _mask_det_rows(md: str) -> str:
    """Blank the four det-derived values in final_score.md, nothing else."""
    md = re.sub(r"(?m)^(\| deterministic \(soft\) \|).*$", r"\1 X |", md)
    md = re.sub(r"(?m)^(\| \*\*process\*\*[^|]*\|).*$", r"\1 X |", md)
    md = re.sub(r"(?m)^(\| \*\*score_eval\*\*[^|]*\|).*$", r"\1 X |", md)
    md = re.sub(r"(?m)^(\| \*\*score_rl\*\*[^|]*\|).*$", r"\1 X |", md)
    return md


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

        new_doc = json.loads((ours / "verifier" / "process.json").read_text())
        old = _flat(json.loads((run_dir / "verifier" / "process.json").read_text()))
        new = _flat(new_doc)
        diffs = [
            k
            for k in set(old) | set(new)
            if old.get(k) != new.get(k) and not CHANGED.match(k)
        ]
        assert diffs == [], f"{rel}: {sorted(diffs)[:6]}"
        # the ladder invariant replaces the exact-weight pin lifted above
        emitted = {c["weight"] for c in new_doc["detail"]["deterministic"]["checks"]}
        assert emitted <= {0, 1, 3, 5}, f"{rel}: off-ladder weights {emitted}"

        assert _mask_det_rows(
            (run_dir / "verifier" / "final_score.md").read_text()
        ) == _mask_det_rows((ours / "verifier" / "final_score.md").read_text()), rel
        assert (run_dir / "verifier" / "verdicts.jsonl").read_bytes() == (
            ours / "verifier" / "verdicts.jsonl"
        ).read_bytes(), rel
        old_r = json.loads((run_dir / "result.json").read_text())
        new_r = json.loads((ours / "result.json").read_text())
        for doc in (old_r, new_r):
            for key in DET_DERIVED_SCORES:
                doc["verifier_result"]["scores"].pop(key, None)
        assert old_r == new_r, rel


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
