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

from skillopt.model import chat_optimizer

JUDGE_SYSTEM_PROMPT = (
    "You are a strict evaluator for agent task outputs. You are given a task, "
    "an acceptance rubric, the agent's final response, and a listing of files "
    "the agent produced. Judge ONLY against the rubric. Reply with ONLY a JSON "
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
    for attempt in range(2):
        prompt = user_prompt if attempt == 0 else user_prompt + _RETRY_SUFFIX
        try:
            reply, _usage = chat_optimizer(system=JUDGE_SYSTEM_PROMPT, user=prompt, stage="skilleval_judge")
        except Exception as exc:  # noqa: BLE001 — judge must never crash the batch
            last_error = f"judge call failed: {type(exc).__name__}: {exc}"
            continue
        verdict = _extract_verdict(reply)
        if verdict is not None:
            result["hard"] = int(verdict["pass"])
            result["soft"] = verdict["score"]
            result["judge_reason"] = verdict["reason"]
            return result
        last_error = f"unparseable judge reply: {reply[:200]!r}"

    result["judge_error"] = last_error
    return result
