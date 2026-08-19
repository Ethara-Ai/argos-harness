# Ops runbook — harness update (2026-08-19)

**TL;DR:** The "thinking tokens sometimes missing" issue is closed — capture is
verified working end to end (two instances re-run against known baselines, all
checks pass). The machine-dependent part was a real bug: bridge addresses only
worked on one platform. That is now fixed for **both proxies (Claude + Codex)
on both platforms (macOS + Linux/EC2)** — the run scripts adapt the address
automatically. Per-turn token variance on healthy runs is expected Anthropic
behavior (see `SCORE_MATH.md` §10) — do not file bugs for it.

---

## 1. One-time: update your checkout

```bash
cd <your>/argos-harness
git pull
make build                      # re-syncs deps + submodule if needed
rm -f .llm_config/*.mac.json    # delete any local config variants — no longer needed
```

That is the entire migration. **Do not edit any file in `.llm_config/` anymore**
— the scripts rewrite the bridge address for your platform at runtime, into a
temp config. The committed files stay canonical.

## 2. Before running on any machine (new machine, or after reboot)

**Step 1 — start the bridge(s) you need:**

```bash
# Claude runs (needs Claude Code login on Mac; creds file on EC2):
proxy/claude_code_bridge.sh start
proxy/claude_code_bridge.sh status     # want: bridge up, healthz ok

# Codex runs (needs ~/.codex/auth.json):
proxy/codex_bridge.sh start
proxy/codex_bridge.sh status
```

**Step 2 — 30-second capture check (replaces hour-long test runs):**

```bash
uv run python scripts/check_thinking_capture.py
```

| probe output | meaning | action |
|---|---|---|
| `PASS` | machine is good | proceed |
| `PASS (with caveat)` signature-only | normal model behavior | proceed |
| `FAIL` … bridge/base_url | bridge down or unreachable | redo step 1 |
| `FAIL` … never asks for thinking | wrong config/model | check `--llm-config` |

## 3. Running evals — same command on every platform

```bash
EGRESS_FILTER_DISABLE=1 RUBRIC_ENABLE=1 bash run_eval.sh \
    --llm-config .llm_config/claude-code.json \
    --dataset <dataset.jsonl> \
    --ecr-prefix <ecr-prefix> \
    --lang <lang>
```

Codex: swap in `--llm-config .llm_config/codex.json`. All other flags per the
pre-run checklist as before — and still **never** pass `--max-iter` below 1000
on scored runs.

Early in the log on a Mac you will see this line — it is expected and means the
auto-fix worked:

```
platform(Darwin): rewrote LLM base_url http://172.17.0.1:8765 -> http://host.docker.internal:8765
```

On EC2 with the Codex config, the mirror image:

```
platform(Linux): rewrote LLM base_url http://host.docker.internal:8766 -> http://172.17.0.1:8766
```

> **First EC2 Codex run after this update:** please confirm that line appears
> and report back — it is the one path not yet exercised on real hardware
> (it is covered by tests).

## 4. New failure you might see: `metrics-attribution`

If a harbor conversion stops with:

```
[converter] metrics-attribution: 0 of N action events matched token_usages ...
```

that run's token data is broken (the failure mode that once shipped corrupted
bundles). **This is the safety guard working — no flag was needed; it is on by
default.** What to do:

1. Do **not** ship that bundle.
2. Escalate / investigate the run. The inference results (`output.jsonl`,
   report) are intact — only packaging refused, so conversion can be re-run
   after diagnosis without re-running the agent.
3. Only if you *knowingly* need the bundle anyway (debugging, reprocessing an
   old broken run):

```bash
HARBOR_STRICT_METRICS=0 bash run_eval.sh ...   # downgrades the failure to a warning
```

Healthy runs are completely unaffected — the guard is silent on them and the
bundle format is byte-identical to before (test-enforced).

## 5. Stop doing (obsolete practices)

1. ❌ Hand-editing `base_url` in `claude-code.json` / `codex.json` per machine
   (the `_ec2_note` manual-swap instruction is dead).
2. ❌ Keeping local config variants like `claude-code.mac.json`.
3. ❌ Running full evals just to check whether thinking capture works — use the
   probe (`scripts/check_thinking_capture.py`).
4. ❌ Reporting "thinking tokens missing on some turns" as a bug. On a healthy
   Claude run, roughly 40–100% of turns carry reasoning text/tokens; 0 on a
   turn means the model returned a signature-only thinking block — expected.
   Reference: `SCORE_MATH.md` §10. Also never compare reasoning-token totals
   across models — Claude's are text-derived estimates, GPT's are
   provider-reported. They measure different things.
5. ❌ Killing agent-server builds that go silent — the `uv sync` copy phase can
   show no output for 15–20 minutes at 0% CPU. Judge by the buildx step's
   elapsed counter, not by log output.
6. ❌ (Mac users) chasing `tests/test_instance_timeout.py` failures — known
   macOS-only test limitation (process spawn exceeds the tests' timing budget);
   Linux CI is authoritative.

## 6. Quick reference

| I want to… | Command / reference |
|---|---|
| Check bridge is up | `proxy/claude_code_bridge.sh status` (or `codex_bridge.sh`) |
| Check capture on this machine | `uv run python scripts/check_thinking_capture.py` |
| Normal / delivery run | standard `run_eval.sh` line — no extra flags needed |
| Convert a broken run anyway (rare, deliberate) | prefix `HARBOR_STRICT_METRICS=0` |
| Token-number semantics | `SCORE_MATH.md` §10 |
| Full history of these changes | `CHANGE_LOG/METRICS_PROVENANCE.md` |

## 7. What changed under the hood (for the curious)

- `run_eval.sh` / `run_custom_eval.sh`: platform-aware bridge base_url rewrite
  (Darwin: `172.17.x.x → host.docker.internal`; Linux:
  `host.docker.internal → 172.17.0.1`), done into a temp config via the
  existing `mk_temp_llm_config` helper; plus macOS-portable `mktemp` templates.
  Pinned by `tests/test_platform_base_url.py` (18 tests, including functional
  execution of the real guard code for both platforms).
- Harbor converter: Class-B metrics guard, strict by default
  (`benchmarks/multiswebench/tests/test_converter_metrics_guard.py`).
- New probe: `scripts/check_thinking_capture.py`.
- Docs: `SCORE_MATH.md` §10 (reasoning-token provenance),
  `CHANGE_LOG/METRICS_PROVENANCE.md` (full investigation record).
