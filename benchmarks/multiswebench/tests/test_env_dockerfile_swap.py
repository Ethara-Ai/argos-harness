"""Tests for the env-Dockerfile swap (TL directive 2026-08-11).

The bundle's environment/Dockerfile ships the input task-folder Dockerfile
verbatim when one is committed under scripts/harbor/env_dockerfiles/; the
task-template render remains the live fallback. Full rationale + revert:
scripts/harbor/env_dockerfiles/DOCKERFILE_SWAP.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmarks.multiswebench.scripts.harbor import converter


ENV_DIR = Path(__file__).resolve().parents[1] / "scripts" / "harbor" / "env_dockerfiles"


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ENV_DOCKERFILE_SOURCE", raising=False)


# ── resolve_env_dockerfile unit cases (isolated tmp dir) ─────────────────────


def _iso(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(converter, "ENV_DOCKERFILES_DIR", tmp_path)
    return tmp_path


def test_no_repo_dir_returns_none(monkeypatch, tmp_path):
    _iso(monkeypatch, tmp_path)
    assert converter.resolve_env_dockerfile("acme", "widget", 1) is None


def test_single_file_used_for_every_pr(monkeypatch, tmp_path):
    root = _iso(monkeypatch, tmp_path)
    d = root / "acme_m_widget"
    d.mkdir()
    (d / "Dockerfile.base").write_text("FROM scratch\n")
    for pr in (1, 999999):
        resolved = converter.resolve_env_dockerfile("Acme", "Widget", pr)
        assert resolved == d / "Dockerfile.base"


def test_multi_file_uses_map(monkeypatch, tmp_path):
    root = _iso(monkeypatch, tmp_path)
    d = root / "acme_m_widget"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM golang:1.17\n")
    (d / "Dockerfile1").write_text("FROM golang:1.20\n")
    (d / "map.json").write_text(json.dumps({"10": "Dockerfile", "20": "Dockerfile1"}))
    assert converter.resolve_env_dockerfile("acme", "widget", 10) == d / "Dockerfile"
    assert converter.resolve_env_dockerfile("acme", "widget", 20) == d / "Dockerfile1"


def test_multi_file_without_map_falls_back(monkeypatch, tmp_path, capsys):
    root = _iso(monkeypatch, tmp_path)
    d = root / "acme_m_widget"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM golang:1.17\n")
    (d / "Dockerfile1").write_text("FROM golang:1.20\n")
    assert converter.resolve_env_dockerfile("acme", "widget", 10) is None
    assert "WARN" in capsys.readouterr().out


def test_map_missing_key_falls_back(monkeypatch, tmp_path, capsys):
    root = _iso(monkeypatch, tmp_path)
    d = root / "acme_m_widget"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM golang:1.17\n")
    (d / "Dockerfile1").write_text("FROM golang:1.20\n")
    (d / "map.json").write_text(json.dumps({"10": "Dockerfile"}))
    assert converter.resolve_env_dockerfile("acme", "widget", 99) is None
    assert "WARN" in capsys.readouterr().out


def test_map_dangling_filename_falls_back(monkeypatch, tmp_path, capsys):
    root = _iso(monkeypatch, tmp_path)
    d = root / "acme_m_widget"
    d.mkdir()
    (d / "Dockerfile").write_text("FROM golang:1.17\n")
    (d / "Dockerfile1").write_text("FROM golang:1.20\n")
    (d / "map.json").write_text(json.dumps({"10": "DockerfileX"}))
    assert converter.resolve_env_dockerfile("acme", "widget", 10) is None
    assert "WARN" in capsys.readouterr().out


def test_template_override_forces_fallback(monkeypatch, tmp_path):
    root = _iso(monkeypatch, tmp_path)
    d = root / "acme_m_widget"
    d.mkdir()
    (d / "Dockerfile.base").write_text("FROM scratch\n")
    monkeypatch.setenv("ENV_DOCKERFILE_SOURCE", "template")
    assert converter.resolve_env_dockerfile("acme", "widget", 1) is None


# ── build_task-level: verbatim copy vs template fallback ─────────────────────


def _record(org: str, repo: str, number: int, lang: str) -> dict:
    return {
        "org": org,
        "repo": repo,
        "number": number,
        "lang": lang,
        "base": {"sha": "deadbeef"},
        "title": "t",
        "body": "b",
        "resolved_issues": [],
        "fix_patch": "--- a\n+++ b\n",
        "test_patch": "--- a\n+++ b\n",
        "number_interval": "n/a",
        "uuid": "u",
    }


def _run_build_task(tmp_path: Path, record: dict) -> str:
    freya = tmp_path / "freya"
    freya.mkdir(exist_ok=True)
    out = tmp_path / "out"
    converter.build_task(
        instance_id=f"{record['org']}__{record['repo']}-{record['number']}",
        record=record,
        freya_pr_dir=freya,
        out_dir=out,
        ecr_prefix="example.com/prefix",
        task_uuid="00000000-0000-0000-0000-000000000000",
    )
    return (out / "task" / "environment" / "Dockerfile").read_text(encoding="utf-8")


def test_build_task_ships_input_dockerfile_verbatim(monkeypatch, tmp_path):
    root = _iso(monkeypatch, tmp_path / "env")
    d = root / "acme_m_widget"
    d.mkdir(parents=True)
    # Contains a template placeholder and the language-patch anchor: neither
    # must be touched (verbatim copy — no render, no language patches).
    payload = (
        "FROM golang:1.26\n"
        "# marker for language-specific fixes\n"
        "\n"
        "RUN apt-get update && echo {repo_name}\n"
    )
    (d / "Dockerfile.base").write_text(payload)
    # java would trigger inject_dockerfile_language_patches on the template
    # path; on the input path it must NOT run (payload stays byte-identical).
    text = _run_build_task(tmp_path, _record("acme", "widget", 7, "java"))
    assert text == payload


def test_build_task_falls_back_to_template_render(monkeypatch, tmp_path, capsys):
    _iso(monkeypatch, tmp_path / "env")  # empty env dir -> no input file
    text = _run_build_task(tmp_path, _record("acme", "widget", 7, "go"))
    expected = converter.render_literal(
        (converter.TEMPLATE_DIR / "environment" / "Dockerfile").read_text(
            encoding="utf-8"
        ),
        base_image="example.com/prefix/acme_m_widget:pr-7",
        repo_name="widget",
    )
    expected = converter.inject_dockerfile_language_patches(expected, "go")
    assert text == expected
    assert "falling back to task-template" in capsys.readouterr().out


# ── committed-asset shields (real env_dockerfiles/ content) ──────────────────


def test_dapr_asset_committed():
    f = ENV_DIR / "dapr_m_dapr" / "Dockerfile.base"
    assert f.is_file() and f.stat().st_size > 0
    assert "FROM golang:" in f.read_text(encoding="utf-8")


def test_xtls_assets_and_map_committed():
    d = ENV_DIR / "xtls_m_xray-core"
    files = {p.name for p in d.iterdir() if p.name.startswith("Dockerfile")}
    assert files == {"Dockerfile", "Dockerfile1", "Dockerfile2", "Dockerfile3"}
    mapping = json.loads((d / "map.json").read_text(encoding="utf-8"))
    assert len(mapping) == 38
    assert all(k.isdigit() for k in mapping)
    assert set(mapping.values()) <= files


def test_xtls_dockerfiles_have_distinct_go_versions():
    d = ENV_DIR / "xtls_m_xray-core"
    versions = {}
    for name in ("Dockerfile", "Dockerfile1", "Dockerfile2", "Dockerfile3"):
        m = re.search(
            r"^FROM\s+golang:(\d+\.\d+)",
            (d / name).read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert m, f"{name} lacks FROM golang:X.Y"
        versions[name] = m.group(1)
    assert len(set(versions.values())) == 4


def test_real_resolution_dapr_and_xtls():
    p = converter.resolve_env_dockerfile("dapr", "dapr", 1351)
    assert p is not None and p.name == "Dockerfile.base"
    # pr-4584 is the era outlier (go 1.26 amid 1.24 neighbors) — pinned from
    # the ECR-derived ground truth so a regressed map gets caught.
    p = converter.resolve_env_dockerfile("XTLS", "Xray-core", 4584)
    assert p is not None and p.name == "Dockerfile3"
    p = converter.resolve_env_dockerfile("XTLS", "Xray-core", 119)
    assert p is not None and p.name == "Dockerfile"
