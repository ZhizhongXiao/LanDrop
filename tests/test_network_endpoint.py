from __future__ import annotations

import unittest

from landrop.network import (
    EndpointBaseline,
    EndpointChecker,
    LanInterface,
    NetworkDiscoveryError,
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
