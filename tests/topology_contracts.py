#!/usr/bin/env python3
"""Offline contracts for multi-role topology and named roles."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frontier_common as fc
import frontier_topology as ft


class TopologyContracts(unittest.TestCase):
    def test_project_topology_has_native_and_builtins(self):
        cfg = fc.defaults()
        topo = ft.project_topology(cfg)
        self.assertEqual(topo["schema"], "frontierfuse.topology.v1")
        self.assertFalse(topo["host"]["swappable_by_plugin"])
        self.assertIn("frontier", topo["native_slots"])
        self.assertIn("executor", topo["native_slots"])
        self.assertIn("frontier", topo["roles"])
        self.assertIn("executor", topo["roles"])
        self.assertFalse(topo["verifier"]["is_model"])

    def test_named_consult_role_resolves(self):
        cfg = fc.defaults()
        cfg["roles"] = {
            "orchestration_consult": {
                "kind": "consult",
                "provider": "codex",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
            }
        }
        binding = ft.resolve_role(cfg, "orchestration_consult")
        self.assertEqual(binding["provider"], "codex")
        self.assertEqual(binding["model"], "gpt-5.6-sol")
        self.assertEqual(binding["effort"], "xhigh")
        mapped = ft.cfg_for_role_consult(cfg, "orchestration_consult")
        self.assertEqual(mapped["frontier_provider"], "codex")
        self.assertEqual(mapped["frontier_model"], "gpt-5.6-sol")
        self.assertEqual(mapped["codex_effort"], "xhigh")

    def test_invalid_role_kind_rejected(self):
        with self.assertRaises(ValueError):
            ft.validate_role_binding({"kind": "wizard", "provider": "codex", "model": "x"})

    def test_invalid_provider_rejected(self):
        with self.assertRaises(ValueError):
            ft.validate_role_binding({"kind": "consult", "provider": "nope", "model": "x"})

    def test_body_role_cannot_consult(self):
        cfg = fc.defaults()
        cfg["roles"] = {
            "workers": {"kind": "body", "provider": "grok", "model": "grok-4.5"}
        }
        with self.assertRaises(ValueError):
            ft.cfg_for_role_consult(cfg, "workers")

    def test_openrouter_role_binding(self):
        cfg = fc.defaults()
        cfg["roles"] = {
            "cheap_worker": {
                "kind": "body",
                "provider": "openrouter",
                "model": "meta-llama/llama-4-maverick",
            }
        }
        mapped = ft.cfg_for_role_body(cfg, "cheap_worker")
        self.assertEqual(mapped["executor"], "openrouter")
        self.assertEqual(mapped["openrouter_model"], "meta-llama/llama-4-maverick")
        cmd = fc.build_body_command(mapped)
        self.assertIn("frontier_openrouter.py", " ".join(cmd))
        self.assertIn("meta-llama/llama-4-maverick", cmd)

    def test_resolve_config_accepts_roles(self):
        cfg = fc.resolve_config({
            "roles": {
                "orchestration_consult": {
                    "kind": "consult",
                    "provider": "codex",
                    "model": "gpt-5.6-sol",
                    "effort": "xhigh",
                }
            }
        })
        self.assertIn("orchestration_consult", cfg["roles"])

    def test_topology_cli_json_no_provider_call(self):
        env = os.environ.copy()
        # Isolate from user global/session config noise with temp config dir
        with tempfile.TemporaryDirectory() as td:
            env["FRONTIER_CONFIG_DIR"] = td
            env["FRONTIER_CONFIG"] = str(Path(td) / "config.json")
            env["FRONTIER_STATE_DIR"] = str(Path(td) / "state")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "frontier_dispatch.py"), "topology", "--json"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertEqual(data["schema"], "frontierfuse.topology.v1")


class OpenRouterContracts(unittest.TestCase):
    def test_openrouter_builder_uses_helper(self):
        cfg = fc.defaults()
        cfg["executor"] = "openrouter"
        cfg["openrouter_model"] = "openrouter/auto"
        cmd = fc.build_body_command(cfg)
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("frontier_openrouter.py"))
        self.assertIn("--prompt-file", cmd)

    def test_openrouter_dry_run_without_key(self):
        helper = ROOT / "frontier_openrouter.py"
        with tempfile.TemporaryDirectory() as td:
            pf = Path(td) / "p.txt"
            pf.write_text("hello", encoding="utf-8")
            env = os.environ.copy()
            env.pop("OPENROUTER_API_KEY", None)
            proc = subprocess.run(
                [sys.executable, str(helper), "--model", "openrouter/auto",
                 "--prompt-file", str(pf), "--dry-run"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(proc.stdout)
            self.assertTrue(data["dry_run"])
            self.assertFalse(data["key_present"])

    def test_openrouter_live_refuses_without_key(self):
        helper = ROOT / "frontier_openrouter.py"
        with tempfile.TemporaryDirectory() as td:
            pf = Path(td) / "p.txt"
            pf.write_text("hello", encoding="utf-8")
            env = os.environ.copy()
            env.pop("OPENROUTER_API_KEY", None)
            env.pop("FRONTIER_OPENROUTER_DRY_RUN", None)
            proc = subprocess.run(
                [sys.executable, str(helper), "--model", "openrouter/auto",
                 "--prompt-file", str(pf)],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("OPENROUTER_API_KEY", proc.stderr)


if __name__ == "__main__":
    unittest.main()
