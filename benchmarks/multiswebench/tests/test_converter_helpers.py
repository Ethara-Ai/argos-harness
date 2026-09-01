from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from benchmarks.multiswebench.scripts.harbor.converter import (
    DIFFICULTY_TIERS,
    LANGUAGE_COMMANDS,
    RESOURCE_CONFIG,
    TASK_CATEGORIES,
    TRIVIAL_PASS_RATE,
    UNBANDED_DIFFICULTY,
    classify_category,
    count_source_hunks,
    get_language_commands,
    get_resource_config,
    is_source_path,
    iso8601_microseconds,
    iso8601_microseconds_offset,
    map_difficulty,
    provider_name_split,
    random_trial_suffix,
    read_text,
    render_literal,
    sanitize_task_id,
    sha256_of_dir,
    to_ecr_image,
    validate_instance_id,
)


def test_read_text_returns_file_contents(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hello\nworld", encoding="utf-8")
    assert read_text(f) == "hello\nworld"


def test_read_text_raises_file_not_found(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Missing file"):
        read_text(tmp_path / "missing.txt")


def test_render_literal_replaces_known_keys():
    out = render_literal("a={a} b={b}", a="1", b="two")
    assert out == "a=1 b=two"


def test_render_literal_preserves_unknown_keys():
    out = render_literal("known={a} unknown={x}", a="1")
    assert out == "known=1 unknown={x}"


def test_render_literal_handles_no_placeholders():
    assert render_literal("plain text", a="x") == "plain text"


def test_render_literal_repeated_placeholder():
    out = render_literal("{n} {n} {n}", n="bob")
    assert out == "bob bob bob"


def test_render_literal_ignores_non_word_chars():
    out = render_literal("{not-a-key} {fine}", fine="ok")
    assert "{not-a-key}" in out
    assert "ok" in out


def test_sanitize_task_id_strips_separators():
    assert sanitize_task_id("apache/commons-cli:pr-42") == "apache_commonscli_pr42"


def test_sanitize_task_id_lowercases():
    assert sanitize_task_id("FOO/BAR") == "foo_bar"


def test_sanitize_task_id_prepends_task_prefix_when_first_char_not_alpha():
    assert sanitize_task_id("123abc").startswith("task_")


def test_sanitize_task_id_no_prefix_when_starts_with_letter():
    assert sanitize_task_id("alpha-beta") == "alphabeta"


def _patch_with_hunks(path: str, hunks: int) -> str:
    lines = [f"diff --git a/{path} b/{path}", "--- a/" + path, "+++ b/" + path]
    for i in range(hunks):
        lines.append(f"@@ -{i + 1},1 +{i + 1},2 @@")
        lines.append("-old")
        lines.append("+new")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("passed_of_8", "expected"),
    [
        (0, "expert"),
        (1, "expert"),
        (2, "hard"),
        (3, "medium"),
        (4, "easy"),
        (5, "easy"),
        (6, "easy"),
        (7, "trivial"),
        (8, "trivial"),
    ],
)
def test_map_difficulty_band_boundaries(passed_of_8: int, expected: str):
    assert map_difficulty(passed_of_8 / 8) == expected


@pytest.mark.parametrize(
    ("pass_rate", "expected"),
    [
        (0.0, "expert"),
        (0.2499, "expert"),
        (0.25, "hard"),
        (0.3749, "hard"),
        (0.375, "medium"),
        (0.4999, "medium"),
        (0.50, "easy"),
        (0.8749, "easy"),
        (0.875, "trivial"),
        (1.0, "trivial"),
    ],
)
def test_map_difficulty_boundaries_are_lower_inclusive(pass_rate: float, expected: str):
    assert map_difficulty(pass_rate) == expected


def test_map_difficulty_unmeasured_is_unbanded():
    assert map_difficulty() == UNBANDED_DIFFICULTY
    assert map_difficulty(None) == UNBANDED_DIFFICULTY
    assert UNBANDED_DIFFICULTY not in set(DIFFICULTY_TIERS)


def test_trivial_tier_is_labelled():
    assert map_difficulty(TRIVIAL_PASS_RATE) == "trivial"
    assert map_difficulty(1.0) == "trivial"
    assert "trivial" in set(DIFFICULTY_TIERS)


def test_difficulty_pass_rate_is_monotonic():
    order = ["expert", "hard", "medium", "easy", "trivial"]
    seen = [map_difficulty(n / 8) for n in range(9)]
    assert [order.index(d) for d in seen] == sorted(order.index(d) for d in seen)


def test_count_source_hunks_excludes_test_paths():
    source = _patch_with_hunks("src/app/core.py", 3)
    tests = _patch_with_hunks("tests/test_core.py", 40)
    assert count_source_hunks(source + tests) == 3


@pytest.mark.parametrize(
    "excluded",
    [
        "tests/test_core.py",
        "test/helper.py",
        "e2e/flow.spec.ts",
        "testdata/fixture.json",
        "src/__tests__/widget.tsx",
        "spec/models/user_spec.rb",
        "docs/guide.py",
        "README.md",
        "docs/api.rst",
        ".github/workflows/ci.yml",
        "poetry.lock",
        "go.sum",
        "go.mod",
        "package-lock.json",
        "yarn.lock",
        "Cargo.lock",
        "ui/pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "Gemfile.lock",
        "uv.lock",
        "charts/app/templates/deploy.yaml",
        "manifests/base/service.yaml",
    ],
)
def test_count_source_hunks_skips_non_source_file(excluded: str):
    assert is_source_path(excluded) is False
    assert count_source_hunks(_patch_with_hunks(excluded, 5)) == 0


@pytest.mark.parametrize(
    "kept",
    [
        "src/app/core.py",
        "application/src/main/java/run/halo/app/Index.java",
        "pkg/server/handler.go",
        "ui/components/Button.tsx",
        "latest/contest.py",
    ],
)
def test_count_source_hunks_keeps_source_file(kept: str):
    assert is_source_path(kept) is True
    assert count_source_hunks(_patch_with_hunks(kept, 4)) == 4


def test_count_source_hunks_ignores_hunk_lines_before_any_header():
    assert count_source_hunks("@@ -1,1 +1,1 @@\n-a\n+b\n") == 0


def test_count_source_hunks_counts_across_mixed_files():
    patch = (
        _patch_with_hunks("src/a.py", 2)
        + _patch_with_hunks("tests/test_a.py", 9)
        + _patch_with_hunks("src/b.py", 3)
        + _patch_with_hunks("docs/a.md", 7)
    )
    assert count_source_hunks(patch) == 5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fix crash when parsing empty config", "bug_fixing"),
        ("NullPointerException raised on startup", "bug_fixing"),
        ("Pagination returns incorrect rows on last page", "bug_fixing"),
        ("Add support for custom storage backends", "feature_development"),
        ("Implement a new --dry-run command", "feature_development"),
        ("Introduce dark mode toggle", "feature_development"),
        ("Optimize the resolver cache for faster builds", "system_optimization"),
        ("Reduce memory usage in the index writer", "system_optimization"),
        ("Speed up graph traversal", "system_optimization"),
        ("Refactor the settings module and rename helpers", "code_refactoring"),
        ("Simplify duplicated validation logic", "code_refactoring"),
        ("Cleanup dead branches in the loader", "code_refactoring"),
        ("Fix lint errors and add missing docstrings", "code_review"),
        ("Correct typos in inline comments", "code_review"),
        ("Compatibility with Python 3.12", "integration_bug"),
        ("Upgrade the postgres dependency to 17", "integration_bug"),
        ("Plugin adapter breaks against the new SDK", "integration_bug"),
    ],
)
def test_classify_category_labels(text: str, expected: str):
    assert classify_category(text) == expected


def test_classify_category_covers_all_six_labels():
    samples = {
        "bug_fixing": "Fix the broken redirect",
        "feature_development": "Add support for webhooks",
        "system_optimization": "Optimize cache lookups",
        "code_review": "Fix lint and docstring nits",
        "code_refactoring": "Refactor the parser",
        "integration_bug": "Upgrade dependency for compatibility",
    }
    assert set(samples) == set(TASK_CATEGORIES)
    for expected, text in samples.items():
        assert classify_category(text) == expected


def test_classify_category_reads_record_title_and_body():
    record = {
        "title": "Crash on save",
        "body": "The editor raises an exception when the title is empty.",
    }
    assert classify_category(record) == "bug_fixing"


def test_classify_category_reads_resolved_issue_prose():
    record = {
        "title": "",
        "body": "",
        "resolved_issues": [{"title": "Add support for YAML output", "body": ""}],
    }
    assert classify_category(record) == "feature_development"


def test_classify_category_falls_back_to_patch_as_weak_signal():
    record = {
        "title": "Update module",
        "body": "",
        "fix_patch": _patch_with_hunks("src/cache.py", 1) + "+# optimize lookup\n",
    }
    assert classify_category(record) == "system_optimization"


def test_classify_category_defaults_to_bug_fixing():
    assert classify_category("") == "bug_fixing"
    assert classify_category({}) == "bug_fixing"
    assert classify_category("Update module contents") == "bug_fixing"


def test_classify_category_always_returns_valid_label():
    for text in ["", "???", "Add", "Fix", "random prose with no signal"]:
        assert classify_category(text) in TASK_CATEGORIES


def test_to_ecr_image_format():
    assert to_ecr_image("reg", "apache", "kafka", 42) == "reg/apache__kafka:pr-42"


def test_to_ecr_image_lowercases_org_and_repo():
    assert to_ecr_image("reg", "CycloneDX", "cdxgen", 7) == "reg/cyclonedx__cdxgen:pr-7"


def test_get_resource_config_known_language_repo_specific():
    cfg = get_resource_config("c", "ponyc/foo")
    assert cfg["memory_mb"] == 16384


def test_get_resource_config_falls_back_to_lang_default():
    cfg = get_resource_config("c", "unknown-repo")
    assert cfg["memory_mb"] == 8192


def test_get_resource_config_unknown_language_falls_back_to_global_default():
    cfg = get_resource_config("brainfuck", "x")
    assert cfg == RESOURCE_CONFIG["_default"]["_default"]


def test_get_resource_config_case_insensitive_on_language():
    cfg = get_resource_config("JAVA", "dubbo")
    assert cfg["memory_mb"] == 16384


def test_get_resource_config_repo_matches_substring_case_insensitive():
    cfg = get_resource_config("typescript", "facebook/Material-UI-Pickers")
    assert cfg["memory_mb"] == 16384


def test_get_language_commands_python():
    assert get_language_commands("python") == LANGUAGE_COMMANDS["python"]


def test_get_language_commands_case_insensitive():
    assert get_language_commands("PYTHON") == LANGUAGE_COMMANDS["python"]


def test_get_language_commands_unknown_returns_placeholders():
    run, test = get_language_commands("foo-lang")
    assert "appropriate" in run
    assert "appropriate" in test


def test_iso8601_microseconds_normalizes_z_suffix():
    out = iso8601_microseconds("2024-01-01T12:00:00Z")
    assert out.endswith("Z")
    assert "+00:00" not in out
    assert "2024-01-01" in out


def test_iso8601_microseconds_returns_now_when_blank():
    out = iso8601_microseconds("")
    parsed = datetime.fromisoformat(out.replace("Z", "+00:00"))
    assert (datetime.now(timezone.utc) - parsed).total_seconds() < 5


def test_iso8601_microseconds_returns_now_when_none():
    out = iso8601_microseconds(None)
    assert out.endswith("Z")


def test_iso8601_microseconds_passthrough_on_unparseable():
    assert iso8601_microseconds("not a date") == "not a date"


def test_iso8601_microseconds_offset_keeps_offset_suffix():
    out = iso8601_microseconds_offset("2024-01-01T12:00:00Z")
    assert out.endswith("+00:00")
    assert not out.endswith("Z")


def test_iso8601_microseconds_offset_returns_now_when_none():
    out = iso8601_microseconds_offset(None)
    assert "+00:00" in out


def test_iso8601_microseconds_offset_passthrough_on_unparseable():
    assert iso8601_microseconds_offset("bad") == "bad"


def test_iso8601_microseconds_naive_input_assumed_utc():
    out = iso8601_microseconds("2024-06-15T10:30:00")
    assert "2024-06-15T10:30:00" in out
    assert out.endswith("Z")


def test_provider_name_split_with_dot():
    assert provider_name_split("openai.gpt-4") == ("openai", "gpt-4")


def test_provider_name_split_without_dot():
    assert provider_name_split("claude") == ("", "claude")


def test_provider_name_split_multiple_dots_only_partitions_once():
    assert provider_name_split("a.b.c") == ("a", "b.c")


def test_random_trial_suffix_default_length():
    out = random_trial_suffix()
    assert len(out) == 7
    assert re.fullmatch(r"[A-Za-z0-9]+", out)


def test_random_trial_suffix_custom_length():
    out = random_trial_suffix(length=12)
    assert len(out) == 12


def test_random_trial_suffix_uses_alnum_only():
    for _ in range(50):
        out = random_trial_suffix(20)
        assert re.fullmatch(r"[A-Za-z0-9]+", out)


def test_sha256_of_dir_deterministic(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.txt").write_text("world")
    h1 = sha256_of_dir(tmp_path)
    h2 = sha256_of_dir(tmp_path)
    assert h1 == h2
    assert len(h1) == 64


def test_sha256_of_dir_changes_with_content(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    h1 = sha256_of_dir(tmp_path)
    (tmp_path / "a.txt").write_text("hello!")
    h2 = sha256_of_dir(tmp_path)
    assert h1 != h2


def test_sha256_of_dir_changes_with_filename(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    h1 = sha256_of_dir(tmp_path)
    (tmp_path / "a.txt").rename(tmp_path / "renamed.txt")
    h2 = sha256_of_dir(tmp_path)
    assert h1 != h2


def test_sha256_of_dir_ignores_subdir_paths_consistently(tmp_path: Path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "x.txt").write_text("nested")
    h1 = sha256_of_dir(tmp_path)
    h2 = sha256_of_dir(tmp_path)
    assert h1 == h2


@pytest.mark.parametrize(
    "valid_id",
    [
        "apache__commons-cli__CLI-291",
        "o__r-1",
        "octo__demo-5",
        "no-underscore-here",
        "noHyphen",
        "a.b_c-1",
    ],
)
def test_validate_instance_id_accepts_real_ids(valid_id: str):
    # S-002: real instance ids pass through unchanged.
    assert validate_instance_id(valid_id) == valid_id


@pytest.mark.parametrize(
    "bad_id",
    [
        "..",
        "../evil",
        "a/../../etc/passwd",
        "foo/bar",
        "a\\b",
        "/abs/path",
        "ok/..",
        "",
        "has space",
    ],
)
def test_validate_instance_id_rejects_traversal_and_separators(bad_id: str):
    # S-002: path separators / parent refs / unsafe chars are rejected.
    with pytest.raises(ValueError, match="path-traversal guard"):
        validate_instance_id(bad_id)
