from __future__ import annotations

import unittest

from landrop.diagnostics import _classify_firewall, _port_scope


class FirewallDiagnosticTests(unittest.TestCase):
    def test_classifies_exact_port_and_program_allow_evidence(self) -> None:
        result = _classify_firewall(
            {
                "profiles": [
                    {"name": "Private", "enabled": True, "default_inbound": "Block"}
                ],
                "rules": [
                    {
                        "display_name": "LanDrop TCP 8000",
                        "profile": "Private",
                        "action": "Allow",
                        "program": r"C:\Python\python.exe",
                        "protocol": "TCP",
                        "local_port": "8000",
                    }
                ],
            },
            8000,
            r"C:\Python\python.exe",
        )

        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["exact_port_allow"], 1)
        self.assertEqual(result["program_allow"], 1)
        self.assertEqual(result["relevant_blocks"], 0)

    def test_relevant_block_takes_precedence_over_allow(self) -> None:
        result = _classify_firewall(
            {
                "profiles": [{"name": "Private", "enabled": True}],
                "rules": [
                    {
                        "display_name": "Allow target",
                        "profile": "Private",
                        "action": "Allow",
                        "program": "Any",
                        "protocol": "TCP",
                        "local_port": "8000",
                    },
                    {
                        "display_name": "Block target range",
                        "profile": "Any",
                        "action": "Block",
                        "program": "Any",
                        "protocol": "TCP",
                        "local_port": "7990-8010",
                    },
                ],
            },
            8000,
            r"C:\Python\python.exe",
        )

        self.assertEqual(result["level"], "warning")
        self.assertEqual(result["relevant_blocks"], 1)

    def test_unrelated_program_rule_is_ignored(self) -> None:
        result = _classify_firewall(
            {
                "profiles": [{"name": "Private", "enabled": True}],
                "rules": [
                    {
                        "display_name": "Other Python",
                        "profile": "Private",
                        "action": "Allow",
                        "program": r"C:\Other\python.exe",
                        "protocol": "TCP",
                        "local_port": "8000",
                    }
                ],
            },
            8000,
            r"C:\Python\python.exe",
        )

        self.assertEqual(result["exact_port_allow"], 0)
        self.assertEqual(result["program_allow"], 0)
        self.assertEqual(result["level"], "warning")

    def test_port_scope_handles_exact_range_and_any(self) -> None:
        self.assertEqual(_port_scope("8000", 8000), "exact")
        self.assertEqual(_port_scope("7999-8001", 8000), "broad")
        self.assertEqual(_port_scope("Any", 8000), "any")
        self.assertEqual(_port_scope("443", 8000), "none")


if __name__ == "__main__":
    unittest.main()
