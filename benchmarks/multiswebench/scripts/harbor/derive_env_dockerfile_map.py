#!/usr/bin/env python3
"""Derive the per-PR env-Dockerfile map for a multi-Dockerfile repo batch.

Ground truth = the already-built ECR images: each ``{org}_m_{repo}:pr-N``
image was built FROM one of the candidate ``golang:X.Y`` bases, and that
choice is recorded in the image config's ``GOLANG_VERSION`` env. This script
reads that back out (metadata only, no image pull) and matches it against the
``FROM golang:X.Y`` line of each candidate Dockerfile in --dockerfile-dir,
writing an explicit ``{"<pr_number>": "<filename>"}`` map.

PRs whose Go version matches no candidate are printed as EXCEPTIONS and left
out of the map (the converter then falls back to the template with a WARN);
those go back to the TL.

Usage (see env_dockerfiles/README.md):
    uv run python benchmarks/multiswebench/scripts/harbor/derive_env_dockerfile_map.py \
        --dataset finalXTLS_shippable.jsonl \
        --ecr-prefix <acct>.dkr.ecr.<region>.amazonaws.com/<repo-prefix> \
        --dockerfile-dir .../env_dockerfiles/xtls_m_xray-core \
        --out .../env_dockerfiles/xtls_m_xray-core/map.json

Requires a valid docker login to the ECR registry.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


def to_ecr_image(ecr_prefix: str, org: str, repo: str, pr: int) -> str:
    # Mirrors converter.to_ecr_image + run_eval.sh's lowercasing.
    return f"{ecr_prefix}/{org}_m_{repo}:pr-{pr}".lower()


def read_dataset_prs(dataset: Path) -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    with dataset.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rows.append((rec["org"], rec["repo"], int(rec["number"])))
    return rows


def candidate_go_versions(dockerfile_dir: Path) -> dict[str, str]:
    """Map ``major.minor`` Go version -> Dockerfile filename."""
    table: dict[str, str] = {}
    for path in sorted(dockerfile_dir.iterdir()):
        if not path.is_file() or not path.name.startswith("Dockerfile"):
            continue
        m = re.search(
            r"^FROM\s+golang:(\d+\.\d+)", path.read_text(encoding="utf-8"), re.MULTILINE
        )
        if not m:
            print(f"WARN: {path.name} has no 'FROM golang:X.Y' line; skipped")
            continue
        version = m.group(1)
        if version in table:
            print(
                f"WARN: duplicate go {version} in {path.name} and {table[version]}; "
                f"keeping {table[version]}"
            )
            continue
        table[version] = path.name
    return table


def image_go_version(image: str) -> str | None:
    """Return ``major.minor`` GOLANG_VERSION from the image config, or None."""
    proc = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            image,
            "--format",
            "{{json .Image}}",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(
            f"WARN: inspect failed for {image}: {proc.stderr.strip().splitlines()[-1:]}"
        )
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"WARN: unparseable inspect output for {image}")
        return None
    # Multi-arch images return {"linux/amd64": {..config..}, ...}; single-arch
    # images return the config object directly (has a "config" key).
    configs = [payload] if "config" in payload else list(payload.values())
    for cfg in configs:
        env = (cfg.get("config") or {}).get("Env") or []
        for entry in env:
            if entry.startswith("GOLANG_VERSION="):
                full = entry.split("=", 1)[1]
                m = re.match(r"(\d+\.\d+)", full)
                return m.group(1) if m else None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", required=True, type=Path)
    ap.add_argument("--ecr-prefix", required=True)
    ap.add_argument("--dockerfile-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    candidates = candidate_go_versions(args.dockerfile_dir)
    if not candidates:
        print("ERROR: no candidate Dockerfiles with FROM golang:X.Y found")
        return 1
    print(
        "Candidates:",
        ", ".join(f"go {v} -> {f}" for v, f in sorted(candidates.items())),
    )

    mapping: dict[str, str] = {}
    exceptions: list[tuple[int, str | None]] = []
    rows = read_dataset_prs(args.dataset)
    for i, (org, repo, pr) in enumerate(rows, 1):
        image = to_ecr_image(args.ecr_prefix, org, repo, pr)
        version = image_go_version(image)
        filename = candidates.get(version or "")
        if filename is None:
            exceptions.append((pr, version))
            print(
                f"[{i}/{len(rows)}] pr-{pr}: go={version} -> EXCEPTION (no matching Dockerfile)"
            )
        else:
            mapping[str(pr)] = filename
            print(f"[{i}/{len(rows)}] pr-{pr}: go={version} -> {filename}")

    args.out.write_text(
        json.dumps(dict(sorted(mapping.items(), key=lambda kv: int(kv[0]))), indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {len(mapping)} entries -> {args.out}")

    print("\n=== PR -> Dockerfile table (for TL confirmation) ===")
    by_file: dict[str, list[int]] = {}
    for pr, filename in mapping.items():
        by_file.setdefault(filename, []).append(int(pr))
    for filename in sorted(by_file):
        prs = sorted(by_file[filename])
        version = next(v for v, f in candidates.items() if f == filename)
        print(
            f"{filename} (golang:{version}): {len(prs)} PRs: {', '.join(map(str, prs))}"
        )
    if exceptions:
        print(
            f"\nEXCEPTIONS ({len(exceptions)}) — no matching Dockerfile, will fall back to template:"
        )
        for pr, version in exceptions:
            print(f"  pr-{pr}: GOLANG_VERSION={version}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
