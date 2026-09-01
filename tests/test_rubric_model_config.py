"""Pins for the dynamic rubric-model config (author_model / judge_model).

.llm_config/rubric-judge.json is the single source of truth for the rubric
chain's models: author_model (bare bridge id) writes narration + R-items,
judge_model (litellm id) drives the anchoring transport and the judge council.
Legacy configs carrying only "model" must keep working unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.multiswebench.scripts.rubric import assay_author
from benchmarks.multiswebench.scripts.rubric.judge_client import load_judge_config


def _cfg(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "base_url": "http://127.0.0.1:8765",
        "api_key": "stub",
    }
    data.update(overrides)
    p = tmp_path / "judge.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestJudgeModelResolution:
    def test_judge_model_wins_over_legacy_model(self, tmp_path: Path):
        p = _cfg(tmp_path, judge_model="anthropic/claude-sonnet-5", model="old-model")
        assert load_judge_config(p).model == "anthropic/claude-sonnet-5"

    def test_legacy_model_still_works(self, tmp_path: Path):
        p = _cfg(tmp_path, model="anthropic/claude-sonnet-5")
        assert load_judge_config(p).model == "anthropic/claude-sonnet-5"

    def test_missing_both_fields_raises(self, tmp_path: Path):
        p = _cfg(tmp_path)
        with pytest.raises(ValueError, match="judge_model"):
            load_judge_config(p)


class TestAuthorModelResolution:
    def test_reads_author_model(self, tmp_path: Path):
        p = _cfg(tmp_path, author_model="claude-opus-5")
        assert assay_author._resolve_author_model(p) == "claude-opus-5"

    def test_preserves_full_id(self, tmp_path: Path):
        # The prefix is what routes the call; it must survive resolution.
        p = _cfg(tmp_path, author_model="anthropic/claude-opus-5")
        assert assay_author._resolve_author_model(p) == "anthropic/claude-opus-5"

    def test_missing_field_falls_back_to_default(self, tmp_path: Path):
        p = _cfg(tmp_path, judge_model="anthropic/claude-sonnet-5")
        assert assay_author._resolve_author_model(p) == assay_author.AUTHOR_MODEL

    def test_missing_file_falls_back_to_default(self, tmp_path: Path):
        assert (
            assay_author._resolve_author_model(tmp_path / "absent.json")
            == assay_author.AUTHOR_MODEL
        )

    def test_invalid_json_falls_back_to_default(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert assay_author._resolve_author_model(p) == assay_author.AUTHOR_MODEL

    def test_committed_config_resolves_to_opus48(self):
        repo_root = Path(__file__).resolve().parent.parent
        p = repo_root / ".llm_config" / "rubric-judge.json"
        if not p.exists():
            pytest.skip(
                ".llm_config/rubric-judge.json is operator-supplied and "
                "deliberately untracked; nothing to validate on a clean clone"
            )
        assert assay_author._resolve_author_model(p) == "anthropic/claude-opus-4-8"


class TestAuthorRouting:
    def test_anthropic_prefix_routes_to_8765(self):
        bare, proxy = assay_author._route_author_model("anthropic/claude-opus-5")
        assert bare == "claude-opus-5"
        assert proxy == assay_author.DEFAULT_PROXY
        assert ":8765" in proxy

    def test_openai_prefix_routes_to_codex_8766(self):
        bare, proxy = assay_author._route_author_model("openai/gpt-5.6-sol")
        assert bare == "gpt-5.6-sol"
        assert proxy == assay_author.CODEX_PROXY
        assert proxy.endswith("/responses") and ":8766" in proxy

    def test_bare_id_defaults_to_anthropic(self):
        bare, proxy = assay_author._route_author_model("claude-opus-5")
        assert bare == "claude-opus-5"
        assert proxy == assay_author.DEFAULT_PROXY

    def test_author_proxy_env_overrides(self, monkeypatch):
        monkeypatch.setenv("ASSAY_AUTHOR_PROXY", "http://10.0.0.1:9999/responses")
        _, proxy = assay_author._route_author_model("openai/gpt-5.6-sol")
        assert proxy == "http://10.0.0.1:9999/responses"

    def test_bridge_call_routes_openai_model(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_call(proxy, model, system, user, cached, max_tokens):
            captured["proxy"] = proxy
            captured["model"] = model
            return "text", "", 0, 200, {}

        import assay.judge as assay_judge

        monkeypatch.setattr(assay_judge, "call", fake_call)
        monkeypatch.delenv("ASSAY_AUTHOR_PROXY", raising=False)
        out = assay_author._bridge_call("sys", "user", model="openai/gpt-5.6-sol")
        assert out == "text"
        assert captured["model"] == "gpt-5.6-sol"  # bare on the wire
        assert captured["proxy"].endswith("/responses")


class TestModelPlumbing:
    def test_bridge_call_forwards_model(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_call(proxy, model, system, user, cached, max_tokens):
            captured["model"] = model
            return "text", "", 0, 200, {}

        import assay.judge as assay_judge

        monkeypatch.setattr(assay_judge, "call", fake_call)
        out = assay_author._bridge_call("sys", "user", model="claude-opus-5")
        assert out == "text"
        assert captured["model"] == "claude-opus-5"

    def test_bridge_call_defaults_to_author_model(self, monkeypatch):
        captured: dict[str, str] = {}

        def fake_call(proxy, model, system, user, cached, max_tokens):
            captured["model"] = model
            return "text", "", 0, 200, {}

        import assay.judge as assay_judge

        monkeypatch.setattr(assay_judge, "call", fake_call)
        assay_author._bridge_call("sys", "user")
        assert captured["model"] == assay_author.AUTHOR_MODEL

    def test_author_flow_threads_resolved_model(self, tmp_path: Path, monkeypatch):
        """_author_from_skeleton resolves author_model from the config and
        hands it to both authoring stages (signature itself is pinned by
        test_argos_pilot_regression's rejected-authoring test)."""
        seen: list[tuple[str, str]] = []

        def fake_narrate(bundle, *, log, author_model):
            seen.append(("narrate", author_model))
            return True

        def fake_draft(bundle, *, log, author_model, feedback=""):
            seen.append(("draft", author_model))
            return False  # stop the flow right after the calls we care about

        monkeypatch.setattr(assay_author, "narrate_bundle", fake_narrate)
        monkeypatch.setattr(assay_author, "draft_items_bundle", fake_draft)
        cfg = _cfg(tmp_path, author_model="claude-opus-5")
        bundle = tmp_path / "0000-uuid"
        bundle.mkdir()
        ok = assay_author._author_from_skeleton(
            bundle, tmp_path, cfg, log=lambda _m: None
        )
        assert ok is False
        assert seen == [("narrate", "claude-opus-5"), ("draft", "claude-opus-5")]
