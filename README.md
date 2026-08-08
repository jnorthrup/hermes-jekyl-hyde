# Jekyll-Hyde Plugin for Hermes Agent

Context optimization and out-of-band evaluation plugin for Hermes Agent.

## Architecture

1. **Defense Memory Pool (`hyde_core.py`)**:
   - Maintains a ranked store of evasion patterns.
   - Enforces a strict 99-entry capacity limit (`MAX_EXCUSES_CAPACITY = 99`).

2. **Out-of-Band Evaluation (`hyde_delegate.py`)**:
   - Executes ephemeral evaluation cycles out-of-band to calibrate prompt execution.
   - Grounds instructions directly in session telemetry and tool history.

3. **Session Compaction (`__init__.py`)**:
   - Replaces intermediate evaluation exchanges with standard session compression tombstones.
   - Keeps the main transcript and prompt cache clean across turns.

## Installation

Install into `~/.hermes/plugins/jekyll-hyde`:

```bash
git clone https://github.com/jnorthrup/hermes-jekyl-hyde.git ~/.hermes/plugins/jekyll-hyde
hermes plugins enable jekyll-hyde
```
