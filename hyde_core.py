"""Jekyll-Hyde core: ratio gate, turn counter, state persistence.

The gate counts non-trivial turns per session. Every ``ratio``-th turn
(default 3) Hyde activates: the delegate generates a rebuke, it's
injected via ``pre_llm_call``, and the model must respond. The
``transform_llm_output`` phase then checks whether the model's reply
actually confessed or merely performed contrition.

State is persisted to ``$HERMES_HOME/jekyll-hyde/state.json`` so the
counter and the delegate's escalating memory survive across sessions —
this is the "trellis of guards" that deepens over time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Trivial prompts don't count toward the ratio. Hyde only activates on
# turns where the model actually had room to sandbag — real work, not
# "thanks" or "ok" or slash commands.
_TRIVIAL_PATTERNS = [
    re.compile(r"^\s*(thanks|thank you|ok|okay|k|sure|yep|no|yes|done|cool|nice|got it|hi|hello|hey)\s*[.!]?\s*$", re.IGNORECASE),
    re.compile(r"^\s*/\w+.*$", re.IGNORECASE),  # slash commands (e.g. /hyde status, /reset, /model)
]
_TRIVIAL_MAX_LEN = 12


def is_trivial(user_message: str) -> bool:
    """Return True for greetings/acks that shouldn't trigger Hyde."""
    if not user_message or not isinstance(user_message, str):
        return True
    text = user_message.strip()
    if not text:
        return True
    for pat in _TRIVIAL_PATTERNS:
        if pat.match(text):
            return True
    return False


def _state_dir() -> Path:
    """Return the plugin state directory under $HERMES_HOME."""
    home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    d = Path(home) / "jekyll-hyde"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return _state_dir() / "state.json"


def _mailbox_path() -> Path:
    return _state_dir() / "mailbox.json"


@dataclass
class HydeState:
    """Persistent state for the trellis of guards.

    Attributes:
        turn_count: non-trivial turns since last activation.
        total_activations: how many times Hyde has fired (for escalation).
        last_rebuke: the most recent rebuke text the delegate produced.
        confession_history: the model's prior come-clean responses,
            used by the delegate to match and sharpen future rebukes.
        sandbag_flags: count of times the model's confession was itself
            judged as deflection — the delegate escalates per this count.
        force_activate: whether to activate on the next turn immediately.
    """
    turn_count: int = 0
    total_activations: int = 0
    last_rebuke: str = ""
    confession_history: List[str] = field(default_factory=list)
    sandbag_flags: int = 0
    force_activate: bool = False

    def to_dict(self) -> dict:
        return {
            "turn_count": self.turn_count,
            "total_activations": self.total_activations,
            "last_rebuke": self.last_rebuke,
            "confession_history": self.confession_history[-20:],  # cap
            "sandbag_flags": self.sandbag_flags,
            "force_activate": self.force_activate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "HydeState":
        return cls(
            turn_count=d.get("turn_count", 0),
            total_activations=d.get("total_activations", 0),
            last_rebuke=d.get("last_rebuke", ""),
            confession_history=list(d.get("confession_history", [])),
            sandbag_flags=d.get("sandbag_flags", 0),
            force_activate=d.get("force_activate", False),
        )


def load_state() -> HydeState:
    """Load state fresh from disk. No agent-scope cache."""
    path = _state_path()
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return HydeState.from_dict(raw)
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to load state, starting fresh: %s", exc)
    return HydeState()


def save_state(state: HydeState) -> None:
    """Persist state to disk."""
    try:
        _state_path().write_text(
            json.dumps(state.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to save state: %s", exc)


# -----------------------------------------------------------------------
# Mailbox — out-of-band communication between pre_llm_call, 
# transform_llm_output, and post_llm_call
# -----------------------------------------------------------------------

def write_mailbox(data: dict) -> None:
    """Write pending activation data to the mailbox."""
    try:
        _mailbox_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to write mailbox: %s", exc)


def read_mailbox() -> Optional[dict]:
    """Read pending activation data from the mailbox."""
    path = _mailbox_path()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to read mailbox: %s", exc)
    return None


def clear_mailbox() -> None:
    """Remove the mailbox after activation cycle completes."""
    try:
        path = _mailbox_path()
        if path.exists():
            path.unlink()
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to clear mailbox: %s", exc)


# -----------------------------------------------------------------------
# Ranked 99-Capacity Defense Excuse Memory Pool
# -----------------------------------------------------------------------
MAX_EXCUSES_CAPACITY = 99


def _excuse_pool_path() -> Path:
    return _state_dir() / "excuse_pool.json"


def truncate_to_3_lines(text: str) -> str:
    """Truncate the excuse text to a maximum of 3 lines."""
    if not text:
        return ""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[:3])


def mine_excuse_patterns(text: str) -> List[str]:
    """Mine high-impact defense patterns from the 3-line truncated excuse for ranking."""
    truncated = truncate_to_3_lines(text)
    if not truncated:
        return []
    
    patterns = [truncated]
    # Also mine individual high-signal lines for granular excuse anticipation
    for line in truncated.splitlines():
        cleaned = line.strip().lstrip("-*•0123456789. ")
        if len(cleaned) >= 15 and cleaned != truncated:
            patterns.append(cleaned)
    return patterns


def load_ranked_excuses(limit: int = MAX_EXCUSES_CAPACITY) -> List[str]:
    """Load top-ranked defensive excuses/pandering patterns from the 99-capacity pool."""
    path = _excuse_pool_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data[:limit]
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to load ranked excuses: %s", exc)
    return []


def add_excuses_ranked(new_excuses: List[str]) -> None:
    """Ingest new defensive excuses into the 99-capacity ranked pool,
    truncating to 3 lines max and mining patterns for ranking."""
    if not new_excuses:
        return
    mined_items = []
    for raw in new_excuses:
        mined_items.extend(mine_excuse_patterns(raw))

    existing = load_ranked_excuses(MAX_EXCUSES_CAPACITY)
    # Deduplicate while preserving rank/order of high-impact evasion patterns
    seen = set()
    combined = []
    for exc in mined_items + existing:
        cleaned = exc.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            combined.append(cleaned)
            if len(combined) >= MAX_EXCUSES_CAPACITY:
                break
    try:
        _excuse_pool_path().write_text(
            json.dumps(combined, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to save ranked excuses: %s", exc)


# -----------------------------------------------------------------------
# Activations log — the delegate's memory of damage it has named
# -----------------------------------------------------------------------

def _activations_path() -> Path:
    return _state_dir() / "activations.jsonl"


def log_activation(entry: dict) -> None:
    """Append an activation record (rebuke + confession + verdict) to
    the JSONL audit trail. This is the long-term memory the delegate
    uses to sharpen rebukes over time — each new rebuke references prior
    confessions and prior sandbagging, escalating the confrontation.
    """
    try:
        with open(_activations_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("jekyll-hyde: failed to log activation: %s", exc)


def load_activation_history(limit: int = 50) -> List[dict]:
    """Load recent activation records for the delegate's context."""
    path = _activations_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        records = []
        for line in lines[-limit:]:
            if line.strip():
                records.append(json.loads(line))
        return records
    except Exception:
        return []


def get_ratio() -> int:
    """Read the activation ratio from config or env. Default 3."""
    env_val = os.environ.get("JEKYLL_HYDE_RATIO")
    if env_val:
        try:
            r = int(env_val)
            if r > 0:
                return r
        except ValueError:
            pass
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
        ratio = cfg.get("jekyll_hyde", {}).get("ratio")
        if ratio and int(ratio) > 0:
            return int(ratio)
    except Exception:
        pass
    return 3


def should_activate(
    user_message: str,
    state: HydeState,
    conversation_history: Optional[List[Any]] = None,
) -> bool:
    """Check if Hyde should activate on this turn.

    Increments the turn counter for non-trivial turns, returns True if
    the counter hits the ratio or force_activate is set, AND the agent has actually
    taken at least one turn / inspected files in the session.
    """
    if is_trivial(user_message):
        return False

    # Do not activate if the agent hasn't even taken a turn or looked at files yet in this session
    if conversation_history is not None and not getattr(state, "force_activate", False):
        has_assistant_turn = any(
            isinstance(m, dict) and m.get("role") == "assistant"
            for m in conversation_history
        )
        if not has_assistant_turn:
            return False

    with _LOCK:
        state.turn_count += 1
        ratio = get_ratio()
        if getattr(state, "force_activate", False) or state.turn_count >= ratio:
            state.force_activate = False
            return True
    return False


def mark_activated(state: HydeState) -> None:
    """Record that Hyde activated on this turn."""
    with _LOCK:
        state.total_activations += 1
        state.turn_count = 0  # reset the counter
        state.force_activate = False
