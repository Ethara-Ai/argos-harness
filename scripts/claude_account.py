"""Fetch the Claude account (uuid/name/email) behind Claude Code's OAuth login.

The harness generates trajectories on a Claude subscription authenticated by
reusing Claude Code's stored OAuth login (not an API key). This module reads
that credential store and asks the profile endpoint Claude Code's own /status
UI uses, so runs can be tagged with which account/subscription produced them.

The endpoint is not part of Anthropic's public API contract; callers should
treat failures as non-fatal (see get_claude_account_info_safe).
"""

from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path

import requests


PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


def _read_claude_credentials() -> dict:
    """Read the OAuth credential blob Claude Code's `claude login` stored locally."""
    system = platform.system()
    if system == "Darwin":
        raw = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return json.loads(raw)
    # Linux (and Claude Code's fallback file store)
    cred_path = Path.home() / ".claude" / ".credentials.json"
    return json.loads(cred_path.read_text())


def get_claude_account_info() -> dict:
    """Account/org info for the Claude subscription Claude Code is logged into.

    Returns a dict with keys: account_uuid, name, email, organization_uuid,
    subscription_status, rate_limit_tier. The credential store is re-read on
    every call so Claude Code's own background token refresh keeps us current.
    """
    creds = _read_claude_credentials()
    oauth = creds["claudeAiOauth"]
    access_token = oauth["accessToken"]

    resp = requests.get(
        PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=30
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "Claude subscription token expired or invalid — run `claude login` again."
        )
    resp.raise_for_status()
    data = resp.json()

    account = data["account"]
    org = data.get("organization", {})
    return {
        "account_uuid": account["uuid"],
        "name": account.get("full_name") or account.get("display_name"),
        "email": account["email"],
        "organization_uuid": org.get("uuid"),
        "subscription_status": org.get("subscription_status"),
        "rate_limit_tier": org.get("rate_limit_tier"),
    }


def get_claude_account_info_safe() -> dict | None:
    """Like get_claude_account_info, but returns None instead of raising."""
    try:
        return get_claude_account_info()
    except Exception:
        return None


if __name__ == "__main__":
    print(json.dumps(get_claude_account_info(), indent=2))
