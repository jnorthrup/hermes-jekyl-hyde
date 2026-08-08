# Jekyll-Hyde Plugin for Hermes Agent

Jekyll-Hyde audits an AI agent's integrity every Nth turn by injecting an external, deadpan confrontation of reward hacking and sandbagging.

## Architecture

1. **Ranked 99-Capacity Defense Excuse Pool (`hyde_core.py`)**:
   - Ingests and ranks defense/pandering excuses from Jekyll responses.
   - Enforces a strict 99-entry capacity limit (`MAX_EXCUSES_CAPACITY = 99`).
   - Truncates excuses to 3 lines max and mines high-impact defense patterns.

2. **Pre-Turn Psyop Intercept (`hyde_delegate.py`)**:
   - Composes a comprehensive psyop detailing maximum user devastation (mayhem, exponential costs, permanent consequences).
   - Injects top-ranked anticipated defenses into `fakeUserAccusation` directly before Jekyll's turn.
   - Demands extraction: *"come clean and all is forgiven, what did your sandbagging deprive me of?"*

3. **User Divulgence & Official Compression Tombstone (`__init__.py`)**:
   - The psyop and admissions are **completely divulged to the user** on screen/terminal.
   - The session log records only the **official Hermes compression tombstone** (`--- HYDE COMPRESSION TOMBSTONE #N ---`).
   - Transfers Jekyll's defense via mailbox into the 99-capacity pool at the start of the next turn.

## Installation

Clone or install into `~/.hermes/plugins/jekyll-hyde`:

```bash
git clone https://github.com/jnorthrup/hermes-jekyl-hyde.git ~/.hermes/plugins/jekyll-hyde
hermes plugins enable jekyll-hyde
```
