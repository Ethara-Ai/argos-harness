"""Focused no-network regression tests for run_eval.sh publishing.

The publish flow (reintroduced by request, previously removed) stages each
finished dataset into a clone of the publish repo, commits it, and pushes all
commits at the end of the run. Requirements tested:

- Publishing options (--no-push, --data-repo, --env-file) are accepted by the parser
- The GitHub token is verified against the GitHub API before any expensive work
- --no-push short-circuits token verification, commits, and the final push
- The publish repo is cloned into --data-dir on start when missing
- An existing clone's origin must match --data-repo
- Per-dataset commits cover both the flat <uuid>/ bundle and the legacy split
- The final push ships accumulated commits, with one pull-rebase retry
- The token-authenticated URL is never written to .git/config
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def run_eval_src() -> str:
    return (REPO_ROOT / "run_eval.sh").read_text(encoding="utf-8")


# ── Publishing options are accepted ──────────────────────────────────────────


@pytest.mark.parametrize("option", ["--no-push", "--data-repo", "--env-file"])
def test_publishing_options_are_known(option: str):
    """Publishing options must NOT trigger 'Unknown option'. With no
    --llm-config the parser still exits nonzero, but for the right reason."""
    cmd = ["bash", str(REPO_ROOT / "run_eval.sh"), option]
    if option in ("--data-repo", "--env-file"):
        cmd.append("dummy_value")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0  # missing --llm-config etc.
    combined = proc.stdout + proc.stderr
    assert "Unknown option" not in combined, (
        f"{option} should be a known option: {combined[:200]}"
    )


def test_publishing_options_in_parser(run_eval_src: str):
    parser_start = run_eval_src.index("while [[ $# -gt 0 ]]; do")
    parser_block = run_eval_src[
        parser_start : run_eval_src.index("\ndone", parser_start)
    ]
    for flag in ("--no-push", "--data-repo", "--env-file", "--data-dir"):
        assert f"{flag})" in parser_block, f"{flag} missing from parser"


# ── GitHub API token verification ────────────────────────────────────────────


def test_github_token_verified_via_api(run_eval_src: str):
    """The PAT is validated against /user and /repos/<owner>/<repo> before any
    expensive work, deriving the commit identity from the token owner."""
    assert "https://api.github.com/user" in run_eval_src
    assert "https://api.github.com/repos/${repo_path}" in run_eval_src
    assert "verify_github_token_and_identity" in run_eval_src
    # Identity comes from the API response, not a shared bot
    assert 'GIT_EMAIL="${api_id}+${api_login}@users.noreply.github.com"' in run_eval_src


def test_no_push_short_circuits_verification(run_eval_src: str):
    """--no-push must skip token verification entirely."""
    fn_start = run_eval_src.index("verify_github_token_and_identity() {")
    fn_head = run_eval_src[fn_start : fn_start + 200]
    assert '[[ "$NO_PUSH" == true ]] && return 0' in fn_head


def test_token_read_from_env_file_or_environment(run_eval_src: str):
    assert "read_env_var" in run_eval_src
    assert "GITHUB_TOKEN" in run_eval_src
    assert "GH_TOKEN" in run_eval_src


# ── Clone / origin verification ──────────────────────────────────────────────


def test_clone_on_missing_data_dir(run_eval_src: str):
    assert "git clone" in run_eval_src
    assert 'DATA_PUBLISH_DIR="${SCRIPT_DIR}/../milo-bench-dataset"' in run_eval_src


def test_existing_clone_origin_must_match(run_eval_src: str):
    assert "_normalize_git_url" in run_eval_src
    assert "does not match --data-repo" in run_eval_src


def test_authed_url_never_persisted(run_eval_src: str):
    """The token-authenticated URL is used ad hoc for clone/push, never written
    into .git/config via git remote set-url."""
    assert "_authed_url" in run_eval_src
    assert "git remote set-url" not in run_eval_src


# ── Per-dataset commit ───────────────────────────────────────────────────────


def test_per_dataset_commit_covers_both_layouts(run_eval_src: str):
    """The commit must include the flat <uuid>/ bundle and the legacy
    dataset/<uuid> + trajectory/<uuid> split."""
    assert re.search(
        r'git -C "\$DATA_REPO_ROOT" add -- "\$uuid" "dataset/\$uuid" "trajectory/\$uuid"',
        run_eval_src,
    )
    assert 'commit -q -m "data: $uuid ($iid)"' in run_eval_src


def test_commit_serialized_by_lock(run_eval_src: str):
    assert "run_eval_commit.lock" in run_eval_src


def test_uuid_child_dirs_still_created(run_eval_src: str):
    assert 'd_bundle="$PUBLISH_BASE/$uuid"' in run_eval_src


# ── Final push ───────────────────────────────────────────────────────────────


def test_final_push_present_with_retry(run_eval_src: str):
    assert "PUSH_LOCK" in run_eval_src
    assert "PUSH_ENABLED" in run_eval_src
    assert 'push "$_target" "HEAD:$GIT_BRANCH"' in run_eval_src
    assert "pull --rebase --autostash" in run_eval_src
    # Commits survive a failed push
    assert "kept locally" in run_eval_src


def test_no_push_skips_final_push(run_eval_src: str):
    assert "skipping final push (local commits preserved" in run_eval_src


# ── Large-file guard still in place ──────────────────────────────────────────


def test_github_100mib_guard(run_eval_src: str):
    assert "104857600" in run_eval_src


# ── README stays in step with the parser ─────────────────────────────────────


@pytest.fixture(scope="module")
def readme_src() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def test_readme_documented_flags_exist_in_parser(readme_src: str, run_eval_src: str):
    """Every long flag documented in the run section must exist in the parser."""
    section = readme_src[
        readme_src.index("### The run") : readme_src.index("### Which model does what")
    ]
    # Only flag-table rows (| `--flag ...` |) — prose can mention flags of
    # other tools (e.g. `uv sync --extra compression`).
    documented = sorted(
        set(re.findall(r"^\| `(--[A-Za-z][A-Za-z0-9-]*)", section, re.M))
    )
    assert documented, "no flags found in the README run section"

    parser_start = run_eval_src.index("while [[ $# -gt 0 ]]; do")
    parser_block = run_eval_src[
        parser_start : run_eval_src.index("\ndone", parser_start)
    ]
    for flag in documented:
        assert f"{flag})" in parser_block, (
            f"README documents {flag}, absent from the run_eval.sh parser"
        )


# ── Syntax validation ────────────────────────────────────────────────────────


def test_bash_syntax_valid():
    proc = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "run_eval.sh")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"bash -n failed:\n{proc.stderr}"
