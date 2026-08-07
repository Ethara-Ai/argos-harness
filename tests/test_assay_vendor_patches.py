"""Shields for the vendored-assay drift patches — each patch from the plan is
pinned so a future re-vendor or upstream sync cannot silently revert it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ASSAY = REPO_ROOT / "assay"
CORPUS = Path("/Users/anzar/Desktop/ori/milo-bench-samples")

sys.path.insert(0, str(REPO_ROOT))


class TestDriftPatches:
    def test_p1_item_key_is_evaluation_rule(self):
        from assay.fingerprint import _ITEM_FIELDS

        assert "evaluation_rule" in _ITEM_FIELDS
        assert "judgment" not in _ITEM_FIELDS
        # R010's required-field tuple names the corpus key (whitespace-neutral:
        # the formatter may re-wrap the tuple)
        lint_src = re.sub(r"\s+", " ", (ASSAY / "lint.py").read_text())
        assert '"evaluation_rule", "evidence"' in lint_src

    def test_p2_emitted_test_filename(self):
        from assay.bundle import TaskBundle

        task = TaskBundle(Path("/nonexistent"))
        assert task.process_test_path.name == "test_output.py"

    def test_p3_preamble_is_g1_to_g6_and_author_appends_g7(self):
        doc = json.loads((ASSAY / "preamble.json").read_text())
        assert [i["id"] for i in doc["items"]] == ["G1", "G2", "G3", "G4", "G5", "G6"]
        assert all("evaluation_rule" in i for i in doc["items"])
        author_src = re.sub(r"\s+", " ", (ASSAY / "author.py").read_text())
        assert '"id": "G7", "dimension": "scope_discipline", "weight": -3' in author_src

    def test_p5_delivery_is_env_overridable(self):
        for path in (ASSAY / "cli.py", ASSAY / "checks" / "conftest.py"):
            assert "ASSAY_DELIVERY" in path.read_text(), path

    def test_p6_prune_is_opt_in(self):
        src = (ASSAY / "cli.py").read_text()
        assert 'getattr(args, "prune", False)' in src
        assert '"--prune"' in src

    def test_p6b_scoring_never_deletes_other_runs(self, tmp_path: Path):
        if not CORPUS.exists():
            pytest.skip("reference corpus not present")
        uuid = "016372a9-f7b9-4e69-919c-15c286423dc9"
        delivery = tmp_path / "delivery"
        store = delivery / "verdicts" / uuid
        store.mkdir(parents=True)
        shutil.copytree(CORPUS / uuid, delivery / uuid)
        for v in (delivery / uuid).glob("trajectories/*/run_*/verifier/verdicts.jsonl"):
            shutil.copy(
                v,
                store / f"{v.parent.parent.parent.name}__{v.parent.parent.name}.jsonl",
            )
        other = delivery / uuid / "trajectories" / "opus-4.8" / "run_2" / "verifier"
        before = {p.name: p.read_bytes() for p in other.iterdir()}
        subprocess.run(
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
                "--models",
                "gpt-5.6-sol",
                "--write",
            ],
            capture_output=True,
            cwd=REPO_ROOT,
            check=True,
        )
        # narrowing --models must NOT delete the uncovered runs' artifacts
        after = {p.name: p.read_bytes() for p in other.iterdir()}
        assert after == before

    def test_p8_target_test_file_guard(self):
        src = (ASSAY / "deterministic.py").read_text()
        assert "if not test_patch.is_file():" in src

    def test_test_stdout_path_is_md(self):
        src = (ASSAY / "bundle.py").read_text()
        assert "test-stdout.md" in src
        assert "test-stdout.txt" not in src

    def test_judge_sections_are_corpus_narration_names(self):
        from assay.truth import JUDGE_SECTIONS

        assert JUDGE_SECTIONS == (
            "Defect",
            "Root cause",
            "Solution shape",
            "Ordered plan",
            "Pitfalls",
        )

    def test_fingerprint_prose_sections_stay_generator_compatible(self):
        # The corpus generator hashed the OLD names (three digest as "").
        # Changing this invalidates every recorded corpus verdict.
        from assay.fingerprint import _PROSE_SECTIONS

        assert _PROSE_SECTIONS == (
            "Defect",
            "Root cause",
            "Pseudocode",
            "Optimal step sequence",
            "Anti-patterns",
        )

    def test_scorer_stamp_hashes_real_files_from_new_location(self):
        import hashlib

        from assay.fingerprint import scorer_stamp

        empty = hashlib.sha256(b"").hexdigest()[:16]
        assert scorer_stamp() != empty  # would mean the literal paths broke

    def test_packages_config_includes_assay(self):
        src = (REPO_ROOT / "pyproject.toml").read_text()
        assert '"benchmarks", "assay"' in src
        assert "PyYAML" in src

    def test_judge_requests_never_send_temperature(self):
        # Claude 5 models reject the temperature parameter outright; the plan
        # pins that no judge request shape carries it.
        from assay.judge import build_request

        for proxy in (
            "http://127.0.0.1:8765/v1/messages",  # anthropic/bridge shape
            "http://127.0.0.1:9999/v1/chat/completions",  # openai shape
        ):
            for cached in ("", "evidence packet"):
                body, _headers = build_request(
                    proxy, "claude-sonnet-5", "system prompt", "question", cached
                )
                assert "temperature" not in body, proxy
