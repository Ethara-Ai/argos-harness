# Pre-run checklist — dapr (41) + XTLS (38) delivery batches

*Written 2026-08-11 against HEAD `613b6bc`. Covers every commit since `9f09bb3` (2026-08-10):
hardening → dynamic rubric models → flat staging → Codex bridge/routing → harbor-tail gate →
**Dockerfile swap** → headerless rubrics.json → kappa removal → CHANGE_LOG move.*

Legend: ☑ = verified already (2026-08-11, this machine) · ☐ = check at run time.

## A. One-time, before any run

- ☐ **Push the 5 unpushed commits** (`e512241`, `1f7393d`, `4ba0172`, `7514c58`, `613b6bc`)
  — work-account credential (abhijeet-ethara PAT). Until pushed, server runs use old behavior.
- ☐ **TL confirms the XTLS PR→Dockerfile table** (`CHANGE_LOG/DOCKERFILE_SWAP.md`).
  Non-blocking for runs — affects only which env Dockerfile ships per bundle.
- ☑ **Full test suite green on HEAD `613b6bc`**: 694 passed; only the 11 known pre-existing
  failures (egress-wiring ×7, instance-timeout ×2, security-fixes ×2 — all fail on clean
  HEAD too, unrelated to any of these changes).
- ☑ **Dockerfile swap smoke-proven live**: converter on real dapr-1351 input →
  `environment/Dockerfile` byte-identical to `Dockerfile.base`; `ENV_DOCKERFILE_SOURCE=template`
  reverts to template render.

## B. Dataset health (per batch)

- ☑ **dapr jsonl**: `dataset/dapr__dapr_41pr_combined_final.jsonl` is md5-identical to the
  task-folder copy (`~/Downloads/dapr_dapr 2/`). 41 records previously verified (uuid +
  real test_patch_result on all; number_interval matches registry byte-for-byte; chain
  proven E2E on pr-1351).
- ☐ **XTLS jsonl**: copy `~/Downloads/XTLS_Xray-core/finalXTLS_shippable.jsonl` → `dataset/`.
- ☑ **XTLS record health (pre-verified on the Downloads copy)**: 38 records; all have `uuid`;
  **zero empty `test_patch_result`** (no 226/76-style authoring-blocker gap); all 38
  `org/number_interval` values match the `multi_swe_bench` evaluator registry **byte-for-byte**
  (42 XTLS interval classes registered) — no "instance not registered" risk.
- ☑ **ECR images**: 41 dapr + 38 XTLS tags verified in the task folders' logs (amd64+arm64);
  spot-re-verified during map derivation. run_eval.sh re-logins to ECR itself.

## C. Bridge + account (trajectory = Claude via OAuth bridge)

- ☐ `proxy/claude_code_bridge.sh status` (start if needed). **If the Claude account was
  switched since the bridge started: `stop && start`** (bridge caches the first-loaded
  account and can write the old one back).
- ☐ 1-token probe against `/v1/messages` before a long batch (catches subscription caps —
  capped runs emit 0-byte trajectories marked done; Ctrl-C immediately if seen).
- ☐ (Only for a Codex-trajectory experiment: `proxy/codex_bridge.sh start`, config
  `.llm_config/codex.json`. Author+judge stay pinned Claude regardless.)

## D. Launch flags (macOS)

```bash
EGRESS_FILTER_DISABLE=1 RUBRIC_ENABLE=1 bash run_eval.sh \
  --llm-config .llm_config/claude-code.json \
  --dataset dataset/<batch>.jsonl \
  --ecr-prefix 426628337772.dkr.ecr.ap-south-1.amazonaws.com/rfp-coding-q1-tag-milo \
  --lang go --no-push --data-dir ../milo-bench-dataset
```

- ☐ **NO `--max-iter`** (default 1000). Any lower ceiling and assay's B4 gate voids ~every run.
- ☐ `--lang go` for BOTH batches (dapr and Xray-core are Go).
- ☐ `--no-push --data-dir` points at the local clone of `EtharaOrion/milo-bench-samples`
  (origin URL must match). Publishing stays manual from the clone — the three `if false`
  sentinels in run_eval.sh guarantee nothing auto-pushes.
- ☐ `.llm_config/claude-code.json` (NOT `proxy/claude-code-oauth.json`) — it carries
  `force_adaptive_thinking: true`, required for thinking capture.

## E. First-bundle verification (run ONE instance, inspect, then unleash the batch)

Inspect `milo_bundles/<uuid>/` of the first finished instance:

| Check | Expect | Comes from |
|---|---|---|
| ☐ `environment/Dockerfile` | **byte-identical to the input file**: dapr → `env_dockerfiles/dapr_m_dapr/Dockerfile.base`; XTLS → the `map.json` file for that PR (`cmp` it) | `e512241` |
| ☐ `harbor.log` | `env-dockerfile: using input Dockerfile <repo>/<file>` (a `WARN … falling back` line = asset lookup failed) | `e512241` |
| ☐ `tests/rubrics.json` | top-level keys exactly `{items, checks, sites}` — **no** schema_version/authored_by/task_uuid header | `4ba0172` |
| ☐ `verifier/process.json` | `judge: {model: claude-sonnet-5}` block (NOT `council`); `composition.rubric_weight: 1.0`; **no kappa anywhere** | `7514c58` |
| ☐ `verifier/final_score.md` | `Judge: claude-sonnet-5` line; process = (det + rubric)/2 | `7514c58` |
| ☐ `result.json` assay block | keys `{alpha, rubric_weight, gate, stratum_size, judge, status}` | `7514c58` |
| ☐ `trajectories/<alias>/run_1/` | alias form (`opus-4.8`), `agent/trajectory.json` present, **thinking/reasoning content non-empty** in the raw output.jsonl | force_adaptive_thinking |
| ☐ result.json `n_episodes` vs `max_turns` | max_turns = 1000; run well under ceiling (B4 gate open unless genuinely cut off) | MAX_ITER default |
| ☐ result.json cost | **non-zero** even though the bridge bills 0 — `derive_cost_from_tokens` now works (latent NameError fixed in `e512241`) | `e512241` |
| ☐ staging | bundle copied FLAT to `<data-dir>/<uuid>/`; `verdicts/` NOT staged | `bf226c3` |
| ☐ status | authored once (`"mode": "judged"` in rubrics.json), scored (`status=scored` unless legitimately voided) | pipeline |

Also: `6735f3d` gates the whole harbor/rubric tail on eval evidence — an infer-only or
eval-less run produces NO `_harbor`/bundle at all (by design, not a bug).

## F. Server-side (QL) — only if the run happens on EC2

- ☐ `git pull` AFTER the push (repo there is behind; without it: template Dockerfiles,
  old rubrics.json format, kappa-era scoring).
- ☐ **Fix `uv` on PATH in the non-interactive shell** — known pre-existing blocker:
  run_eval.sh dies at startup ("environment is in an inconsistent state",
  `command not found: uv`). Activate the venv / add uv to PATH for the QL's shell.
- ☐ `.llm_config/claude-code.json` on the server: `base_url` **`http://172.17.0.1:8765`**
  (fixed 2026-08-10; keep — NOT host.docker.internal, NOT 0.0.0.0), and the bridge running
  on the server with a valid login.
- ☐ NO `EGRESS_FILTER_DISABLE` needed on Linux.
- ☐ Server `vendor/software-agent-sdk` submodule sync after pull (`git submodule update`).

## G. Known non-blockers (don't chase these)

- The 11 pre-existing test failures (list in §A) — unrelated, fail on clean HEAD.
- Existing dapr-1351 bundle `edd779ae…` keeps its old template Dockerfile (deliberate:
  old bundles not regenerated).
- `milo-bench-samples` corpus bundles differ from ours in Dockerfile shape + rubrics.json
  header + council block — all three are deliberate TL-directed divergences
  (see `DOCKERFILE_SWAP.md`, `REWARD_CHANGE_LOG.md`, commit `4ba0172`).
- Timing expectation: ~20 min/instance full chain → 41 dapr ≈ 10–14 h, 38 XTLS similar;
  caps interrupt cleanly — resume by feeding leftover per-instance files from
  `eval_outputs/_split/`.
