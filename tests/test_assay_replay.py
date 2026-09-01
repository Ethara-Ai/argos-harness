"""THE argos-format acceptance test: replay the reference corpus through the
vendored assay.

The corpus at argos-samples/ was produced by a newer assay than the copy
we vendored; six drift patches were applied. Originally this test required
byte-identical regeneration of every artifact. The single-judge reward change
("remove kappa, keep alpha") deliberately diverges from the corpus format:
the council block became judge{model}, the process score is the plain average
(det + rubric) / 2 instead of the pooled-kappa-weighted blend, and the composed
scores moved accordingly. So the byte-identity claim is now scoped to the channels the
change did not touch — outcome, the deterministic checks, and the rubric
tally — while the composed fields are required to equal a recomputation under
the new formula from those byte-identical inputs.

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
CORPUS = Path("/Users/anzar/Desktop/ori/argos-samples")

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


# Paths whose values the single-judge change moved (or removed) on purpose,
# UNIONED with the paths the weight-ladder change moves (everything downstream
# of the det soft score). Everything OUTSIDE these subtrees must replay
# byte-identically against the corpus.
CHANGED = re.compile(
    r"^\.version\.scorer$|"
    r"^\.judge\b|^\.council\b|"
    r"^\.process\.(deterministic|score|weights|contested_items)\b|"
    r"^\.composition\b|^\.rl\b|"
    r"^\.detail\.deterministic\.soft_score$|"
    r"^\.detail\.deterministic\.checks\[\d+\]\.weight$|"
    r"^\.detail\.rubric\.(judge_members|contested|dissent_filtered)\b|"
    r"^\.detail\.rubric\.items\[\d+\]\.(resolution|abstained)$"
)


def _unrounded_channels(new: dict) -> tuple[float, float | None]:
    """Rebuild det/rubric at full float precision from the detail block.

    process.json stores the channels rounded to 4 dp, but the scorer composes
    and stratifies the unrounded values; recomputing from the per-check and
    per-item records is the only way to match it exactly.
    """
    from assay.rubric import FLOOR_BAND, dimension_weights

    checks = new["detail"]["deterministic"]["checks"]
    soft = [c for c in checks if c["gate"] == "soft" and c["verdict"] != "abstain"]
    denom = sum(c["weight"] for c in soft)
    det = (
        sum(c["weight"] for c in soft if c["verdict"] == "pass") / denom
        if denom
        else 0.0
    )
    rub_detail = new["detail"]["rubric"]
    if rub_detail is None:
        return det, None
    items = rub_detail["items"]
    eff = dimension_weights(
        [
            {"id": i["id"], "dimension": i["dimension"], "weight": i["weight"]}
            for i in items
        ]
    )
    scored = [i for i in items if not i["abstained"]]
    rden = sum(eff[i["id"]] for i in scored if eff[i["id"]] > 0)
    if rden == 0:
        return det, 0.0
    raw = sum(eff[i["id"]] for i in scored if i["satisfied"]) / rden
    rub = (
        FLOOR_BAND + (1.0 - FLOOR_BAND) * min(1.0, raw)
        if raw >= 0.0
        else FLOOR_BAND / (1.0 - raw)
    )
    return det, rub


@pytest.mark.parametrize("uuid", sorted(BUNDLES), ids=lambda u: u[:8])
def test_corpus_replay_channels_identical_scores_recomposed(uuid: str, tmp_path: Path):
    sys.path.insert(0, str(REPO_ROOT))
    from assay.compose import combine, process_score, stratify

    replayed = _replay(uuid, tmp_path)
    corpus_runs = sorted((CORPUS / uuid / "trajectories").glob("*/run_*"))
    assert len(corpus_runs) == 9

    news = {
        r: json.loads(
            (
                replayed / r.relative_to(CORPUS / uuid) / "verifier" / "process.json"
            ).read_text()
        )
        for r in corpus_runs
    }
    exact = {
        r: process_score(det=det, rubric=rub)
        for r, (det, rub) in ((r, _unrounded_channels(n)) for r, n in news.items())
    }

    for run_dir in corpus_runs:
        rel = run_dir.relative_to(CORPUS / uuid)
        ours = replayed / rel
        old = json.loads((run_dir / "verifier" / "process.json").read_text())
        new = news[run_dir]

        # 1. Unchanged channels replay byte-identically.
        oldf, newf = _flat(old), _flat(new)
        diffs = [
            k
            for k in set(oldf) | set(newf)
            if oldf.get(k) != newf.get(k) and not CHANGED.search(k)
        ]
        assert diffs == [], f"{rel}: {sorted(diffs)[:6]}"
        # the ladder invariant replaces the exact-weight pin lifted above
        emitted = {c["weight"] for c in new["detail"]["deterministic"]["checks"]}
        assert emitted <= {0, 1, 3, 5}, f"{rel}: off-ladder weights {emitted}"

        # 2. The composed fields equal a recomputation under the new formula
        #    from those byte-identical channel inputs. A run with no outcome
        #    score never enters the composer (unchanged behavior) — its
        #    process score reports 0.0 via the report fallback.
        comp, proc = new["composition"], new["process"]
        rubric = proc["rubric"]
        if not comp:
            assert old["composition"] == {}, rel
            assert proc["score"] == 0.0, rel
            _check_schema_and_writeback(run_dir, ours, old, new, rel)
            continue
        det_exact, rub_exact = _unrounded_channels(new)
        assert proc["deterministic"] == round(det_exact, 4), rel
        assert rubric == (round(rub_exact, 4) if rub_exact is not None else None), rel
        assert "rubric_weight" not in comp, rel  # the weight dial is gone entirely
        assert comp["alpha"] == old["composition"]["alpha"]  # alpha untouched
        expected_process = exact[run_dir]
        assert proc["score"] == round(expected_process, 4), rel
        assert comp["process"] == round(expected_process, 4), rel
        alpha, gate = comp["alpha"], proc["gate"]
        outcome = new["outcome"]["score"] or 0.0
        assert comp["score_eval"] == round(
            combine(outcome=outcome, p=expected_process, alpha=alpha, gate=gate), 4
        ), rel
        stratum = [
            exact[r]
            for r, n in news.items()
            if n.get("composition")
            and abs((n["outcome"]["score"] or 0.0) - outcome) < 1e-9
        ]
        assert comp["stratum_size"] == len(stratum), rel
        strat = stratify(expected_process, stratum)
        assert comp["process_stratified"] == round(strat, 4), rel
        assert comp["score_rl"] == round(
            combine(outcome=outcome, p=strat, alpha=alpha, gate=gate), 4
        ), rel

        _check_schema_and_writeback(run_dir, ours, old, new, rel)


def _check_schema_and_writeback(run_dir: Path, ours: Path, old, new, rel) -> None:
    # 3. New single-judge schema; the corpus roster carries over.
    assert list(new["judge"]) == ["model"]
    assert new["judge"]["model"] == "+".join(old["council"]["members"]), rel
    assert "kappa" not in json.dumps(new), rel

    # 4. Verdicts pass through untouched; result.json keeps every
    #    harness-owned field and gains the new assay block.
    assert (run_dir / "verifier" / "verdicts.jsonl").read_bytes() == (
        ours / "verifier" / "verdicts.jsonl"
    ).read_bytes(), rel
    old_vr = json.loads((run_dir / "result.json").read_text())["verifier_result"]
    new_vr = json.loads((ours / "result.json").read_text())["verifier_result"]
    for key in ("score", "score_binary", "score_continuous_v2"):
        assert new_vr["scores"].get(key) == old_vr["scores"].get(key), (rel, key)
    # score_deterministic is NOT corpus-pinned: the weight ladder moved it.
    for key in ("score_outcome", "score_rubric"):
        if key in old_vr["scores"]:
            assert new_vr["scores"][key] == old_vr["scores"][key], (rel, key)
    assert list(new_vr["assay"]) == [
        "alpha",
        "gate",
        "stratum_size",
        "judge",
        "status",
    ], rel

    # 5. final_score.md is regenerated in the new template.
    md = (ours / "verifier" / "final_score.md").read_text()
    assert "κ" not in md and "Judges:" not in md, rel
    assert "Judge: " in md, rel


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
