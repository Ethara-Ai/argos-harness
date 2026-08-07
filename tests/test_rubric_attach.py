"""Layer-2/3 tests: attach step + task_checksum fixed point."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from benchmarks.multiswebench.scripts.harbor.converter import sha256_of_dir
from benchmarks.multiswebench.scripts.rubric.attach import (
    attach_harbor_dir,
    attach_rubric,
    cleanup_stale_tmp,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "rubric"
PKG_943 = REPO_ROOT / "harbor_workspace" / "output" / "tortoise__tortoise-orm-943"
IID = "tortoise__tortoise-orm-943"

pytestmark = pytest.mark.skipif(
    not PKG_943.exists(), reason="local harbor output not present"
)


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    dest = tmp_path / "harbor_out" / IID
    dest.parent.mkdir(parents=True)
    shutil.copytree(PKG_943, dest)
    # Pristine converter output: a fresh convert rmtree's task/ (no rubric/) and
    # writes result.json without a rubric block. The live workspace may have
    # been attached/judged already, so strip those artifacts deterministically.
    shutil.rmtree(dest / "task" / "rubric", ignore_errors=True)
    for report in dest.glob("trajectory/*/run_*/verifier/rubric_report.json"):
        report.unlink()
    for stability in dest.glob("trajectory/*/run_*/verifier/rubric_stability.json"):
        stability.unlink()
    return dest


@pytest.fixture()
def assets_root(tmp_path: Path) -> Path:
    root = tmp_path / "rubric_assets"
    assets = root / IID
    assets.mkdir(parents=True)
    shutil.copy(FIXTURES / "rubric_sample.json", assets / "rubric.json")
    (assets / "TRUTH.md").write_text(
        "# Canonical Solve Notes\nnotes\n", encoding="utf-8"
    )
    (assets / "anchoring_report.json").write_text('{"ok": true}\n', encoding="utf-8")
    return root


def _result_paths(package: Path) -> list[Path]:
    return sorted(package.glob("trajectory/*/run_*/result.json"))


class TestAttach:
    def test_attach_copies_and_patches_checksum(self, package: Path, assets_root: Path):
        report = attach_rubric(package, assets_root / IID)
        assert report["errors"] == []
        assert report["changed"] is True
        assert set(report["copied"]) == {
            "rubric.json",
            "TRUTH.md",
            "anchoring_report.json",
        }
        for name in ("rubric.json", "TRUTH.md", "anchoring_report.json"):
            assert (package / "task" / "rubric" / name).is_file()
        # invariant: task_checksum covers task/ INCLUDING rubric/
        expected = sha256_of_dir(package / "task")
        assert report["new_checksum"] == expected
        for result_path in _result_paths(package):
            result = json.loads(result_path.read_text(encoding="utf-8"))
            assert result["task_checksum"] == expected
        assert report["results_patched"] == len(_result_paths(package))

    def test_attach_is_a_fixed_point(self, package: Path, assets_root: Path):
        attach_rubric(package, assets_root / IID)
        snapshot = {p: p.read_bytes() for p in package.rglob("*") if p.is_file()}
        report2 = attach_rubric(package, assets_root / IID)
        assert report2["changed"] is False
        assert report2["copied"] == []
        assert report2["results_patched"] == 0
        for path, data in snapshot.items():
            assert path.read_bytes() == data, path
        report3 = attach_rubric(package, assets_root / IID)
        assert report3["changed"] is False
        assert report3["new_checksum"] == report2["new_checksum"]

    def test_only_task_checksum_key_changes_in_result(
        self, package: Path, assets_root: Path
    ):
        before = {
            p: json.loads(p.read_text(encoding="utf-8")) for p in _result_paths(package)
        }
        attach_rubric(package, assets_root / IID)
        for result_path, old in before.items():
            new = json.loads(result_path.read_text(encoding="utf-8"))
            old.pop("task_checksum")
            new.pop("task_checksum")
            assert new == old

    def test_reattach_after_asset_edit_updates(self, package: Path, assets_root: Path):
        attach_rubric(package, assets_root / IID)
        (assets_root / IID / "TRUTH.md").write_text(
            "# updated notes\n", encoding="utf-8"
        )
        report = attach_rubric(package, assets_root / IID)
        assert report["changed"] is True
        assert report["copied"] == ["TRUTH.md"]
        text = (package / "task" / "rubric" / "TRUTH.md").read_text(encoding="utf-8")
        assert text == "# updated notes\n"
        expected = sha256_of_dir(package / "task")
        for result_path in _result_paths(package):
            assert json.loads(result_path.read_text())["task_checksum"] == expected

    def test_stale_files_are_removed(self, package: Path, assets_root: Path):
        attach_rubric(package, assets_root / IID)
        stale = package / "task" / "rubric" / "old_leftover.json"
        stale.write_text("{}", encoding="utf-8")
        report = attach_rubric(package, assets_root / IID)
        assert not stale.exists()
        assert report["changed"] is True

    def test_missing_assets_is_error(self, package: Path, tmp_path: Path):
        report = attach_rubric(package, tmp_path / "nope")
        assert report["errors"]
        assert not (package / "task" / "rubric" / "rubric.json").exists()

    def test_attach_harbor_dir_end_to_end(self, package: Path, assets_root: Path):
        logs: list[str] = []
        code = attach_harbor_dir(package.parent, assets_root, log=logs.append)
        assert code == 0
        assert any("updated" in m for m in logs)
        code = attach_harbor_dir(package.parent, assets_root, log=logs.append)
        assert code == 0
        assert any("up-to-date" in m for m in logs)

    def test_cleanup_stale_tmp(self, package: Path):
        junk = package / "task" / ".rubric_tmp.rubric.json"
        junk.write_text("partial", encoding="utf-8")
        assert cleanup_stale_tmp(package) == 1
        assert not junk.exists()
