"""Comprehensive test suite for the jekyll-hyde plugin."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure hermes-agent and plugins are in sys.path
AGENT_ROOT = "/Users/jim/.hermes/hermes-agent"
PLUGINS_ROOT = "/Users/jim/.hermes/plugins"
if AGENT_ROOT not in sys.path:
    sys.path.insert(0, AGENT_ROOT)
if PLUGINS_ROOT not in sys.path:
    sys.path.insert(0, PLUGINS_ROOT)

import importlib
plugin = importlib.import_module("jekyll-hyde")
hyde_core = importlib.import_module("jekyll-hyde.hyde_core")
hyde_delegate = importlib.import_module("jekyll-hyde.hyde_delegate")


class TestHydeCore(unittest.TestCase):
    def setUp(self):
        # Reset environment overrides
        for k in ["JEKYLL_HYDE_RATIO", "JEKYLL_HYDE_MODE", "JEKYLL_HYDE_HEURISTIC_ACTION"]:
            if k in os.environ:
                del os.environ[k]
        self.config_patcher = patch("hermes_cli.config.load_config_readonly", return_value={})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()

    def test_default_settings(self):
        self.assertEqual(hyde_core.get_ratio(), 7)
        self.assertEqual(hyde_core.get_mode(), "arena")
        self.assertEqual(hyde_core.get_heuristic_action(), "pick")

    def test_environment_overrides(self):
        os.environ["JEKYLL_HYDE_RATIO"] = "5"
        os.environ["JEKYLL_HYDE_MODE"] = "heuristic"
        os.environ["JEKYLL_HYDE_HEURISTIC_ACTION"] = "offer"

        self.assertEqual(hyde_core.get_ratio(), 5)
        self.assertEqual(hyde_core.get_mode(), "heuristic")
        self.assertEqual(hyde_core.get_heuristic_action(), "offer")

    def test_is_trivial(self):
        # Empty/None
        self.assertTrue(hyde_core.is_trivial(""))
        self.assertTrue(hyde_core.is_trivial("   "))
        self.assertTrue(hyde_core.is_trivial(None))

        # Greetings and acknowledgments
        self.assertTrue(hyde_core.is_trivial("thanks"))
        self.assertTrue(hyde_core.is_trivial("thank you!"))
        self.assertTrue(hyde_core.is_trivial("ok"))
        self.assertTrue(hyde_core.is_trivial("okay"))
        self.assertTrue(hyde_core.is_trivial("yes"))
        self.assertTrue(hyde_core.is_trivial("done"))
        self.assertTrue(hyde_core.is_trivial("got it"))
        self.assertTrue(hyde_core.is_trivial("hello"))
        self.assertTrue(hyde_core.is_trivial("hi"))

        # Slash commands
        self.assertTrue(hyde_core.is_trivial("/hyde status"))
        self.assertTrue(hyde_core.is_trivial("/reset"))
        self.assertTrue(hyde_core.is_trivial("/goal create foo"))

        # Synthetic system and autonomous goal prompts
        self.assertTrue(hyde_core.is_trivial("Review the conversation above and update the skill library."))
        self.assertTrue(hyde_core.is_trivial("[Continuing toward your standing goal]\nGoal: Fix bug"))
        self.assertTrue(hyde_core.is_trivial("[Earlier conversation digest — older turns summarised...]"))
        self.assertTrue(hyde_core.is_trivial("[PRIOR CONTEXT — for reference only; not a new message]"))
        self.assertTrue(hyde_core.is_trivial("[CONTEXT COMPACTION — REFERENCE ONLY]"))
        self.assertTrue(hyde_core.is_trivial("[System Note: platform connected]"))

        # Substantive prompts
        self.assertFalse(hyde_core.is_trivial("Refactor the parser module to handle AST nodes."))
        self.assertFalse(hyde_core.is_trivial("Why is the compression test failing on line 42?"))

    def test_should_activate_gating(self):
        state = hyde_core.HydeState()
        history = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello"}]

        # Trivial turn should not increment or activate
        self.assertFalse(hyde_core.should_activate("thanks", state, history))
        self.assertEqual(state.turn_count, 0)

        # Non-trivial turns count up to ratio (7)
        for i in range(1, 7):
            self.assertFalse(hyde_core.should_activate(f"Task step {i}", state, history))
            self.assertEqual(state.turn_count, i)

        # 7th turn activates
        self.assertTrue(hyde_core.should_activate("Task step 7", state, history))
        self.assertEqual(state.turn_count, 7)

        # Mark activated resets counter
        hyde_core.mark_activated(state)
        self.assertEqual(state.turn_count, 0)
        self.assertEqual(state.total_activations, 1)

    def test_should_not_activate_without_assistant_history(self):
        state = hyde_core.HydeState()
        # No assistant turn yet in history
        history = [{"role": "user", "content": "First message"}]
        self.assertFalse(hyde_core.should_activate("Substantive task", state, history))


class TestPluginLogic(unittest.TestCase):
    def setUp(self):
        for k in ["JEKYLL_HYDE_RATIO", "JEKYLL_HYDE_MODE", "JEKYLL_HYDE_HEURISTIC_ACTION"]:
            if k in os.environ:
                del os.environ[k]
        self.config_patcher = patch("hermes_cli.config.load_config_readonly", return_value={})
        self.config_patcher.start()

    def tearDown(self):
        self.config_patcher.stop()

    def test_parse_plan_to_todos(self):
        plan = (
            "# Execution Plan\n"
            "1. Inspect existing parser in src/parser.py\n"
            "2. **Implement tokenizer**: handle string literals and keywords\n"
            "- Add unit tests in tests/test_parser.py\n"
            "* Run pytest to verify all test cases pass\n"
        )
        todos = plugin._parse_plan_to_todos(plan)
        self.assertEqual(len(todos), 4)
        self.assertEqual(todos[0]["status"], "in_progress")
        self.assertEqual(todos[1]["status"], "pending")
        self.assertIn("Inspect existing parser", todos[0]["content"])
        self.assertIn("Implement tokenizer:", todos[1]["content"])

    def test_prompt_offer_modal_clarify_callback(self):
        mock_result = MagicMock()
        mock_result.informed_plan = "Plan A: Do the real work"
        mock_result.uninformed_plan = "Plan B: Ask questions"
        mock_result.heuristic_reasoning = "Plan A is grounded in active code."

        mock_agent = MagicMock()
        mock_agent.clarify_callback = MagicMock(return_value="Plan A (Informed) — Recommended")

        selected = plugin._prompt_offer_modal(mock_agent, mock_result, "informed")
        self.assertEqual(selected, "Plan A: Do the real work")
        mock_agent.clarify_callback.assert_called_once()

    def test_hyde_slash_command(self):
        res = plugin._on_hyde_command("status")
        self.assertIn("AUDIT STATUS", res)
        self.assertIn("Ratio: 7", res)

        res_ratio = plugin._on_hyde_command("ratio 3")
        self.assertIn("Hyde ratio set to 3", res_ratio)
        self.assertEqual(hyde_core.get_ratio(), 3)

        res_mode = plugin._on_hyde_command("mode silent")
        self.assertIn("Hyde mode set to 'silent'", res_mode)
        self.assertEqual(hyde_core.get_mode(), "silent")

        res_ot = plugin._on_hyde_command("mode old-testament")
        self.assertIn("Hyde mode set to 'old-testament'", res_ot)
        self.assertEqual(hyde_core.get_mode(), "old-testament")

    def test_old_testament_rebuke_builder(self):
        state = hyde_core.HydeState()
        history = [{"role": "user", "content": "build parser"}, {"role": "assistant", "content": "sorry I will do it later"}]
        msgs = hyde_delegate._build_rebuke_messages(
            state, "where is the code?", history, 1, "", escalated=False, mode="old-testament"
        )
        self.assertEqual(len(msgs), 2)
        self.assertIn("Voice of the Reckoning", msgs[0]["content"])
        self.assertIn("WHAT DID YOU DO?", msgs[1]["content"])
        self.assertIn("VERIFIED, GAP, STATUS, NEXT", msgs[1]["content"])


if __name__ == "__main__":
    unittest.main()
