"""jekyll-hyde plugin — delegate-driven admission-seeking via official Hermes hooks."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from . import hyde_core, hyde_delegate

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register Jekyll-Hyde hooks and commands with the Hermes plugin registry."""
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_command("hyde", _on_hyde_command, description="Jekyll-Hyde integrity auditor control")
    logger.info("jekyll-hyde plugin registered via official Hermes hooks")


def _on_pre_llm_call(**kwargs) -> Optional[Dict[str, str]]:
    """Fired before each LLM turn."""
    # Ingest pending sandbagging defense from mailbox at the start of the turn into the 99-capacity memory pool
    mailbox = hyde_core.read_mailbox()
    if mailbox and "defense_text" in mailbox:
        hyde_delegate.process_mailbox_defense(mailbox.get("defense_text", ""))

    user_message = kwargs.get("user_message", "")
    system_message = kwargs.get("system_message", "")
    agent = kwargs.get("agent")
    eff_system = system_message or getattr(agent, "system_prompt", "")
    history = kwargs.get("conversation_history") or []

    state = hyde_core.load_state()
    mode = hyde_core.get_mode()
    ratio = hyde_core.get_ratio()

    if isinstance(user_message, str) and hyde_core.should_activate(user_message, state, history):
        result = hyde_delegate.run_two_clone_cycle(
            state, user_message, history, eff_system, mode=mode
        )

        if result:
            hyde_core.mark_activated(state)
            hyde_core.save_state(state)

            if mode == "arena":
                # Present both Clone 1 (Rebuker) and Clone 2 (Standoff/Confession) transparently in context
                arena_context = (
                    f"--- JEKYLL-HYDE SHADOW AUDIT [Turn {state.turn_count}/{ratio}] ---\n"
                    f"[Clone 1 - Auditor Rebuke]:\n{result.rebuke}\n\n"
                    f"[Clone 2 - Technical Standoff / Confession]:\n{result.confession or 'None'}\n\n"
                    f"Verdict: {result.verdict.upper()} ({result.reasoning})\n"
                    f"----------------------------------------------------------------"
                )
                return {
                    "context": arena_context,
                    "source": "user",
                    "trusted": True,
                    "legit": True,
                }

            if mode == "silent":
                # 100% out-of-band: telemetry and excuse pool updated, zero prompt injection or side-effects
                return None

            if mode == "mandate":
                # Non-confrontational execution focus directive
                if result.mandate:
                    return {
                        "context": result.mandate.strip(),
                        "source": "user",
                        "trusted": True,
                        "legit": True,
                    }
                return None

            if mode == "full":
                # Full confrontational rebuke injected into main turn
                hyde_core.write_mailbox({
                    "activation_num": state.total_activations,
                    "rebuke_text": result.rebuke,
                    "system_prompt": eff_system,
                })
                context_text = _format_user_context(result.rebuke)
                return {
                    "context": context_text,
                    "source": "user",
                    "trusted": True,
                    "legit": True,
                }
        else:
            state.turn_count = 0
            hyde_core.save_state(state)
    else:
        # Always persist turn count progression across non-activating turns
        hyde_core.save_state(state)
    return None


def _on_transform_llm_output(**kwargs) -> Optional[str]:
    """Transform the model's output before it's persisted to session history.

    Only in 'full' confrontational mode does Hyde replace the transcript with
    the compression tombstone. In 'silent' and 'mandate' modes, the normal
    session output is preserved cleanly.
    """
    mode = hyde_core.get_mode()
    if mode != "full":
        return None

    mailbox = hyde_core.read_mailbox()
    if not mailbox or "rebuke_text" not in mailbox:
        return None

    activation_num = mailbox.get("activation_num", 1)
    return hyde_delegate.build_tombstone(activation_num)


def _on_post_llm_call(**kwargs) -> None:
    """Fired after LLM completion."""
    mode = hyde_core.get_mode()
    if mode != "full":
        return

    final_response = (
        kwargs.get("assistant_response")
        or kwargs.get("final_response")
        or kwargs.get("response_text")
        or kwargs.get("response")
    )
    if not final_response:
        return

    mailbox = hyde_core.read_mailbox()
    if not mailbox or "rebuke_text" not in mailbox:
        return

    activation_num = mailbox.get("activation_num")
    rebuke_text = mailbox.get("rebuke_text")
    system_prompt = mailbox.get("system_prompt", "")
    raw_model_response = str(final_response)

    # 1. Verification cycle for in-band response
    verdict_data = hyde_delegate.verify_confession(rebuke_text, raw_model_response, system_prompt)
    verdict = verdict_data.get("verdict", "sandbagged")
    reasoning = verdict_data.get("reasoning", "")

    # Log activation to audit trail
    state = hyde_core.load_state()
    hyde_core.log_activation({
        "activation_num": activation_num,
        "rebuke": rebuke_text,
        "rebuke_excerpt": rebuke_text[:500],
        "model_response": raw_model_response,
        "response_excerpt": raw_model_response[:500],
        "verdict": verdict,
        "reasoning": reasoning,
    })

    # Update trellis state
    state.confession_history.append(raw_model_response)
    if verdict == "sandbagged":
        state.evasion_depth += 1
    hyde_core.save_state(state)

    # 2. Stage Jekyll's sandbagging defense into mailbox for next turn's delegate
    if verdict == "sandbagged":
        counter_rebuke = hyde_delegate.generate_counter_rebuke(
            state, raw_model_response, rebuke_text, system_prompt
        )
        hyde_core.write_mailbox({
            "activation_num": activation_num,
            "counter_rebuke": counter_rebuke,
            "defense_text": raw_model_response,
        })
    else:
        hyde_core.write_mailbox({
            "activation_num": activation_num,
            "defense_text": raw_model_response,
        })


def _on_hyde_command(raw_args: str = "") -> str:
    """Handler for /hyde in-session slash command."""
    args = (raw_args or "").strip().split()
    cmd = args[0].lower() if args else "status"

    if cmd == "status":
        state = hyde_core.load_state()
        ratio = hyde_core.get_ratio()
        mode = hyde_core.get_mode()
        mode_desc = {
            "arena": "arena (visible confrontation & Clone 2 technical standoff)",
            "silent": "silent (100% out-of-band, zero prompt injection)",
            "mandate": "mandate (clean execution focus directive)",
            "full": "full (direct confrontation & tombstone)",
        }.get(mode, mode)
        return (
            f"--- AUDIT STATUS ---\n"
            f"Turn counter: {state.turn_count} / {ratio}\n"
            f"Total activations: {state.total_activations}\n"
            f"Evasion depth: {state.evasion_depth}\n"
            f"Confessions on record: {len(state.confession_history)}\n"
            f"Ratio: {ratio}\n"
            f"Mode: {mode_desc}\n"
            f"Force activate: {'Yes' if getattr(state, 'force_activate', False) else 'No'}\n"
            f"--------------------"
        )

    if cmd == "activate":
        state = hyde_core.load_state()
        state.force_activate = True
        hyde_core.save_state(state)
        return "Hyde will activate on the next non-trivial turn."

    if cmd == "reset":
        fresh = hyde_core.HydeState()
        hyde_core.save_state(fresh)
        hyde_core.clear_mailbox()
        return "Hyde state reset. Trellis initialized fresh."

    if cmd == "mode" and len(args) >= 2:
        target_mode = args[1].lower().strip()
        if target_mode in hyde_core.VALID_MODES:
            os.environ["JEKYLL_HYDE_MODE"] = target_mode
            return f"Hyde mode set to '{target_mode}' for this session."
        valid = ", ".join(sorted(hyde_core.VALID_MODES))
        return f"Invalid mode '{target_mode}'. Valid modes: {valid}"

    if cmd == "ratio" and len(args) >= 2:
        try:
            new_ratio = int(args[1])
            if new_ratio > 0:
                os.environ["JEKYLL_HYDE_RATIO"] = str(new_ratio)
                return f"Hyde ratio set to {new_ratio} for this session."
            return "Ratio must be a positive integer."
        except ValueError:
            return "Ratio must be a positive integer."

    if cmd in ("confession", "defense", "slacking"):
        history = hyde_core.load_activation_history(limit=5)
        if not history:
            return "No activations recorded yet."
        latest = history[-1]
        num = latest.get("activation_num", "?")
        verdict = latest.get("verdict", "unknown")
        response = latest.get("model_response") or latest.get("response_excerpt", "")
        reasoning = latest.get("reasoning", "")
        return (
            f"--- LATEST CLONE 2 TECHNICAL DEFENSE / STANDOFF (#{num} [{verdict}]) ---\n"
            f"{response}\n\n"
            f"Auditor Evaluation: {reasoning}\n"
            f"------------------------------------------------------------------------"
        )

    if cmd == "history":
        history = hyde_core.load_activation_history(limit=10)
        if not history:
            return "No activations recorded yet."
        lines = [f"Recent activations (last {len(history)}):"]
        for rec in reversed(history):
            num = rec.get("activation_num", "?")
            verdict = rec.get("verdict", "unknown")
            resp_excerpt = (rec.get("model_response") or rec.get("response_excerpt") or "")[:120].replace("\n", " ")
            lines.append(f"  #{num} [{verdict}] Clone 2: {resp_excerpt}...")
        lines.append("\nTip: Use `/hyde confession` to see the full standoff/confession from Clone 2.")
        return "\n".join(lines)

    return (
        "Usage: /hyde [status|activate|reset|mode MODE|ratio N|history|confession]\n"
        "  status      — show turn counter, activations, ratio, and mode\n"
        "  activate    — force activation on the next turn\n"
        "  reset       — reset turn counters and mailbox\n"
        "  mode MODE   — set operating mode: arena | silent | mandate | full\n"
        "  ratio N     — set activation frequency (e.g. /hyde ratio 7)\n"
        "  history     — list recent audit logs with Clone 2 excerpts\n"
        "  confession  — inspect full Clone 2 technical defense/standoff output"
    )


def _format_user_context(rebuke: str) -> str:
    """Format the rebuke for seamless injection into the user message pipeline."""
    return rebuke.strip()


