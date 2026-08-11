# env_dockerfiles — input task-folder Dockerfiles shipped in bundles

Per TL directive (2026-08-11), the harbor converter ships the **input task-folder
Dockerfile** as each bundle's `environment/Dockerfile` instead of rendering the
`task-template/environment/Dockerfile` template. The template remains the live
fallback for repos with no entry here. Full rationale, accepted risks and revert
steps: [CHANGE_LOG/DOCKERFILE_SWAP.md](../../../../../CHANGE_LOG/DOCKERFILE_SWAP.md).

## Layout convention

One subdirectory per repo, named with the image convention
`{org}_m_{repo}` (lowercased) — the same formula `to_ecr_image()` uses:

```
env_dockerfiles/
├── dapr_m_dapr/
│   └── Dockerfile.base            # single file → used for every PR
└── xtls_m_xray-core/
    ├── Dockerfile                 # golang:1.17 era
    ├── Dockerfile1                # golang:1.20 era
    ├── Dockerfile2                # golang:1.24 era
    ├── Dockerfile3                # golang:1.26 era
    └── map.json                   # {"<pr_number>": "<filename>", ...}
```

Resolution (`converter.resolve_env_dockerfile`):
- no subdir for the repo → template fallback (loud WARN in harbor.log);
- exactly one `Dockerfile*` file → that file, for every PR;
- multiple `Dockerfile*` files → `map.json` must map `str(pr_number)` to a
  filename; missing map or missing key → template fallback (loud WARN).
- `ENV_DOCKERFILE_SOURCE=template` env var → force the old template behavior
  everywhere (instant revert switch).

The chosen file is copied **verbatim** — no placeholder substitution, no
language patches.

## Adding a new repo batch

1. Copy the Dockerfile(s) from the task folder the TL provides into
   `env_dockerfiles/{org}_m_{repo}/` (keep original filenames; bytes verbatim).
2. Multiple Dockerfiles? Generate the per-PR map from the already-built ECR
   images (ground truth — reads each image's `GOLANG_VERSION`):

   ```bash
   uv run python benchmarks/multiswebench/scripts/harbor/derive_env_dockerfile_map.py \
       --dataset <batch>.jsonl \
       --ecr-prefix 426628337772.dkr.ecr.ap-south-1.amazonaws.com/rfp-coding-q1-tag-milo \
       --dockerfile-dir benchmarks/multiswebench/scripts/harbor/env_dockerfiles/{org}_m_{repo} \
       --out benchmarks/multiswebench/scripts/harbor/env_dockerfiles/{org}_m_{repo}/map.json
   ```

   Requires ECR docker login. Any PR the script cannot match is printed as an
   EXCEPTION, excluded from the map (falls back to template), and should go
   back to the TL.
3. Commit + push, and make sure the server-side runner pulls before its run.
