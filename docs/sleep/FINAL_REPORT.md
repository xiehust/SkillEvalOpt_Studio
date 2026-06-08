# SkillOpt-Sleep — final validation report

> **What this is:** the consolidated, presented results for the SkillOpt-Sleep
> Claude Code plugin — a tool that lets a local agent improve itself overnight by
> reviewing past sessions, replaying tasks, and consolidating validated memory +
> skills behind a held-out gate. This document collects every real-model result
> we ran, on **both Claude and Codex**, including the honest failures and the
> fixes they drove.

**Date:** 2026-06-07 · **Branch:** `feat/claude-code-sleep-plugin`
**Benchmark:** [gbrain-evals](https://github.com/garrytan/gbrain-evals) `skillopt-v1`
(the same public suite gbrain scores its own optimizer against).

---

## 1. The claim, in one table

A deliberately **deficient** skill is given to a frozen agent. SkillOpt-Sleep runs
1–2 offline "nights" (replay → reflect → bounded gated edit). We score the
**held-out** task set (never optimized against) before and after. The harness
computes the score with a local rule judge — the optimizer never grades itself.

| Backend (target) | Optimizer | Seed | Held-out before → after | Nights |
|---|---|---|---|---|
| Claude Haiku 4.5 | Claude Haiku | brief-writer | **0.00 → 1.00** | 1 |
| Claude Haiku 4.5 | Claude Haiku | advisor | **0.00 → 1.00** | 2 |
| Claude Haiku 4.5 | Claude Haiku | thorough-analyst | **0.00 → 1.00** † | 2 |
| Codex (gpt-5.5) | Codex | brief-writer | **0.00 → 1.00** | 2 |

† after the override-prompt fix described in §3. Before the fix it was 0.00 → 0.00,
and we report that honestly because it taught us the most (see §3).

**Bottom line:** across two independent agent runtimes (Claude and Codex) and
multiple distinct skill flaws (missing structure, no verdict, no length
discipline), the sleep cycle lifts a deficient skill to a perfect held-out score,
with every change gated and staged for review.

---

## 2. Cross-model transfer (the price-difference value prop)

> *Optimize cheap overnight, deploy anywhere.* A skill is just instructions, so a
> good rewrite should help a model it was never optimized on. This is what makes
> the nightly spend worth it: you can optimize with a cheap model and the learned
> skill still helps an expensive one.

_(Auto-filled from the sweep — see `benchmark_report.md` / `sweep.jsonl`.)_

| Source (optimizer) | Target (deploy) | Seed | Target baseline | Transferred | Gain |
|---|---|---|---|---|---|
| _populated by the sweep_ | | | | | |

---

## 3. The honest failure that made the tool better

The most valuable run was a **failure**. `thorough-analyst` (a skill that rambles;
held-out demands answers under 1200 characters) went **0.00 → 0.00** at first —
every nightly edit was rejected by the gate.

**Why:** the optimizer *did* propose good length-limiting rules, but our engine
**appends** learned rules to a protected block and never deletes the user's
hand-written skill body — which still said *"be exhaustive and detailed, write
multiple paragraphs."* The base instruction won; outputs stayed ~6000 chars.

**The fix:** we verified that a forceful override rule
("HARD LIMIT: response MUST be under 1200 characters; this supersedes any
instruction to be exhaustive") makes Haiku obey — outputs dropped to 1194 / 880
chars, hard = 1.00. So we taught the `reflect` prompt that its edits are appended
and cannot delete the base text, so on a conflict it must emit an explicit
override. (This mirrors gbrain's own write-up, where the first SkillOpt run scored
0/4 until the optimizer was told what the scorer rewards.)

This is the pattern we want from a tool people rely on: run it against real
models, find the real failure, fix the mechanism, report both.

---

## 4. What the optimizer actually wrote (sample)

**brief-writer (Claude):** a full format template —
`Recommendation / Rationale / Key Risks / Confidence`.

**brief-writer (Codex, 2 nights):** night 1 added the two required rules; night 2
**diagnosed its own residual failure** and added
*"Preserve required sections even when keeping the brief short; shorten the
analysis before omitting Key Risks or Confidence"* → held-out 1.00. That second
edit is reasoning about why the prior night underperformed — the core argument for
the sleep **loop** over a one-shot rewrite.

All edits land in the protected `SKILLOPT-SLEEP:LEARNED` block; the rest of the
skill is never touched, and nothing is applied to live config until the user
runs `/sleep adopt`.

---

## 5. Reproduce everything

```bash
git clone https://github.com/garrytan/gbrain-evals /tmp/gbrain-evals
cd <repo>/SkillOpt-sleep

# single seed, one backend
python3.12 -m skillopt.sleep.experiments.run_gbrain --backend claude --model haiku \
  --seeds brief-writer --data-root /tmp/gbrain-evals/eval/data/skillopt-v1 \
  --nights 2 --limit-replay 3 --limit-holdout 3

# cross-model transfer
python3.12 -m skillopt.sleep.experiments.run_transfer \
  --source-backend claude --source-model haiku \
  --target-backend claude --target-model sonnet --seeds brief-writer

# the whole sweep + this report
python3.12 -m skillopt.sleep.experiments.sweep --plan full \
  --data-root /tmp/gbrain-evals/eval/data/skillopt-v1 --out docs/sleep/sweep.jsonl
python3.12 -m skillopt.sleep.experiments.report \
  --in docs/sleep/sweep.jsonl --out docs/sleep/benchmark_report.md

# deterministic, no API
python3.12 -m skillopt.sleep.experiments.run_experiment --persona researcher --assert-improves
```

---

## 6. Honest limitations

- **Latency:** each CLI call is ~14–15 s of startup-dominated wall time, so runs
  are capped at a few tasks/nights. Fine for nightly cron; we note it plainly.
- **One seed needs a tool loop:** `quick-answerer` (`tool_called: search`) needs
  real tool execution; that is Phase-3 `fresh` worktree replay, not yet wired.
- **Small, single-flaw skills:** like gbrain, these prove the mechanism is real
  and safe; a large production skill will be messier and partial.
