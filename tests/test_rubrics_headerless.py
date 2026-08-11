"""The headerless rubrics.json format (3 keys: items, checks, sites).

The five header keys (schema_version, authored_by, task_uuid, instance_id,
description) were removed from the delivery format per TL spec. Two things must
hold forever after: the generator emits only the 3-key shape, and the
hand-authored guard — which used to read the in-file authored_by stamp — still
protects hand-written rubrics via TRUTH.md's Provenance block.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay.author import AUTHOR_STAMP, generate_rubric
from assay.cli import _is_hand_authored


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "milo_bundles"

TRUTH_GENERATED = """# TRUTH.md - x

## Provenance

```yaml
authored_by: generated
```
"""


def test_generate_rubric_emits_only_items():
    doc = json.loads(
        generate_rubric(sites=[], test_paths=[], target_ids=[], issue_titles=["t (#1)"])
    )
    assert list(doc) == ["items"]


@pytest.mark.parametrize("uuid", [f.name for f in sorted(FIXTURES.glob("*-*"))])
def test_fixture_bundles_are_headerless(uuid: str):
    doc = json.loads((FIXTURES / uuid / "tests" / "rubrics.json").read_text())
    assert list(doc) == ["items", "checks", "sites"], uuid


# ---------------------------------------------------------------- guard


def _bundle(tmp_path: Path, rubric: str | None, truth: str | None) -> Path:
    """A minimal bundle shaped like the real thing: the guard walks
    rubric_path.parent.parent to find TRUTH.md."""
    root = tmp_path / "00000000-0000-0000-0000-000000000000"
    (root / "tests").mkdir(parents=True)
    rubric_path = root / "tests" / "rubrics.json"
    if rubric is not None:
        rubric_path.write_text(rubric)
    if truth is not None:
        (root / "TRUTH.md").write_text(truth)
    return rubric_path


def test_missing_rubric_is_not_hand_authored(tmp_path: Path):
    assert not _is_hand_authored(_bundle(tmp_path, None, None))


def test_old_format_generator_stamp(tmp_path: Path):
    rubric = json.dumps({"authored_by": AUTHOR_STAMP, "items": []})
    assert not _is_hand_authored(_bundle(tmp_path, rubric, None))


def test_old_format_foreign_stamp_is_hand_authored(tmp_path: Path):
    rubric = json.dumps({"authored_by": "a human", "items": []})
    assert _is_hand_authored(_bundle(tmp_path, rubric, None))


def test_new_format_generated_provenance(tmp_path: Path):
    rubric = json.dumps({"items": [], "checks": {}, "sites": []})
    assert not _is_hand_authored(_bundle(tmp_path, rubric, TRUTH_GENERATED))


def test_new_format_human_provenance_is_hand_authored(tmp_path: Path):
    truth = TRUTH_GENERATED.replace("generated", "a. human")
    rubric = json.dumps({"items": [], "checks": {}, "sites": []})
    assert _is_hand_authored(_bundle(tmp_path, rubric, truth))


def test_new_format_missing_truth_is_not_hand_authored(tmp_path: Path):
    rubric = json.dumps({"items": [], "checks": {}, "sites": []})
    assert not _is_hand_authored(_bundle(tmp_path, rubric, None))


def test_new_format_malformed_provenance_protects(tmp_path: Path):
    truth = TRUTH_GENERATED.replace("authored_by: generated", "authored_by: [unclosed")
    rubric = json.dumps({"items": [], "checks": {}, "sites": []})
    assert _is_hand_authored(_bundle(tmp_path, rubric, truth))


def test_unparseable_rubric_protects(tmp_path: Path):
    assert _is_hand_authored(_bundle(tmp_path, "{not json", TRUTH_GENERATED))
