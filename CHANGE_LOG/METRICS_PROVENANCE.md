# Reasoning-token provenance and the Aug-4 metrics artifacts

Status: **documented + guarded** (2026-08-18). No score is affected — reasoning-token
fields are display-only metadata; `score_v2g` never reads them and assay's only
touchpoint is the presence-only `D1-reasoning-present` check.

## Why this document exists

The reported symptom was "thinking tokens appear on some runs/turns and not others",
with the recent bandit commits or a wrong config suspected. A full investigation
(2026-08-18, verified end to end against aurora-harness baselines; see
`THINKING_CAPTURE_JOURNAL.md` while it exists — it is untracked scratch) found **no
thinking-capture bug in the current tree**. The partial coverage is the composition of
two upstream facts, plus two dated artifact classes in the delivered corpus.

## The two upstream facts (expected behavior, not defects)

1. **Anthropic returns thinking text only when it chooses to.** With adaptive thinking
   (`force_adaptive_thinking` + `display:"summarized"`, `chat_options.py:103`), some
   turns come back as signature-only blocks — `{"thinking":"","signature":"…"}` — with
   no text. The fraction varies per run (observed 39–100% of turns with text on the
   same instance/config).
2. **Claude reasoning tokens in this pipeline are a local estimate.** litellm
   computes `token_counter(text=reasoning_content)` (`transformation.py:1757`), so a
   signature-only turn contributes exactly 0. Correction (2026-08-19, verified
   against platform.claude.com "Steering thinking" → Pricing): Anthropic *does*
   report a provider-side count — `usage.output_tokens_details.thinking_tokens`,
   the raw internal reasoning, present regardless of the `display` setting — but
   the pinned litellm fork (`a276b06a`) never reads that field (grep confirms no
   response-side `thinking_tokens`/`output_tokens_details` handling in its
   anthropic transformation). A future litellm bump or fork patch that maps
   `thinking_tokens` → `reasoning_tokens` would surface true provider counts,
   including on signature-only turns. Until then, the OpenAI path is
   provider-reported (`output_tokens_details.reasoning_tokens`, `telemetry.py:228`)
   while the Claude path is a text estimate. **Cross-provider comparisons of
   reasoning tokens are therefore not apples-to-apples.** See `SCORE_MATH.md` §10
   for the analyst rules.

## The two dated artifact classes (13 of 270 delivered trajectories, batch `189b37e`, 2026-08-04)

- **Class A — thinking never requested (10 files).** All `opus-4.8/run_1`. Zero
  `reasoning_content` on every step, no `reasoning_tokens` key anywhere, healthy
  prompt/completion/cache counts. run_2/run_3 of the same bundles are normal (e.g.
  `f874a27e`: run_1 missing, run_2 = 4,012, run_3 = 1,580) — consistent with run_1s
  predating the `force_adaptive_thinking` wiring.
- **Class B — metrics attribution lost (3 files, all bundle `0ae03990`).** Every step
  reads `prompt_tokens: 0, completion_tokens: 0, cached_tokens: 0`; the whole
  `token_usages` → `llm_response_id` join failed upstream. Root cause not confirmed
  (snapshot serialization is the candidate: `MetricsSnapshot` excludes the per-call
  lists, `metrics.py:76`).

Neither class can recur silently:

- The converter has written `final_metrics.extra.total_reasoning_tokens`
  **unconditionally** since the current tree — a broken run shows `0`, never a missing
  key (`converter.py`, `build_trajectory`).
- **New (2026-08-18, polarity flipped 2026-08-19): Class-B guard.**
  `build_atif_trajectory` detects the signature (a run has ActionEvents but zero of
  them join to `token_usages`) and is **strict by default**: the conversion fails
  with `[converter] metrics-attribution: …` so a corrupted-but-plausible bundle can
  never ship silently. `HARBOR_STRICT_METRICS=0` downgrades it to a warning — the
  explicit opt-out for debugging or deliberate reprocessing of known-broken
  historical runs. (Initially shipped warn-by-default with `=1` opt-in; flipped the
  next day because "someone forgot the flag" is precisely the failure mode being
  guarded against.) Partial join coverage stays silent in any mode — that is
  expected upstream behavior (fact 1). Pinned by
  `benchmarks/multiswebench/tests/test_converter_metrics_guard.py`.

## Ruled out during the investigation (with evidence)

Bandit commits `e1066fd`/`f88219d` (comment/config only), the `.llm_config` files
(byte-identical to aurora), the litellm pin (`a276b06a` in both), the SDK bump
`3ab822f5→5fd2444a` (2 files, egress only), and the converter/bridges/`atif.py`
(byte-identical). The only functional difference between the repos is multiswebench's
default prompt (`long_horizon.j2`, commit `0f1a5b4`).

## Related fix landed alongside

`run_eval.sh` / `run_custom_eval.sh` now rewrite bridge base_urls per platform
(temp config via `mk_temp_llm_config`; pinned by `tests/test_platform_base_url.py`):
on Darwin `http://172.17.x.x` → `host.docker.internal` (docker0 does not exist on
Docker Desktop), and on Linux `http://host.docker.internal` → `172.17.0.1` (that
name does not resolve in plain Linux Docker; the harness's agent containers run on
the default bridge network with no `--add-host`). Both directions were
total-failure footguns (empty trajectories) depending on which platform a config
was committed for — `claude-code.json` was Linux-style, `codex.json` Mac-style.
Found during the same investigation; could never cause *partial* thinking coverage.

## How to revert

- Class-B guard: delete the `action_rids` block in `build_atif_trajectory`
  (`converter.py`, directly above the `latency_by_response` loop) and
  `test_converter_metrics_guard.py`.
- Platform rewrite: delete the `_os="$(uname -s)"` guard block above
  `RUNTIME_LLM_CONFIG="$LLM_CONFIG"` in both scripts, the `PLATFORM_TEMP_CFG` line
  in `_cleanup_compression`, and `tests/test_platform_base_url.py`.
- This document and `SCORE_MATH.md` §10 are documentation only.
