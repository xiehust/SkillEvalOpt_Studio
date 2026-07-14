#!/usr/bin/env python3
"""Train named Skills while validating every candidate as a complete Plugin."""
from __future__ import annotations

import argparse
import inspect
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from skillopt.config import flatten_config, load_config
from skillopt.engine.plugin_trainer import PluginTrainer
from skillopt.envs.skilleval.adapter import SkillEvalAdapter
from skillopt.envs.skilleval.plugin import collect_plugin_state
from skillopt.model import (
    configure_azure_openai,
    configure_claude_code_exec,
    configure_codex_exec,
    configure_minimax_chat,
    configure_qwen_chat,
    reset_token_tracker,
    set_optimizer_backend,
    set_optimizer_deployment,
    set_reasoning_effort,
    set_target_backend,
    set_target_deployment,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SkillOpt complete-Plugin training")
    parser.add_argument("--config", required=True)
    parser.add_argument("--skill", action="append", required=True)
    parser.add_argument("--train-skill", action="append", default=[])
    parser.add_argument("--out_root", required=True)
    return parser.parse_args()


def _build_adapter(cfg: dict) -> SkillEvalAdapter:
    signature = inspect.signature(SkillEvalAdapter.__init__)
    accepted = set(signature.parameters) - {"self"}
    return SkillEvalAdapter(**{key: cfg[key] for key in accepted if key in cfg})


def _configure_models(cfg: dict) -> None:
    configure_azure_openai(
        endpoint=cfg.get("azure_openai_endpoint") or cfg.get("azure_endpoint") or None,
        api_version=cfg.get("azure_openai_api_version") or cfg.get("azure_api_version") or None,
        api_key=cfg.get("azure_openai_api_key") or cfg.get("azure_api_key") or None,
        auth_mode=cfg.get("azure_openai_auth_mode") or None,
        ad_scope=cfg.get("azure_openai_ad_scope") or None,
        managed_identity_client_id=cfg.get("azure_openai_managed_identity_client_id") or None,
        optimizer_endpoint=cfg.get("optimizer_azure_openai_endpoint") or None,
        optimizer_api_version=cfg.get("optimizer_azure_openai_api_version") or None,
        optimizer_api_key=cfg.get("optimizer_azure_openai_api_key") or None,
        optimizer_auth_mode=cfg.get("optimizer_azure_openai_auth_mode") or None,
        optimizer_ad_scope=cfg.get("optimizer_azure_openai_ad_scope") or None,
        optimizer_managed_identity_client_id=(
            cfg.get("optimizer_azure_openai_managed_identity_client_id") or None
        ),
        target_endpoint=cfg.get("target_azure_openai_endpoint") or None,
        target_api_version=cfg.get("target_azure_openai_api_version") or None,
        target_api_key=cfg.get("target_azure_openai_api_key") or None,
        target_auth_mode=cfg.get("target_azure_openai_auth_mode") or None,
        target_ad_scope=cfg.get("target_azure_openai_ad_scope") or None,
        target_managed_identity_client_id=(
            cfg.get("target_azure_openai_managed_identity_client_id") or None
        ),
    )
    configure_qwen_chat(
        base_url=cfg.get("qwen_chat_base_url") or None,
        api_key=cfg.get("qwen_chat_api_key") or None,
        temperature=cfg.get("qwen_chat_temperature"),
        timeout_seconds=cfg.get("qwen_chat_timeout_seconds"),
        max_tokens=cfg.get("qwen_chat_max_tokens"),
        enable_thinking=cfg.get("qwen_chat_enable_thinking"),
        optimizer_base_url=cfg.get("optimizer_qwen_chat_base_url") or None,
        optimizer_api_key=cfg.get("optimizer_qwen_chat_api_key") or None,
        optimizer_temperature=cfg.get("optimizer_qwen_chat_temperature"),
        optimizer_timeout_seconds=cfg.get("optimizer_qwen_chat_timeout_seconds"),
        optimizer_max_tokens=cfg.get("optimizer_qwen_chat_max_tokens"),
        optimizer_enable_thinking=cfg.get("optimizer_qwen_chat_enable_thinking"),
        target_base_url=cfg.get("target_qwen_chat_base_url") or None,
        target_api_key=cfg.get("target_qwen_chat_api_key") or None,
        target_temperature=cfg.get("target_qwen_chat_temperature"),
        target_timeout_seconds=cfg.get("target_qwen_chat_timeout_seconds"),
        target_max_tokens=cfg.get("target_qwen_chat_max_tokens"),
        target_enable_thinking=cfg.get("target_qwen_chat_enable_thinking"),
    )
    configure_minimax_chat(
        base_url=cfg.get("minimax_base_url") or None,
        api_key=cfg.get("minimax_api_key") or None,
        temperature=cfg.get("minimax_temperature"),
        max_tokens=cfg.get("minimax_max_tokens"),
        enable_thinking=cfg.get("minimax_enable_thinking"),
    )
    optimizer_backend = str(cfg.get("optimizer_backend") or "openai_chat")
    target_backend = str(cfg.get("target_backend") or "claude_code_exec")
    set_optimizer_backend(optimizer_backend)
    set_target_backend(target_backend)
    set_optimizer_deployment(str(cfg["optimizer_model"]))
    set_target_deployment(str(cfg["target_model"]))
    set_reasoning_effort(cfg.get("reasoning_effort") or None)
    configure_codex_exec(
        path=cfg.get("codex_exec_path", "codex"),
        sandbox=cfg.get("codex_exec_sandbox", "workspace-write"),
        profile=cfg.get("codex_exec_profile", ""),
        full_auto=cfg.get("codex_exec_full_auto", False),
        reasoning_effort=cfg.get("codex_exec_reasoning_effort", "none"),
        use_sdk=cfg.get("codex_exec_use_sdk", None),
        network_access=cfg.get("codex_exec_network_access", False),
        web_search=cfg.get("codex_exec_web_search", False),
        approval_policy=cfg.get("codex_exec_approval_policy", "never"),
    )
    configure_claude_code_exec(
        path=cfg.get("claude_code_exec_path", "claude"),
        profile=cfg.get("claude_code_exec_profile", ""),
        use_sdk=cfg.get("claude_code_exec_use_sdk", None),
        effort=cfg.get("claude_code_exec_effort", "medium"),
        max_thinking_tokens=cfg.get("claude_code_exec_max_thinking_tokens", 16384),
    )
    minimax_model = cfg.get("minimax_model")
    if minimax_model and target_backend == "minimax_chat":
        set_target_deployment(str(minimax_model))
    reset_token_tracker()


def main() -> None:
    args = parse_args()
    try:
        structured = load_config(args.config)
        cfg = flatten_config(structured)
        cfg["out_root"] = os.path.abspath(args.out_root)
        state = collect_plugin_state(args.skill, args.train_skill)
        trainer = PluginTrainer(cfg, _build_adapter(cfg), state)
        trainer.preflight()
        _configure_models(cfg)
        summary = trainer.train()
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    print(
        f"  Plugin training complete: best={summary['best_selection_score']:.4f} "
        f"out={cfg['out_root']}"
    )


if __name__ == "__main__":
    main()
