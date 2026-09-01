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
CORPUS = Path("/Users/anzar/Desktop/ori/argos-samples")

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
            "http://127.0.0.1:9999/v1/chat/completions",  # openai chat shape
            "http://127.0.0.1:8766/responses",  # codex responses shape
        ):
            for cached in ("", "evidence packet"):
                body, _headers = build_request(
                    proxy, "claude-sonnet-5", "system prompt", "question", cached
                )
                assert "temperature" not in body, proxy

    def test_p8_responses_endpoint_detection(self):
        from assay.judge import is_responses_endpoint

        assert is_responses_endpoint("http://127.0.0.1:8766/responses")
        assert is_responses_endpoint("http://127.0.0.1:8766/v1/responses")
        assert not is_responses_endpoint("http://127.0.0.1:8765/v1/messages")
        assert not is_responses_endpoint("http://127.0.0.1:9999/v1/chat/completions")

    def test_p8_responses_branch_body_shape(self):
        from assay.judge import build_request

        body, headers = build_request(
            "http://127.0.0.1:8766/responses",
            "gpt-5.6-sol",
            "system prompt",
            "the question",
            "evidence packet",
            max_tokens=8000,
        )
        assert body["model"] == "gpt-5.6-sol"
        assert body["instructions"] == "system prompt"
        assert body["store"] is False  # backend requires; bridge also forces
        # typed user message so the bridge's fold matcher accepts it
        item = body["input"][0]
        assert item["type"] == "message" and item["role"] == "user"
        assert item["content"][0]["type"] == "input_text"
        assert "evidence packet" in item["content"][0]["text"]
        assert "the question" in item["content"][0]["text"]
        # bridge strips Authorization + injects the real token
        assert headers["Authorization"].startswith("Bearer ")

    def test_p8_extract_text_handles_responses_output(self):
        from assay.judge import extract_text

        doc = {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "hello "},
                        {"type": "output_text", "text": "world"},
                    ],
                },
            ]
        }
        assert extract_text(doc) == "hello world"

    def test_p8_cli_proxy_default_is_anthropic_bridge(self):
        # :8766 now hosts the Codex bridge; a bare judge must not default there.
        src = (ASSAY / "cli.py").read_text()
        assert 'ASSAY_PROXY", "http://127.0.0.1:8765/v1/messages"' in src
        assert "8766/v1/messages" not in src


class TestTruthUnderSolution:
    """TRUTH.md ships at solution/TRUTH.md; root copies stay readable.

    The harbor delivery format and the trinity leak gate both name
    solution/TRUTH.md, so writers must land there while every bundle already on
    disk keeps grading from the root.
    """

    def _bundle(self, root: Path, truth_rel: str | None) -> Path:
        (root / "solution").mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir(parents=True, exist_ok=True)
        if truth_rel is not None:
            path = root / truth_rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# TRUTH.md - x\n\n## Defect\n\nprose\n", encoding="utf-8")
        return root

    def test_writer_target_is_under_solution(self, tmp_path: Path):
        from assay.bundle import TaskBundle

        task = TaskBundle(self._bundle(tmp_path / "b", None))
        assert task.truth_path == task.root / "solution" / "TRUTH.md"

    def test_reader_prefers_solution_copy(self, tmp_path: Path):
        from assay.bundle import TaskBundle

        root = self._bundle(tmp_path / "b", "solution/TRUTH.md")
        (root / "TRUTH.md").write_text("stale root copy\n", encoding="utf-8")
        task = TaskBundle(root)
        assert task.truth_path == root / "solution" / "TRUTH.md"
        assert "stale" not in task.truth_path.read_text()

    def test_reader_falls_back_to_legacy_root(self, tmp_path: Path):
        from assay.bundle import TaskBundle

        root = self._bundle(tmp_path / "b", "TRUTH.md")
        task = TaskBundle(root)
        assert task.truth_path == root / "TRUTH.md"
        assert task.truth_path.is_file()

    def test_fixture_bundles_still_resolve(self):
        from assay.bundle import TaskBundle

        fixtures = REPO_ROOT / "tests" / "fixtures" / "argos_bundles"
        bundles = (
            [p for p in sorted(fixtures.glob("*-*")) if (p / "tests").is_dir()]
            if fixtures.is_dir()
            else []
        )
        if not bundles:
            pytest.skip(
                "tests/fixtures/argos_bundles is not carried in this harness; "
                "no pre-move bundles on disk to check back-compat against"
            )
        for root in bundles:
            assert TaskBundle(root).truth_path.is_file(), root

    def test_site_table_resolves_from_either_location(self, tmp_path: Path):
        # _find_spec walks up from TRUTH.md to reach tests/rubrics.json, and a
        # miss there is silent: every site disappears and nothing scores.
        from assay.truth import _find_spec

        spec = {"sites": [{"path": "a.py", "probes": ["x"]}]}
        for rel in ("TRUTH.md", "solution/TRUTH.md"):
            root = self._bundle(tmp_path / rel.replace("/", "_"), rel)
            (root / "tests" / "rubrics.json").write_text(json.dumps(spec))
            assert _find_spec(root / rel) == root / "tests" / "rubrics.json", rel
