from __future__ import annotations

import unittest
from unittest.mock import patch
import subprocess

from landrop.network import (
    EndpointBaseline,
    EndpointChecker,
    LanInterface,
    NetworkDiscoveryError,
    RUNTIME_CATEGORY_TIMEOUT_SECONDS,
    read_network_category,
    select_interface,
)


class EndpointCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = EndpointBaseline(
            interface_index=12,
            address="192.168.50.10",
            category="Private",
            alias="Ethernet",
        )

    def test_requires_matching_interface_index_and_ipv4(self) -> None:
        checker = EndpointChecker(
            self.baseline,
            address_reader=lambda: [
                ("192.168.50.10", 99),
                ("192.168.50.11", 12),
            ],
        )

        observation = checker.observe()

        self.assertFalse(observation.address_present)
        self.assertEqual(observation.error, "")

    def test_ignores_other_adapters_when_frozen_endpoint_still_exists(self) -> None:
        checker = EndpointChecker(
            self.baseline,
            address_reader=lambda: [
                ("10.0.0.4", 33),
                ("192.168.50.10", 12),
                ("198.18.0.1", 55),
            ],
            category_reader=lambda _index: "Private",
        )

        observation = checker.observe(include_category=True)

        self.assertTrue(observation.address_present)
        self.assertTrue(observation.category_private)

    def test_reader_failures_are_reported_as_unknown_observations(self) -> None:
        def fail():
            raise OSError("unavailable")

        checker = EndpointChecker(self.baseline, address_reader=fail)

        observation = checker.observe()

        self.assertIsNone(observation.address_present)
        self.assertIn("unavailable", observation.error)

    def test_category_is_only_read_when_requested(self) -> None:
        calls: list[int] = []
        checker = EndpointChecker(
            self.baseline,
            address_reader=lambda: [("192.168.50.10", 12)],
            category_reader=lambda index: calls.append(index) or "Public",
        )

        lightweight = checker.observe()
        categorized = checker.observe(include_category=True)

        self.assertEqual(calls, [12])
        self.assertIsNone(lightweight.category)
        self.assertTrue(categorized.explicitly_public)

    def test_runtime_category_queries_only_the_frozen_interface(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"alias":"Ethernet","interface_index":12,'
                '"category":"Private","connectivity":"Internet","name":"LAN"}'
            ),
            stderr="",
        )
        with (
            patch("landrop.network.subprocess.run", return_value=completed) as run,
            patch("landrop.network._read_connection_profiles_result") as full_reader,
            patch("landrop.network._read_wifi_registry_profile") as wifi_fallback,
        ):
            category = read_network_category(12, alias="WLAN")

        self.assertEqual(category, "Private")
        command = run.call_args.args[0]
        self.assertIn("Get-NetConnectionProfile -InterfaceIndex 12", command[-1])
        self.assertEqual(run.call_args.kwargs["timeout"], 4.0)
        self.assertEqual(RUNTIME_CATEGORY_TIMEOUT_SECONDS, 4.0)
        full_reader.assert_not_called()
        wifi_fallback.assert_not_called()

    def test_runtime_category_timeout_does_not_use_registry_fallback(self) -> None:
        with (
            patch(
                "landrop.network.subprocess.run",
                side_effect=subprocess.TimeoutExpired("powershell", 4.0),
            ),
            patch("landrop.network._read_wifi_registry_profile") as wifi_fallback,
        ):
            with self.assertRaisesRegex(NetworkDiscoveryError, "查询超时"):
                read_network_category(12, alias="WLAN")

        wifi_fallback.assert_not_called()

    def test_explicit_public_interface_is_rejected_before_start(self) -> None:
        public = LanInterface(
            alias="Ethernet",
            interface_index=7,
            address="192.168.50.1",
            category="Public",
            connectivity="LocalNetwork",
            has_gateway=False,
            description="Direct Ethernet",
        )

        with self.assertRaisesRegex(NetworkDiscoveryError, "Public.*拒绝启动"):
            select_interface([public], "192.168.50.1")


if __name__ == "__main__":
    unittest.main()
