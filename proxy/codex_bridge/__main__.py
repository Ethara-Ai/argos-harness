"""CLI entry: ``python -m codex_bridge [--host 0.0.0.0] [--port 8766] [--check]``.

Run from the ``proxy/`` directory (which contains the ``codex_bridge`` package),
or via ``./codex_bridge.sh start`` which handles that for you.
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .bridge import build_app
from .credentials import CodexCredentialProvider, CredentialsError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m codex_bridge")
    # 0.0.0.0 so the agent-server inside the eval Docker container can reach the
    # bridge on the host (docker0 gateway 172.17.0.1 on Linux/EC2, or
    # host.docker.internal on macOS Docker Desktop).
    p.add_argument("--host", default="0.0.0.0")  # nosec B104 - bridge must bind all interfaces so containers reach it via the docker0 gateway
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--log-level", default="info")
    p.add_argument(
        "--check",
        action="store_true",
        help="Verify credentials load successfully, then exit.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    provider = CodexCredentialProvider()
    try:
        token = provider.get_access_token()
        account_id = provider.get_account_id()
    except CredentialsError as e:
        print(f"[codex-bridge] credentials error: {e}", file=sys.stderr)
        return 2

    print(
        f"[codex-bridge] credentials OK (token prefix: {token[:12]}..., "
        f"account_id: {account_id})"
    )
    if args.check:
        return 0

    print(f"[codex-bridge] listening on http://{args.host}:{args.port}")
    print("[codex-bridge] point the harness LLM config 'base_url' at:")
    print(
        f"           http://172.17.0.1:{args.port}            (Linux / EC2 docker0 gateway)"
    )
    print(f"           http://host.docker.internal:{args.port}  (macOS Docker Desktop)")
    print(
        f"           http://127.0.0.1:{args.port}             (agent on host, no docker)"
    )
    uvicorn.run(
        build_app(provider),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
