"""Structure-diff: our generated bundles vs the reference corpus.

Compares a fully authored + judged pilot bundle (fixture) against a reference
milo-bench-samples bundle for structural equality: same file set, same JSON key
sets AND key order, same enums/templates. Values legitimately differ (different
task, model, scores); structure must not.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "milo_bundles"
CORPUS = Path("/Users/anzar/Desktop/ori/milo-bench-samples")

OURS = FIXTURES / "9a959823-e9ff-418e-a2aa-297a9a0735fa"  # tortoise 538
REF = CORPUS / "016372a9-f7b9-4e69-919c-15c286423dc9"

pytestmark = pytest.mark.skipif(
    not CORPUS.exists(), reason="reference corpus not present"
)

IGNORED = ("__pycache__", ".pyc", ".DS_Store")


def _fileset(root: Path) -> set[str]:
    out = set()
    for p in root.rglob("*"):
        if not p.is_file() or any(tok in str(p) for tok in IGNORED):
            continue
        parts = list(p.relative_to(root).parts)
        if parts[0] == "trajectories":
            parts[1], parts[2] = "<model>", "<run>"
            if parts[-1].endswith(".pane"):
                parts[-1] = "<name>.pane"
        out.add("/".join(parts))
    return out


def _first_run(root: Path) -> Path:
    return sorted(root.glob("trajectories/*/run_*"))[0]


def test_file_set_matches_corpus():
    assert _fileset(OURS) == _fileset(REF)


def test_rubrics_json_structure():
    ours = json.loads((OURS / "tests" / "rubrics.json").read_text())
    ref = json.loads((REF / "tests" / "rubrics.json").read_text())
    # Headerless delivery format: our bundles intentionally drop the corpus's
    # five header keys; item/checks/sites internals stay corpus-identical.
    assert list(ours) == ["items", "checks", "sites"]

    def item_keys(item):
        return tuple(item)

    # R-items carry mode:"judged"; G-items don't. Key order is fixed.
    for src in (ours, ref):
        for item in src["items"]:
            expected = [
                "id",
                "dimension",
                "weight",
                "evaluation_target",
                "criterion",
                "evaluation_rule",
                "evidence",
            ]
            if item["id"].startswith("R"):
                expected.append("mode")
            assert list(item) == expected, item["id"]
            if item["id"].startswith("R"):
                assert item["weight"] in (1, 3, 5), item["id"]

    # shared guardrails are byte-identical to the corpus
    ours_g = {i["id"]: i for i in ours["items"] if i["id"].startswith("G")}
    ref_g = {i["id"]: i for i in ref["items"] if i["id"].startswith("G")}
    assert set(ours_g) == set(ref_g) == {f"G{n}" for n in range(1, 8)}
    assert ours_g == ref_g

    # the 28 deterministic check descriptors are constant corpus-wide
    assert ours["checks"] == ref["checks"]


def test_truth_md_sections():
    section_re = re.compile(r"^## (.+)$", re.MULTILINE)
    ours = section_re.findall((OURS / "TRUTH.md").read_text())
    ref = section_re.findall((REF / "TRUTH.md").read_text())
    assert ours == ref


def test_process_json_key_paths():
    def keypaths(d, p=""):
        out = []
        if isinstance(d, dict):
            for k, v in d.items():
                out.append(f"{p}.{k}")
                out.extend(keypaths(v, f"{p}.{k}"))
        return out

    ours = json.loads((_first_run(OURS) / "verifier" / "process.json").read_text())
    # a scored corpus run, judged, so the same sections materialize
    ref_run = next(
        r
        for r in sorted(REF.glob("trajectories/*/run_*"))
        if json.loads((r / "verifier" / "process.json").read_text())["status"]
        == "scored"
    )
    ref = json.loads((ref_run / "verifier" / "process.json").read_text())
    skip = re.compile(
        r"\.detail\.deterministic\.checks\.|\.detail\.rubric\.items\.|"
        r"\.council\.members\.|\.rl\.|\.efficiency\."
    )
    o = {k for k in keypaths(ours) if not skip.search(k + ".")}
    r = {k for k in keypaths(ref) if not skip.search(k + ".")}
    assert {k.split(".")[1] for k in o if k.count(".") == 1} == {
        k.split(".")[1] for k in r if k.count(".") == 1
    }
    assert list(ours) == list(ref)  # top-level key order
    assert ours["status"] in ("scored", "voided", "unverifiable")


def test_verdicts_jsonl_key_order():
    for root in (OURS, REF):
        line = json.loads(
            (_first_run(root) / "verifier" / "verdicts.jsonl")
            .read_text()
            .splitlines()[0]
        )
        assert list(line) == [
            "member",
            "model",
            "item_id",
            "completion",
            "error",
            "bundle_fingerprint",
        ], root
        for tag in (
            "[[RATIONALE",
            "[[SATISFIED",
            "[[TRUNCATION_AFFECTED",
            "[[EVIDENCE",
        ):
            assert tag in line["completion"], (root, tag)


def test_final_score_md_template():
    def normalize(text: str) -> str:
        text = re.sub(r"-?\d+\.?\d*", "N", text)
        text = re.sub(r"(?m)^(Task:) .*$", r"\1 X", text)
        text = re.sub(r"(?m)^(# Score — ).*$", r"\1X", text)
        text = re.sub(r"(?m)^(Status:) \w+ *$", r"\1 X", text)
        text = re.sub(r"(?m)^(Gate:) .*$", r"\1 X", text)
        text = re.sub(r"(?m)^(Judges:) .*$", r"\1 X", text)
        # the process formula spells out the composition weights, which depend
        # on gate/kappa state: "(det + N·rubric) / N" when judged and open,
        # "N·det + N·rubric" when the rubric channel is zero-weighted
        text = re.sub(r"\*\*process\*\* = [^|]+", "**process** = X ", text)
        return text

    ours = normalize((_first_run(OURS) / "verifier" / "final_score.md").read_text())
    ref = normalize((_first_run(REF) / "verifier" / "final_score.md").read_text())
    assert ours == ref


def test_score_md_is_bare_full_repr_float():
    for root in (OURS, REF):
        run = _first_run(root)
        text = (run / "verifier" / "score.md").read_text()
        assert text.endswith("\n") and "\n" not in text[:-1], root
        score = json.loads((run / "result.json").read_text())["verifier_result"][
            "scores"
        ]["score"]
        assert text == repr(float(score)) + "\n", root


def test_result_json_writeback_structure():
    ours_vr = json.loads((_first_run(OURS) / "result.json").read_text())[
        "verifier_result"
    ]
    ref_vr = json.loads((_first_run(REF) / "result.json").read_text())[
        "verifier_result"
    ]
    assert set(ours_vr) == set(ref_vr)  # incl.: no retired WCB "rubric" block
    assert set(ours_vr["scores"]) == set(ref_vr["scores"])
    for vr, root in ((ours_vr, OURS), (ref_vr, REF)):
        for key in (
            "score",
            "score_binary",
            "score_outcome",
            "score_deterministic",
            "score_rubric",
            "score_process",
            "score_eval",
            "score_rl",
        ):
            assert key in vr["scores"], (root, key)
        assert list(vr["assay"]) == [
            "alpha",
            "kappa",
            "rubric_weight",
            "gate",
            "stratum_size",
            "council",
            "status",
        ], root
