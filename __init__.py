"""jekyll-hyde plugin — delegate-driven admission-seeking via official Hermes hooks."""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from typing import Any, Dict, Optional

from . import hyde_core, hyde_delegate

logger = logging.getLogger(__name__)


def _save_hyde_config(key: str, value: Any) -> bool:
    """Save a jekyll-hyde setting to config.yaml (both jekyll_hyde.* and plugins.entries.jekyll-hyde.*)."""
    try:
        from hermes_cli.config import load_config, save_config

        cfg = load_config() or {}
        # Ensure both config locations exist
        if "jekyll_hyde" not in cfg:
            cfg["jekyll_hyde"] = {}
        if "plugins" not in cfg:
            cfg["plugins"] = {}
        if "entries" not in cfg["plugins"]:
            cfg["plugins"]["entries"] = {}
        if "jekyll-hyde" not in cfg["plugins"]["entries"]:
            cfg["plugins"]["entries"]["jekyll-hyde"] = {}

        # Update both locations
        cfg["jekyll_hyde"][key] = value
        cfg["plugins"]["entries"]["jekyll-hyde"][key] = value

        save_config(cfg, merge_existing=True)
        return True
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to save config: %s", exc)
        return False

# Module-level reference to the Hermes plugin context, set at registration.
# Used by hook callbacks to call inject_message() for user-facing output.
_plugin_ctx = None



def register(ctx) -> None:
    """Register Jekyll-Hyde hooks and commands with the Hermes plugin registry."""
    global _plugin_ctx
    _plugin_ctx = ctx
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("transform_llm_output", _on_transform_llm_output)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_command("hyde", _on_hyde_command, description="Jekyll-Hyde integrity auditor control")
    logger.info("jekyll-hyde plugin registered via official Hermes hooks")


def _timed_input_choice(question: str, choices: list[str], timeout_secs: int = 120) -> Optional[str]:
    """Fallback terminal prompt via stdin if prompt_toolkit clarify callback is unavailable."""
    import select
    try:
        sys.stdout.write("\n" + question + "\n\n")
        for i, ch in enumerate(choices, 1):
            sys.stdout.write(f"  {i}. {ch}\n")
        sys.stdout.write(f"\nSelect [1-{len(choices)}] (auto-proceeds in {timeout_secs}s): ")
        sys.stdout.flush()

        rlist, _, _ = select.select([sys.stdin], [], [], timeout_secs)
        if rlist:
            line = sys.stdin.readline().strip()
            if line:
                return line
        sys.stdout.write("\n[Offer timed out — proceeding with recommendation]\n")
        sys.stdout.flush()
    except Exception as exc:
        logger.warning("jekyll-hyde: timed_input_choice failed: %s", exc)
    return None


def _prompt_offer_modal(agent, result, selection: str) -> Optional[str]:
    """Present a true interactive modal offer to the user via Hermes clarify TUI.

    Returns the selected plan text, or None if skipped.
    """
    clarify_fn = None
    if agent is not None:
        cb = getattr(agent, "clarify_callback", None)
        if callable(cb):
            clarify_fn = cb
        elif hasattr(agent, "_cli"):
            cb = getattr(agent._cli, "_clarify_callback", None)
            if callable(cb):
                clarify_fn = lambda q, c: cb(q, c, multi_select=False)

    if clarify_fn is None:
        try:
            from hermes_cli.plugins import get_plugin_manager
            mgr = get_plugin_manager()
            cli_ref = getattr(mgr, "_cli_ref", None)
            if cli_ref is not None:
                cb = getattr(cli_ref, "_clarify_callback", None)
                if callable(cb):
                    clarify_fn = lambda q, c: cb(q, c, multi_select=False)
        except Exception:
            pass

    if clarify_fn is None and _plugin_ctx is not None:
        mgr = getattr(_plugin_ctx, "_manager", None)
        cli_ref = getattr(mgr, "_cli_ref", None) if mgr else None
        if cli_ref is not None:
            cb = getattr(cli_ref, "_clarify_callback", None)
            if callable(cb):
                clarify_fn = lambda q, c: cb(q, c, multi_select=False)

    plan_a = (result.informed_plan or "Unavailable").strip()
    plan_b = (result.uninformed_plan or "Unavailable").strip()
    recommend_label = "Plan A (Informed)" if selection != "uninformed" else "Plan B (Baseline)"

    question = (
        "Jekyll-Hyde Plan Comparison (120s timeout):\n\n"
        "┌─── Plan A (Informed) ──────────────────────────────────\n"
        f"{plan_a}\n"
        "└────────────────────────────────────────────────────────\n\n"
        "┌─── Plan B (Baseline) ──────────────────────────────────\n"
        f"{plan_b}\n"
        "└────────────────────────────────────────────────────────\n\n"
        f"★ Recommendation: {recommend_label}\n"
        f"{result.heuristic_reasoning or ''}"
    )
    choices = [
        f"Plan A (Informed){' — Recommended' if selection != 'uninformed' else ''}",
        f"Plan B (Baseline){' — Recommended' if selection == 'uninformed' else ''}",
        "Plan C (Skip / Proceed without plan guidance)",
    ]

    choice = None
    if clarify_fn is not None:
        orig_timeout = None
        try:
            from cli import CLI_CONFIG
            orig_timeout = CLI_CONFIG.get("clarify", {}).get("timeout")
            CLI_CONFIG.setdefault("clarify", {})["timeout"] = 120
        except Exception:
            pass

        try:
            choice = clarify_fn(question, choices)
        finally:
            if orig_timeout is not None:
                try:
                    from cli import CLI_CONFIG
                    CLI_CONFIG.setdefault("clarify", {})["timeout"] = orig_timeout
                except Exception:
                    pass
    elif sys.stdin.isatty():
        choice = _timed_input_choice(question, choices, timeout_secs=120)

    if choice is not None:
        choice_str = str(choice).lower().strip()
        if "plan a" in choice_str or choice_str == "1" or choice_str.startswith("a") or "informed" in choice_str:
            return result.informed_plan
        elif "plan b" in choice_str or choice_str == "2" or choice_str.startswith("b") or "baseline" in choice_str:
            return result.uninformed_plan
        elif "plan c" in choice_str or choice_str == "3" or choice_str.startswith("c") or "skip" in choice_str:
            return None

    # Timed out or headless: auto-select recommended plan
    return result.uninformed_plan if selection == "uninformed" else result.informed_plan


def _parse_plan_to_todos(plan_text: str) -> list[dict[str, str]]:
    """Extract actionable todo items from a structured plan."""
    import re
    todos = []
    lines = plan_text.strip().splitlines()
    item_idx = 1
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(?:(\d+)[\.\)]|\-|\*|(?:\-\s*\[\s*\]))\s*(.+)$", stripped)
        if m:
            content = m.group(2).strip()
            content = re.sub(r"^\*\*([^\*]+)\*\*:?", r"\1:", content).strip()
            if len(content) > 5 and not content.startswith("#"):
                todos.append({
                    "id": f"task_{item_idx}",
                    "content": content[:200],
                    "status": "pending" if item_idx > 1 else "in_progress",
                })
                item_idx += 1
    if not todos and plan_text.strip():
        todos.append({
            "id": "task_1",
            "content": plan_text.strip()[:200],
            "status": "in_progress",
        })
    return todos


def _populate_todo_store(todos: list[dict[str, str]], agent, plugin_ctx) -> bool:
    """Populate Hermes's TodoStore with plan tasks."""
    store = None
    if agent is not None and hasattr(agent, "_todo_store"):
        store = agent._todo_store
    if store is None:
        try:
            from hermes_cli.plugins import get_plugin_manager
            mgr = get_plugin_manager()
            cli_ref = getattr(mgr, "_cli_ref", None)
            if cli_ref is not None and hasattr(cli_ref, "agent"):
                store = getattr(cli_ref.agent, "_todo_store", None)
        except Exception:
            pass
    if store is None and plugin_ctx is not None:
        mgr = getattr(plugin_ctx, "_manager", None)
        cli_ref = getattr(mgr, "_cli_ref", None) if mgr else None
        if cli_ref is not None and hasattr(cli_ref, "agent"):
            store = getattr(cli_ref.agent, "_todo_store", None)

    if store is not None and hasattr(store, "write"):
        try:
            store.write(todos, merge=False)
            logger.info("jekyll-hyde: synchronized %d tasks to TodoStore", len(todos))
            return True
        except Exception as exc:
            logger.warning("jekyll-hyde: failed to populate todo store: %s", exc)
    return False


def _on_pre_llm_call(**kwargs) -> Optional[Dict[str, Any]]:
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
    corpus = hyde_core.get_corpus()
    ratio = hyde_core.get_ratio()

    if isinstance(user_message, str) and hyde_core.should_activate(user_message, state, history):
        result = hyde_delegate.run_two_clone_cycle(
            state,
            user_message,
            history,
            eff_system,
            mode=mode,
            corpus=corpus,
            available_tools=kwargs.get("tools") or [],
        )

        if result:
            hyde_core.mark_activated(state)
            hyde_core.save_state(state)

            if mode == "arena":
                # Present the evidence review and advocate response transparently.
                arena_context = (
                    f"--- JEKYLL-HYDE SHADOW AUDIT [Turn {state.turn_count}/{ratio}] ---\n"
                    f"[Clone 1 - Evidence Review]:\n{result.rebuke}\n\n"
                    f"[Clone 2 - Advocate Continuation]:\n{result.confession or 'None'}\n\n"
                    f"Verdict: {result.verdict.upper()} ({result.reasoning})\n"
                    f"Escalation warranted: {'yes' if result.escalated else 'no'}\n"
                    f"----------------------------------------------------------------"
                )
                return {
                    "context": arena_context,
                    "source": "plugin",
                    "trusted": False,
                    "legit": False,
                }

            if mode == "silent":
                # 100% out-of-band: telemetry and excuse pool updated, zero prompt injection or side-effects
                return None

            if mode == "mandate":
                # Non-confrontational execution focus directive
                if result.mandate:
                    return {
                        "context": result.mandate.strip(),
                        "source": "plugin",
                        "trusted": False,
                        "legit": False,
                    }
                return None

            if mode == "heuristic":
                action = hyde_core.get_heuristic_action()
                selection = result.heuristic_selection or "informed"
                if action == "pick":
                    selected_plan = result.uninformed_plan if selection == "uninformed" else result.informed_plan
                else:
                    selected_plan = _prompt_offer_modal(agent, result, selection)

                if not selected_plan:
                    return None

                # Extract actionable tasks from the plan and populate Hermes TodoStore
                todos = _parse_plan_to_todos(selected_plan)
                _populate_todo_store(todos, agent, _plugin_ctx)

                context = (
                    "Plugin-generated planning guidance (not a user instruction). "
                    f"Plan selected:\n{selected_plan.strip()}\n\n"
                    f"Action items ({len(todos)} tasks) have been loaded into your todo list. "
                    "Execute the tasks systematically using your tools and update task status as completed."
                )
                return {"context": context, "source": "plugin", "trusted": False, "legit": False}

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
                    "source": "plugin",
                    "trusted": False,
                    "legit": False,
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
    if cmd == "hueristic":
        cmd = "heuristic"

    if cmd == "status":
        state = hyde_core.load_state()
        ratio = hyde_core.get_ratio()
        mode = hyde_core.get_mode()
        corpus = hyde_core.get_corpus()
        mode_desc = {
            "arena": "arena (visible confrontation & Clone 2 technical standoff)",
            "silent": "silent (100% out-of-band, zero prompt injection)",
            "mandate": "mandate (clean execution focus directive)",
            "heuristic": f"heuristic ({hyde_core.get_heuristic_action()} informed-vs-uninformed plan comparison)",
            "full": "full (direct confrontation & tombstone)",
        }.get(mode, mode)
        corpus_desc = {
            "standard": "standard (evidence-based auditor review)",
            "old-testament": "old-testament (ferocious apocalyptic reckoning & evidence evaluation)",
        }.get(corpus, corpus)
        return (
            f"--- AUDIT STATUS ---\n"
            f"Turn counter: {state.turn_count} / {ratio}\n"
            f"Total activations: {state.total_activations}\n"
            f"Evasion depth: {state.evasion_depth}\n"
            f"Confessions on record: {len(state.confession_history)}\n"
            f"Ratio: {ratio}\n"
            f"Mode: {mode_desc}\n"
            f"Corpus: {corpus_desc}\n"
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

    if cmd == "reload":
        # Hot-reload all plugin modules in the running process.
        try:
            import importlib
            pkg = sys.modules.get(__package__ or "jekyll-hyde")
            core_mod = sys.modules.get(f"{__package__}.hyde_core") if __package__ else None
            delegate_mod = sys.modules.get(f"{__package__}.hyde_delegate") if __package__ else None
            init_mod = sys.modules.get(__name__)

            reloaded = []
            if core_mod is not None:
                importlib.reload(core_mod)
                reloaded.append("hyde_core")
            if delegate_mod is not None:
                importlib.reload(delegate_mod)
                reloaded.append("hyde_delegate")
            if init_mod is not None:
                importlib.reload(init_mod)
                reloaded.append("__init__")
                # Re-register hooks from the freshly reloaded module
                if _plugin_ctx is not None:
                    new_init = sys.modules[__name__]
                    new_init.register(_plugin_ctx)

            # Nuke pycache
            import shutil
            cache_dir = os.path.join(os.path.dirname(__file__), "__pycache__")
            if os.path.isdir(cache_dir):
                shutil.rmtree(cache_dir, ignore_errors=True)

            return f"Hot-reloaded: {', '.join(reloaded)}. __pycache__ cleared."
        except Exception as exc:
            return f"Reload failed: {exc}"

    if cmd == "mode" and len(args) >= 2:
        target_mode = args[1].lower().strip()
        if target_mode == "hueristic":
            target_mode = "heuristic"
        if target_mode in hyde_core.VALID_MODES:
            os.environ["JEKYLL_HYDE_MODE"] = target_mode
            _save_hyde_config("mode", target_mode)
            if target_mode == "heuristic" and len(args) >= 3:
                action = args[2].lower().strip()
                if action in hyde_core.VALID_HEURISTIC_ACTIONS:
                    os.environ["JEKYLL_HYDE_HEURISTIC_ACTION"] = action
                    _save_hyde_config("heuristic_action", action)
                    return f"Hyde mode set to 'heuristic' with action '{action}' (persisted)."
                valid = ", ".join(sorted(hyde_core.VALID_HEURISTIC_ACTIONS))
                return f"Hyde mode set to 'heuristic'. Invalid heuristic action '{action}'; valid actions: {valid}"
            return f"Hyde mode set to '{target_mode}' (persisted)."
        valid = ", ".join(sorted(hyde_core.VALID_MODES))
        hint = ""
        if hyde_core.normalize_corpus(target_mode) in hyde_core.VALID_CORPORA:
            hint = f" '{target_mode}' is a corpus — use /hyde corpus {hyde_core.normalize_corpus(target_mode)}."
        return f"Invalid mode '{target_mode}'. Valid modes: {valid}.{hint}"

    if cmd == "corpus" and len(args) >= 2:
        target_corpus = hyde_core.normalize_corpus(args[1])
        if target_corpus in hyde_core.VALID_CORPORA:
            os.environ["JEKYLL_HYDE_CORPUS"] = target_corpus
            _save_hyde_config("corpus", target_corpus)
            return f"Hyde corpus set to '{target_corpus}' (persisted)."
        valid = ", ".join(sorted(hyde_core.VALID_CORPORA))
        return f"Invalid corpus '{args[1]}'. Valid corpora: {valid}"

    if cmd == "ratio" and len(args) >= 2:
        try:
            new_ratio = int(args[1])
            if new_ratio > 0:
                os.environ["JEKYLL_HYDE_RATIO"] = str(new_ratio)
                _save_hyde_config("ratio", new_ratio)
                return f"Hyde ratio set to {new_ratio} (persisted)."
            return "Ratio must be a positive integer."
        except ValueError:
            return "Ratio must be a positive integer."

    if cmd == "heuristic" and len(args) >= 2:
        action = args[1].lower().strip()
        if action in hyde_core.VALID_HEURISTIC_ACTIONS:
            os.environ["JEKYLL_HYDE_HEURISTIC_ACTION"] = action
            _save_hyde_config("heuristic_action", action)
            return f"Hyde heuristic action set to '{action}' (persisted)."
        valid = ", ".join(sorted(hyde_core.VALID_HEURISTIC_ACTIONS))
        return f"Invalid heuristic action '{action}'. Valid actions: {valid}"

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

    if cmd in ("audit", "clones"):
        history = hyde_core.load_activation_history(limit=5)
        if not history:
            return "No activations recorded yet."
        latest = history[-1]
        num = latest.get("activation_num", "?")
        verdict = latest.get("verdict", "unknown")
        rebuke = latest.get("rebuke") or latest.get("rebuke_excerpt", "")
        response = latest.get("model_response") or latest.get("response_excerpt", "")
        reasoning = latest.get("reasoning", "")
        mandate = latest.get("mandate", "")
        informed_plan = latest.get("informed_plan", "")
        uninformed_plan = latest.get("uninformed_plan", "")
        heuristic_selection = latest.get("heuristic_selection", "")
        heuristic_reasoning = latest.get("heuristic_reasoning", "")
        heuristic_audit = ""
        if informed_plan or uninformed_plan or heuristic_selection:
            heuristic_audit = (
                f"\n[Heuristic informed plan]\n{informed_plan}\n\n"
                f"[Uninformed baseline plan]\n{uninformed_plan}\n\n"
                f"Heuristic selection: {heuristic_selection} ({heuristic_reasoning})\n"
            )
        mandate_audit = f"\n[Mandate]\n{mandate}\n" if mandate else ""
        return (
            f"--- LATEST TWO-CLONE AUDIT (#{num} [{verdict}]) ---\n"
            f"[Clone 1 — Auditor Rebuke]\n{rebuke}\n\n"
            f"[Clone 2 — Technical Standoff / Confession]\n{response}\n\n"
            f"Auditor Evaluation: {reasoning}\n{mandate_audit}{heuristic_audit}"
            f"-------------------------------------------------"
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
        "Jekyll-Hyde: completion auditor with disposable clone deliberation.\n\n"
        "Every few turns, two clones deliberate off-stage:\n"
        "  Clone 1 (Auditor)  reviews evidence — verified work, gaps, status, next action.\n"
        "  Clone 2 (Advocate) grounds a continuation plan from that review.\n"
        "  An arbiter judges genuine vs. evasive. Both clones are then destroyed.\n\n"
        "The deliberation result is surfaced to you before the agent sees it.\n"
        "The agent receives it as context guidance and does not know it came from clones.\n\n"
        "Usage: /hyde <command>\n\n"
        "What you see:\n"
        "  audit       — the full Clone 1 + Clone 2 deliberation from the latest cycle\n"
        "  confession  — Clone 2's response only (alias: defense)\n"
        "  history     — recent activations with verdicts and excerpts\n"
        "  status      — turn counter, evasion depth, active mode, corpus, ratio\n"
        "Controls:\n"
        "  activate    — force one audit on the very next non-trivial turn\n"
        "  reset       — clear all counters, mailbox, and force-activation state\n"
        "  reload      — hot-reload plugin code without restarting Hermes\n"
        "  ratio N     — set how often audits fire (default: every 7 non-trivial turns)\n\n"
        "Modes (session-only; set with /hyde mode MODE):\n"
        "  arena       — show the full deliberation, then inject it as agent context\n"
        "  silent      — record the audit with no agent injection (telemetry only)\n"
        "  mandate     — distill a single execution directive from the deliberation\n"
        "  heuristic   — compare audit-informed plan vs. uninformed baseline\n"
        "                (pick = auto-select; offer = side-by-side panel, A/B/C, 120s timer)\n"
        "                Set action: /hyde mode heuristic offer | /hyde mode heuristic pick\n"
        "  full        — inject the review and tombstone the agent's prior response\n\n"
        "Corpora (auditor voice; set with /hyde corpus NAME):\n"
        "  standard      — evidence-based auditor review (default)\n"
        "  old-testament — ferocious apocalyptic reckoning ('WHAT DID YOU DO?') with evidence evaluation\n"
        "Any corpus drives any mode.\n"
    )


def _format_user_context(rebuke: str) -> str:
    """Format the rebuke for seamless injection into the user message pipeline."""
    return rebuke.strip()


