"""The fixed quality block: seven positive items, one per dimension, lint-clean,
with stable digests, shipped as package data."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from assay import quality
from assay.lint import lint_quality


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_manifest_has_one_item_per_dimension_on_the_ladder():
    items = quality.load_quality_items(quality.MANIFEST_PATH)
    assert [i.id for i in items] == [f"Q{n}" for n in range(1, 8)]
    assert sorted(i.dimension for i in items) == sorted(quality.QUALITY_DIMENSIONS)
    assert all(i.weight in (1, 3, 5) for i in items)
    assert all(i.effective_weight == float(i.weight) for i in items)
    assert all(i.evaluation_target == "final_diff" for i in items)
    assert all("final_diff" in i.evidence for i in items)


def test_manifest_passes_lint():
    res = lint_quality(quality.MANIFEST_PATH)
    assert res.ok, [str(f) for f in res.errors]


def test_lint_rejects_missing_dimension_and_negative_weight(tmp_path: Path):
    doc = quality.load_manifest(quality.MANIFEST_PATH)
    doc["items"] = doc["items"][:-1]
    doc["items"][0]["weight"] = -3
    p = tmp_path / "quality.json"
    p.write_text(json.dumps(doc))
    rules = {f.rule for f in lint_quality(p).errors}
    assert "Q002-dimension-set" in rules
    assert "Q003-negative" in rules


def test_loader_rejects_bad_or_missing_manifest(tmp_path: Path):
    doc = quality.load_manifest(quality.MANIFEST_PATH)
    doc["items"][0]["dimension"] = "honesty"
    p = tmp_path / "quality.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="dimensions"):
        quality.load_quality_items(p)
    doc = quality.load_manifest(quality.MANIFEST_PATH)
    doc["quality_version"] = "quality-v0"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="version"):
        quality.load_quality_items(p)
    with pytest.raises(quality.ManifestMissing):
        quality.load_quality_items(tmp_path / "absent.json")


def test_digests_are_stable_and_sensitive(tmp_path: Path):
    m = quality.MANIFEST_PATH
    a, b = quality.manifest_digest(m), quality.manifest_digest(m)
    assert a == b and len(a) == 16
    pa, pb = quality.prompt_digest(m), quality.prompt_digest(m)
    assert pa == pb and len(pa) == 16 and pa != a
    doc = quality.load_manifest(m)
    doc["items"][0]["criterion"] += " Also this."
    p = tmp_path / "quality.json"
    p.write_text(json.dumps(doc))
    assert quality.manifest_digest(p) != a
    assert quality.prompt_digest(p) != pa


def test_rubric_lint_still_rejects_quality_dimensions(tmp_path: Path):
    """The refactor must not widen the rubric vocabulary."""
    from assay.lint import lint_rubric

    items = quality.load_manifest(quality.MANIFEST_PATH)["items"]
    doc = {"items": [dict(items[0], weight=-3), items[1]]}
    p = tmp_path / "rubrics.json"
    p.write_text(json.dumps(doc))
    assert "R030-dimension" in {f.rule for f in lint_rubric(p).errors}


def test_fixture_rubrics_lint_unchanged():
    from assay.lint import lint_rubric

    fixtures = REPO_ROOT / "tests" / "fixtures" / "argos_bundles"
    for b in sorted(fixtures.glob("*-*")):
        res = lint_rubric(b / "tests" / "rubrics.json")
        assert res.ok, (b.name, [str(f) for f in res.errors])


def test_manifests_are_declared_package_data():
    """A built wheel must carry the JSON the scorer reads beside its modules;
    the editable install hid that both were missing."""
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    data = cfg["tool"]["setuptools"]["package-data"]["assay"]
    assert "quality.json" in data and "preamble.json" in data
    assert (REPO_ROOT / "assay" / "quality.json").is_file()
    assert (REPO_ROOT / "assay" / "preamble.json").is_file()


def test_utf8_clip_never_splits_a_code_point():
    text = "a" * 5 + "✓" * 5  # ✓ is 3 bytes
    out, cut = quality.clip_utf8(text, 7)
    assert cut and out == "a" * 5  # 7 bytes lands mid-code-point; dropped whole
    out, cut = quality.clip_utf8(text, 8)
    assert cut and out == "a" * 5 + "✓"
    assert quality.clip_utf8(text, 100) == (text, False)
