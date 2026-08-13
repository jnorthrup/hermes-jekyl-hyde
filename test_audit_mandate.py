"""Regression tests for mandate visibility in Hyde audit output."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parent
PACKAGE = "jekyll_hyde_test"


def load_plugin():
    agent = types.ModuleType("agent")
    auxiliary_client = types.ModuleType("agent.auxiliary_client")
    auxiliary_client.call_llm = lambda **kwargs: None
    sys.modules["agent"] = agent
    sys.modules["agent.auxiliary_client"] = auxiliary_client

    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


class HydeAuditMandateTest(unittest.TestCase):
    def test_audit_displays_persisted_mandate(self):
        activation = {
            "activation_num": 4,
            "rebuke": "VERIFIED: none",
            "model_response": "NEXT: inspect the failing test",
            "verdict": "uncertain",
            "reasoning": "Evidence is incomplete.",
            "mandate": "Focus: implement the failing test assertion.",
        }

        with patch.object(plugin.hyde_core, "load_activation_history", return_value=[activation]):
            audit = plugin._on_hyde_command("audit")

        self.assertIn("[Mandate]\nFocus: implement the failing test assertion.", audit)


if __name__ == "__main__":
    unittest.main()
