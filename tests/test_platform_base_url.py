"""Pins for the platform base_url guard in run_eval.sh / run_custom_eval.sh.

A committed .llm_config bridge config can only encode one platform's bridge
host, and both directions break: 172.17.x.x (Linux docker0 gateway) does not
exist on macOS Docker Desktop, and host.docker.internal does not resolve in
plain Linux Docker (no --add-host on the agent containers). Without a rewrite
every LLM call fails and the trajectory comes back empty (proven live
2026-08-18; see THINKING_CAPTURE_JOURNAL.md). Both run scripts must rewrite
the wrong-platform host into a temp config, before the headroom block reads
the upstream base_url:

- Darwin: http://172.17.x.x      -> http://host.docker.internal
- Linux:  http://host.docker.internal -> http://172.17.0.1
"""

import json
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_EVAL = REPO_ROOT / "run_eval.sh"
RUN_CUSTOM_EVAL = REPO_ROOT / "run_custom_eval.sh"

GUARD_START = '_os="$(uname -s)"'


@pytest.fixture(scope="module", params=["run_eval.sh", "run_custom_eval.sh"])
def script_src(request) -> str:
    return (REPO_ROOT / request.param).read_text(encoding="utf-8")


def test_both_direction_branches_present(script_src):
    guard = _guard_block(script_src)
    assert '"$_os" == "Darwin" && "$_cfg_base" =~ ^http://172\\.17\\.' in guard
    assert (
        '"$_os" == "Linux" && "$_cfg_base" =~ ^http://host\\.docker\\.internal' in guard
    )
    assert "http://host.docker.internal" in guard
    assert "172.17.0.1" in guard


def test_guard_rewrites_via_mk_temp_llm_config(script_src):
    guard = _guard_block(script_src)
    assert "mk_temp_llm_config" in guard
    assert 'LLM_CONFIG="$PLATFORM_TEMP_CFG"' in guard


def test_darwin_branch_matches_any_docker0_address(script_src):
    # 172.17.0.1 is the default but the subnet is configurable; the Darwin
    # branch must match the whole 172.17.x.x range, not one literal address.
    guard = _guard_block(script_src)
    assert re.search(r"172\\\.17\\\.\[0-9\]\+\\\.\[0-9\]\+", guard)


def test_guard_runs_before_headroom_upstream_read(script_src):
    # The headroom proxy uses the config's base_url as its upstream; an
    # unrewritten wrong-platform upstream is unreachable from the host too.
    guard_pos = script_src.index(GUARD_START)
    upstream_pos = script_src.index("ORIG_BASE_URL=")
    assert guard_pos < upstream_pos


def test_mktemp_template_is_bsd_portable(script_src):
    # BSD/macOS mktemp only substitutes TRAILING X's; a suffix after them makes
    # it use the literal template name, which succeeds once and then fails
    # "File exists" forever after. The template must end with the X's.
    for m in re.finditer(r'mktemp "\$\{TMPDIR:-/tmp\}/([^"]+)"', script_src):
        assert m.group(1).endswith("XXXXXX"), f"non-portable template: {m.group(1)}"


def test_temp_config_cleanup_registered(script_src):
    cleanup = re.search(r"^_cleanup_compression\(\) \{.*?^\}", script_src, re.S | re.M)
    assert cleanup, "_cleanup_compression not found"
    assert "PLATFORM_TEMP_CFG" in cleanup.group(0)


def _guard_block(src: str) -> str:
    start = src.index(GUARD_START)
    end = src.index('RUNTIME_LLM_CONFIG="$LLM_CONFIG"', start)
    return src[start:end]


def _mk_temp_fn(src: str) -> str:
    m = re.search(r"^mk_temp_llm_config\(\) \{.*?^\}", src, re.S | re.M)
    assert m, "mk_temp_llm_config not found"
    return m.group(0)


def _run_guard(tmp_path: Path, base_url: str, os_name: str) -> tuple[str, str]:
    """Execute the actual guard block from run_eval.sh with uname shimmed to
    os_name. Returns (final LLM_CONFIG path, its base_url)."""
    src = RUN_EVAL.read_text(encoding="utf-8")
    cfg = tmp_path / "cfg.json"
    cfg.write_text(
        json.dumps(
            {"model": "anthropic/claude-opus-4-8", "base_url": base_url, "api_key": "x"}
        )
    )
    snippet = "\n".join(
        [
            f"uname() {{ echo {os_name}; }}",
            'log() { echo "$@"; }',
            _mk_temp_fn(src),
            f'LLM_CONFIG="{cfg}"',
            _guard_block(src),
            'printf "FINAL:%s" "$LLM_CONFIG"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", snippet], capture_output=True, text=True, check=True
    )
    final = proc.stdout.rsplit("FINAL:", 1)[1].strip()
    return final, json.loads(Path(final).read_text())["base_url"]


def _cleanup(tmp_path: Path, final: str) -> None:
    if final != str(tmp_path / "cfg.json"):
        Path(final).unlink(missing_ok=True)


def test_darwin_rewrites_docker0(tmp_path):
    final, new_base = _run_guard(tmp_path, "http://172.17.0.1:8765", "Darwin")
    try:
        assert final != str(tmp_path / "cfg.json"), "expected a temp config"
        assert new_base == "http://host.docker.internal:8765"
        orig = json.loads((tmp_path / "cfg.json").read_text())
        assert orig["base_url"] == "http://172.17.0.1:8765"
    finally:
        _cleanup(tmp_path, final)


def test_linux_rewrites_docker_desktop_host(tmp_path):
    final, new_base = _run_guard(tmp_path, "http://host.docker.internal:8766", "Linux")
    try:
        assert final != str(tmp_path / "cfg.json"), "expected a temp config"
        assert new_base == "http://172.17.0.1:8766"
    finally:
        _cleanup(tmp_path, final)


def test_darwin_leaves_docker_desktop_host_alone(tmp_path):
    final, base = _run_guard(tmp_path, "http://host.docker.internal:8766", "Darwin")
    assert final == str(tmp_path / "cfg.json")
    assert base == "http://host.docker.internal:8766"


def test_linux_leaves_docker0_alone(tmp_path):
    final, base = _run_guard(tmp_path, "http://172.17.0.1:8765", "Linux")
    assert final == str(tmp_path / "cfg.json")
    assert base == "http://172.17.0.1:8765"


def test_both_leave_https_urls_alone(tmp_path):
    for os_name in ("Darwin", "Linux"):
        final, base = _run_guard(
            tmp_path, "https://llm-proxy.eval.all-hands.dev", os_name
        )
        assert final == str(tmp_path / "cfg.json"), (
            f"guard must not touch https ({os_name})"
        )
        assert base == "https://llm-proxy.eval.all-hands.dev"


def test_guard_survives_repeat_invocation(tmp_path):
    # Regression for the BSD mktemp literal-template bug: the second rewrite
    # on the same machine must not fail with "File exists".
    a, _ = _run_guard(tmp_path, "http://172.17.0.1:8765", "Darwin")
    b, _ = _run_guard(tmp_path, "http://172.17.0.1:8765", "Darwin")
    try:
        assert a != b, "temp config paths must be unique per invocation"
    finally:
        _cleanup(tmp_path, a)
        _cleanup(tmp_path, b)
