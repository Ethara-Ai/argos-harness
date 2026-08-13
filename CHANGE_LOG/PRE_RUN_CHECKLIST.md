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

> **2026-08-13: the `--delivery` intake automation was REMOVED before push**
> (TL revert directive — see `DOCKERFILE_SWAP.md` REVERTED chapter). Intake is
> manual again: the steps below ARE the procedure.

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
| ☐ `environment/Dockerfile` | **template render** (two-stage: python-fetch + `FROM <per-PR ECR image>` + repo name) — the swap was REVERTED 2026-08-13; input-file Dockerfiles appear ONLY in the 31 pre-revert dapr bundles (accepted mixed state) | revert of `e512241` |
| ☐ `tests/rubrics.json` | top-level keys exactly `{items, checks, sites}` — **no** schema_version/authored_by/task_uuid header | `4ba0172` |
| ☐ `verifier/process.json` | **`council` block** (n_judges/members/kappa…) + `composition` has `kappa`/`kappa_scope` — the single-judge change was REVERTED 2026-08-13; single-judge process = deterministic checks only (judge verdicts recorded, zero-weighted — known/accepted) | revert of `7514c58` |
| ☐ `verifier/final_score.md` | `Judges: n=1 (sonnet-5), κ=…` header; kappa-weighted formula row | revert of `7514c58` |
| ☐ `result.json` assay block | keys `{alpha, kappa, rubric_weight, gate, stratum_size, council, status}` | revert of `7514c58` |
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

## RUN JOURNAL — dapr batch, night of 2026-08-11 → 08-12 (Claude, unattended; NOT committed)

**Launch 1** (Anzar, 18:49): died at instance #6 — see incident below. **Launch 2** (Claude,
22:04, same command, background): resumed clean — 1351/1638 skipped 22:06, batch proceeding.

### Incident: mass FATAL "could not obtain base image" (20:37, 35 instances in 15 s)

- **Root cause: Docker disk exhaustion, NOT internet.** Docker usage had reached ~240 GB
  (165 GB images + 67 GB build cache + volumes); every ECR pull failed instantly. Proof it
  wasn't the network: after cleanup, the same `docker pull` of pr-3923 succeeded on the same
  connection. Each instance leaves ~5 GB of per-PR images (ECR tag + mswebench tag +
  agent-server build + eval images) and run_eval.sh has **no per-instance image cleanup**
  (verified by grep) — a 41-instance batch inevitably fills a laptop.
- **Fixes applied (no repo changes):** (1) `docker builder prune -af` + container/dangling
  prune + removed per-PR images of completed instances + tortoise leftovers → 240 GB → 55 GB;
  (2) background **image janitor** (`/tmp/dapr_image_janitor.sh`, PID in
  `/tmp/dapr_image_janitor.log`): every 5 min deletes per-PR images of instances whose log
  shows `done status=` (keeps `:base` and shared images; covers dapr + XTLS log patterns).
- **☐ FOLLOW-UP (checklist addition): add per-instance image cleanup to run_eval.sh** (or
  document the janitor as a required sidecar) before anyone runs a 40+ batch on a laptop.
  Server with big disk: less urgent, same leak.

### Incident 2: second mass FATAL wave (10:57, 18 instances in 11 s) — ECR TOKEN EXPIRY

- Launch 2 ran 22:04 → 10:57 (~13 h). **run_eval.sh does ECR `docker login` ONCE at startup;
  the token lives ~12 h.** Every instance whose image pull started after the ~12 h mark
  failed instantly ("could not obtain base image"). Proof: manual pull of pr-7662 failed at
  11:00, succeeded immediately after a fresh `aws ecr get-login-password | docker login`.
  Disk was fine (114 GB) — different root cause than Incident 1.
- **Fix applied (no repo change): `/tmp/ecr_relogin.sh` sidecar** — refreshes the docker
  login every 6 h for the remainder of the batches (log: `/tmp/ecr_relogin.log`).
- **☐ FOLLOW-UP (real harness bug): run_eval.sh must re-login to ECR periodically or
  per-instance (or retry the pull once after a fresh login) — any batch longer than 12 h
  dies at the token boundary.** This will hit the QL's server runs too (41 dapr ≈ 20+ h).
- Note: launch 2's exit summary listed 19 "image-fail" — that count includes stale
  results-file entries from launch 1; the true launch-2 wave was 18 instances (7662…9718).

### Per-instance results (updated as the night progresses)

| # | PR | Trajectory | Eval | Authoring/scoring | Notes |
|---|---|---|---|---|---|
| 1 | 1351 | reused | ✓ | ✓ scored (header-strip applied — pre-4ba0172-authored, both copies fixed) | bundle `edd779ae` — all §E format checks PASS |
| 2 | 1638 | reused | ✓ | ✓ scored, natively new-format | bundle `6816e922` — all §E checks PASS |
| 3 | 1749 | ✓ fresh, **15 thinking events** | ✓ | ✓ scored on resume pass 22:13 (rollback→auto-retry worked; det 0.154 / rub 0.409 / process 0.281) | transient confirmed one-off |
| 4 | 1796 | ✓ fresh | ✓ | ✓ scored on resume pass 22:22 (fresh redraft passed the anchor gate) | first-pass rejection was the quality gate doing its job |
| 5 | 2873 | ✓ fresh | ✓ | ✓ scored | `rubric: ok`; score_eval 0.6526 (partial outcome) |
| 6 | 3682 | ✓ fresh (26 thinking ev) | ✓ | ✓ scored first-pass | 32-min cycle |
| 7 | 3887 | ✓ fresh | ✓ | ✓ scored first-pass | disk steady 74 GB (janitor working) |
| 8 | 3923 | ✓ fresh | ✓ **RESOLVED 1/1** | ✓ scored | **first outcome-success of the batch** 🎉 |
| 9 | 4452 | ✓ fresh | ✓ | ✓ scored first-pass | |
| 10 | 4519 | ✓ fresh (257 KB, intact) | ✗ `no-report` — eval's `git clone dapr` hit a NETWORK blip 00:22–00:32 (GitHub 200 again by 00:40) | skipped by the 6735f3d gate (correct) | **re-run eval at end of batch** — a final resume pass re-evals it (trajectory reused). This one WAS internet, unlike the 20:37 disk incident. |
| 11 | 5582 | ✓ | ✓ | ✓ scored | 57-min cycle (heavy build) |
| 12 | 5992 | ✓ | ✓ | ✓ scored | large image set (26 GB transient — janitor verified working; disk peak ~100 GB vs 240 ceiling, no leak) |
| 13 | 6039 | ✓ | ✓ | ✓ scored | |
| 14 | 6086 | ✓ | ✓ | ✓ scored | bridge probed healthy 03:47 |
| 15 | 6284 | ✓ | ✓ | ✓ scored | |
| 16 | 6329 | ✓ | ✓ **RESOLVED 1/1** (2nd 🎉) | ⚠ authored ✓ but judge verdict for `R3` missing (transient judge call) → score flagged incomplete (exit 1) | **resume pass re-judges the missing item** |
| 17 | 6431 | ✓ | ✓ | ✓ scored | |
| 18 | 6452 | ✓ | ✓ | ✓ scored | |
| 19 | 6598 | ✓ | ✓ | ✓ scored | |
| 20 | 6934 | ✓ | ✓ | ⚠ authored ✓, judge verdict `G6` missing (same transient as #16) | resume pass re-judges |

**§E checklist: ALL CHECKS PASS (verified on real bundles, 22:10):** Dockerfile ✓ ·
harbor.log line ✓ · headerless rubrics ✓ · judge block/weight ✓ · thinking ✓ (1749: 15
events) · staging flat ✓ · cost non-zero ✓ (2873: `agent_result.cost_usd = 1.079` — litellm
fix proven live) · n_episodes vs ceiling ✓ (2873: 64 vs 1000) · final_score.md ✓
("Judge: sonnet-5", "process = (det + 1·rubric) / 2"). 2873 scored `score_eval 0.6526`
(partial outcome credit + process 0.496 — real signal, not the flat-zero pattern).

### Alignment review: checklist vs what was actually done (2026-08-11 ~22:05)

- §A push — **DONE** (Anzar pushed `6735f3d..4c4c105`, 8 commits; checklist said 5 —
  three more landed after writing: `aca9a08`, `4c4c105` scope fix, `2aa0084`).
- §A TL map confirmation — **PENDING** (draft message given to Anzar).
- §B XTLS jsonl → dataset/ — **DONE** (md5-verified copy).
- §C bridge — up + healthy all evening (probed before relaunch).
- §D flags — launch used exactly the §D command (no --max-iter, correct config/lang/data-dir).
- §F server-side — untouched tonight (QL's side; needs pull + uv fix).
- Deviation from §G: "existing 1351 bundle keeps old Dockerfile" is now stale — the batch
  re-exported 1351 with the new Dockerfile + judge block, and its rubrics.json was
  header-stripped manually. The whole dapr delivery is now format-uniform.

### Overnight plan

dapr 36 remaining ≈ 15–18 h (may extend past morning); if the batch process exits before
morning AND all instances are terminal, launch XTLS (`dataset/finalXTLS_shippable.jsonl`,
same flags) — janitor already covers it. Any new WARN/FATAL gets journaled here. Nothing
gets committed.

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

## RUN JOURNAL — 2026-08-12 afternoon: delivery-intake automation (Claude; NOT committed until suite green)

User-approved plan executed: `run_eval.sh --delivery <folder>` automation.

- **Built**: `intake_delivery.py` (strict flat intake, per-repo FAIL isolation),
  generalized `derive_env_dockerfile_map.py` (go/python/java two-tier signatures,
  atomic map writes — exit-2 no longer leaves a partial map), run_eval.sh
  `--delivery/--intake-dry-run/--allow-template-fallback` wiring with path-scoped
  auto commit+push of env_dockerfiles/, 33 new tests, docs
  (`DELIVERY_INTAKE.md`, env_dockerfiles README, §B pointer above).
- **Layer 2 (real 14-repo delivery, dry run)**: **7 READY / 7 FAILED** — plan
  expected 8/6; delta = 3 genuine DATA defects the strict validator caught:
  kubevela jsonl malformed JSON (line 65), future-architect + reflex-dev records
  missing `uuid`. Other FAILs: crawlee/apollo/dbeaver/changedetection.io not
  flat / bad Dockerfile names. **XTLS known-answer proof passed: 38/38 PRs
  re-derived from ECR, committed map verified byte-identical.**
  → TL needs: re-upload 4 non-flat repos + fix 3 defective jsonls (+ ship
  future-architect's missing Dockerfile).
- **Layer 3**: dapr-1351 re-converted via old `--dataset` path →
  `environment/Dockerfile` byte-identical to library ✓ (no behavior change).
- **Layer 1**: full pytest suite — running, result appended below.
- **Layer 4 smokes (go-zero/ytdl/radare2, --n-limit 1)**: queued behind the
  dapr pass-3 batch still running (PID 50036).
- dapr batch untouched throughout (no restarts; no shared-state writes).

### 2026-08-12 ~17:50 — batch STOPPED intentionally (user: "major change" incoming)
dapr pass-3 killed cleanly at 31/41 bundles (process group TERM; leftover pr-9206
agent container stopped; kensei containers untouched). Language smokes NOT started;
XTLS NOT launched. Resume later = same launch command (completed instances skip).

### 2026-08-13 — Dockerfile swap + delivery intake REVERTED (TL directive)
Full removal executed per approved plan: converter back to template-only render
(byte-verified vs pre-change golden), env_dockerfiles/ + derive script + swap
tests deleted, intake commit bddc34b dropped pre-push, litellm crash-fix kept.
31 pre-revert dapr bundles keep input Dockerfiles (user decision). Details:
DOCKERFILE_SWAP.md REVERTED chapter. Batch remains stopped at 31/41; resume =
same command (post-resume exports will render template Dockerfiles).

### 2026-08-13 — kappa-removal (7514c58) REVERTED (TL directive) + old outputs discarded
Full git revert executed; assay/ + fixtures + replay/structure tests byte-identical
to pre-7514c58 (verified). Consequence accepted: single-judge process = det checks
only, judge verdicts zero-weighted; corpus-format parity restored (council/kappa).
SCORE_MATH.md deleted with the revert. ALL trajectories to be regenerated fresh —
eval_outputs dapr dirs, milo_bundles, and our staged (untracked) uuid dirs in the
publish clone wiped per user decision. Details: REWARD_CHANGE_LOG.md REVERTED chapter.
