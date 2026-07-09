"""SkillEval LLM judge — rubric-based verdicts on agent responses.

Each task carries its own ``rubric``; the judge model (routed through
``chat_optimizer`` so it shares the optimizer backend configuration) reads
the task, the agent's response, and a listing of files produced in the
work_dir, then returns a strict JSON verdict.  Parsing is tolerant, retries
once on malformed output, and never raises: unjudgeable results surface as
``judge_error`` so the report can list them explicitly.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable

from skillopt.model import chat_optimizer

JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluator for agent task outputs. You are given a task, "
    "an acceptance rubric, the agent's final response, and a listing of files "
    "the agent produced (with the contents of produced text files, possibly "
    "truncated). Judge ONLY against the rubric, and only credit criteria the "
    "provided evidence verifies. Reply with ONLY a JSON "
    'object, no prose: {"pass": true|false, "score": <float 0.0-1.0>, '
    '"reason": "<short justification>"}. "pass" means the rubric is fully '
    'satisfied; "score" is partial credit toward the rubric.'
)

_RETRY_SUFFIX = (
    "\n\nYour previous reply was not valid JSON. Reply again with ONLY the "
    'JSON object {"pass": bool, "score": float, "reason": str}.'
)


def _find_balanced_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring of *text*, if any."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start: pos + 1]
        start = text.find("{", start + 1)
    return None


def _extract_verdict(text: str) -> dict | None:
    """Tolerantly parse a judge verdict out of *text*.

    Tries raw JSON, then fence-stripped JSON, then the first balanced
    ``{...}`` block.  Returns ``None`` when no verdict with a boolean-able
    ``pass`` and numeric ``score`` can be recovered.
    """
    candidates = []
    stripped = (text or "").strip()
    if stripped:
        candidates.append(stripped)
        if stripped.startswith("```"):
            defenced = stripped.strip("`")
            if defenced.lower().startswith("json"):
                defenced = defenced[4:]
            candidates.append(defenced.strip())
        balanced = _find_balanced_object(stripped)
        if balanced:
            candidates.append(balanced)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "pass" not in data:
            continue
        try:
            score = float(data.get("score"))
        except (TypeError, ValueError):
            continue
        return {
            "pass": bool(data["pass"]),
            "score": min(1.0, max(0.0, score)),
            "reason": str(data.get("reason") or ""),
        }
    return None


def _build_judge_user_prompt(item: dict, response: str, artifacts_listing: str) -> str:
    return "\n\n".join([
        f"## Task\n{item['question']}",
        f"## Acceptance rubric\n{item['rubric']}",
        f"## Agent response\n{response}",
        f"## Files produced in the workspace\n{artifacts_listing or '(none)'}",
    ])


def artifacts_listing(work_dir: str) -> str:
    """List files the agent left in *work_dir* (relative path + size).

    Harness-internal files (hidden dirs, the task prompt) are skipped so the
    judge only sees artifacts the agent actually produced.
    """
    if not work_dir or not os.path.isdir(work_dir):
        return ""
    lines = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, work_dir)
            if rel.startswith((".agents", ".claude")) or rel == "task.md":
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            lines.append(f"{rel} ({size} bytes)")
    return "\n".join(lines)


def artifacts_excerpts(
    work_dir: str,
    exclude_rel: Iterable[str] = (),
    *,
    per_file_chars: int = 2000,
    max_files: int = 8,
    max_total_chars: int = 10000,
) -> str:
    """Contents of agent-produced text files, for the judge's evidence.

    A rubric usually constrains what the agent *writes*, not just that a file
    exists — a judge that only sees a name/size listing cannot verify those
    criteria and will (correctly) refuse to credit them. Walks *work_dir* with
    the same skips as ``artifacts_listing``, additionally excluding
    *exclude_rel* (task-seeded input files) and binary files. Truncation is
    always marked, never silent.
    """
    if not work_dir or not os.path.isdir(work_dir):
        return ""
    excluded = {os.path.normpath(str(rel)) for rel in (exclude_rel or ())}
    blocks: list[str] = []
    total = 0
    skipped = 0
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, work_dir)
            if rel.startswith((".agents", ".claude")) or rel == "task.md":
                continue
            if os.path.normpath(rel) in excluded:
                continue
            if len(blocks) >= max_files or total >= max_total_chars:
                skipped += 1
                continue
            try:
                with open(full, "rb") as f:
                    raw = f.read(per_file_chars * 4)
                size = os.path.getsize(full)
            except OSError:
                continue
            if b"\x00" in raw:
                continue  # binary — the listing still shows it
            decoded = raw.decode("utf-8", errors="replace")
            text = decoded[:per_file_chars][: max(0, max_total_chars - total)]
            header = f"--- {rel}"
            if len(raw) < size or len(text) < len(decoded):
                header += f" (truncated: first {len(text)} chars of {size} bytes)"
            header += " ---"
            blocks.append(f"{header}\n{text}")
            total += len(text)
    if skipped:
        blocks.append(f"... {skipped} more file(s) not shown")
    return "\n\n".join(blocks)


def merge_scores(items: list[dict], rollout_results: list[dict], judge_fn) -> list[dict]:
    """Merge rollout results with judge verdicts; errored tasks skip the judge."""
    merged = []
    for item, rollout_result in zip(items, rollout_results):
        result = dict(rollout_result)
        if result.get("error"):
            result.update({"hard": 0, "soft": 0.0, "judge_reason": ""})
        else:
            work_dir = result.get("work_dir", "")
            listing = artifacts_listing(work_dir)
            excerpts = artifacts_excerpts(work_dir, exclude_rel=(item.get("files") or {}).keys())
            if excerpts:
                listing = (f"{listing}\n\n"
                           f"Contents of agent-produced text files:\n{excerpts}")
            verdict = judge_fn(item, result.get("response", ""), listing)
            result.update(verdict)
        merged.append(result)
    return merged


def judge(item: dict, response: str, artifacts_listing: str = "") -> dict:
    """Score one agent *response* against *item*'s rubric via the judge model.

    Never raises.  Returns a result fragment with ``id``, ``hard``, ``soft``,
    ``judge_reason`` and, when applicable, ``judge_skipped`` / ``judge_error``.
    """
    result = {
        "id": str(item["id"]),
        "hard": 0,
        "soft": 0.0,
        "judge_reason": "",
    }
    if not (response or "").strip():
        result["judge_skipped"] = "empty_response"
        return result

    user_prompt = _build_judge_user_prompt(item, response, artifacts_listing)
    last_error = "no response"
    judge_usage = {"input": 0, "output": 0}
    judge_calls = 0
    for attempt in range(2):
        prompt = user_prompt if attempt == 0 else user_prompt + _RETRY_SUFFIX
        try:
            reply, usage = chat_optimizer(system=JUDGE_SYSTEM_PROMPT, user=prompt, stage="skilleval_judge")
        except Exception as exc:  # noqa: BLE001 — judge must never crash the batch
            last_error = f"judge call failed: {type(exc).__name__}: {exc}"
            continue
        judge_calls += 1
        if isinstance(usage, dict):
            judge_usage["input"] += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            judge_usage["output"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        verdict = _extract_verdict(reply)
        if verdict is not None:
            result["hard"] = int(verdict["pass"])
            result["soft"] = verdict["score"]
            result["judge_reason"] = verdict["reason"]
            if judge_calls:
                result["judge_usage"] = judge_usage
            return result
        last_error = f"unparseable judge reply: {reply[:200]!r}"

    result["judge_error"] = last_error
    if judge_calls:
        result["judge_usage"] = judge_usage
    return result
