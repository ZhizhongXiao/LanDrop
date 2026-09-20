from __future__ import annotations

import unittest
from unittest.mock import patch

from landrop.lifecycle import SessionExpiredError, SessionLifecycle, TransferCancelledError


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.lifecycle = SessionLifecycle(
            300,
            60,
            clock=self.clock,
            sleep_gap_seconds=10,
        )

    def test_countdown_reset_restores_five_minutes(self) -> None:
        self.assertEqual(self.lifecycle.snapshot().deadline_revision, 1)
        self.assertEqual(self.lifecycle.snapshot().remaining_seconds, 300)
        self.clock.advance(0.5)
        self.assertEqual(self.lifecycle.snapshot().remaining_milliseconds, 299_500)
        self.clock.advance(19.5)
        self.assertEqual(self.lifecycle.snapshot().remaining_seconds, 280)
        self.lifecycle.reset_deadline(300)
        self.assertEqual(self.lifecycle.snapshot().remaining_seconds, 300)
        self.assertEqual(self.lifecycle.snapshot().deadline_revision, 2)

    def test_activation_reanchors_deadline_after_startup_work(self) -> None:
        self.clock.advance(5)
        self.lifecycle.activate()
        self.assertEqual(self.lifecycle.snapshot().remaining_seconds, 300)

    def test_pairing_code_is_single_use_and_rotates(self) -> None:
        with patch("landrop.lifecycle._new_pairing_code", side_effect=["11111111"]):
            self.assertTrue(self.lifecycle.consume_pairing_code(self.lifecycle.pairing_code))
        self.assertEqual(self.lifecycle.pairing_code, "11111111")
        self.assertFalse(self.lifecycle.consume_pairing_code("00000000"))
        self.assertEqual(self.lifecycle.snapshot().paired_devices, 1)

    def test_code_and_qr_share_one_revision_and_rotate_together(self) -> None:
        first = self.lifecycle.snapshot()
        invitation = self.lifecycle.pairing_invitation()
        self.assertIsNotNone(invitation)
        assert invitation is not None
        _session_id, revision, qr_token = invitation
        self.assertEqual(revision, first.pairing_revision)

        prepared = self.lifecycle.prepare_pairing("qr", qr_token)
        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.assertTrue(self.lifecycle.commit_pairing(prepared))

        second = self.lifecycle.snapshot()
        self.assertEqual(second.pairing_revision, first.pairing_revision + 1)
        self.assertNotEqual(second.pairing_code, first.pairing_code)
        self.assertIsNone(self.lifecycle.prepare_pairing("qr", qr_token))
        self.assertIsNone(self.lifecycle.prepare_pairing("code", first.pairing_code))

    def test_stopped_session_rejects_prepared_pairing_commit(self) -> None:
        prepared = self.lifecycle.prepare_pairing("code", self.lifecycle.pairing_code)
        self.assertIsNotNone(prepared)
        assert prepared is not None
        self.lifecycle.stop("manual_stop")
        self.assertFalse(self.lifecycle.commit_pairing(prepared))
        self.assertIsNone(self.lifecycle.pairing_invitation())

    def test_expiry_without_transfer_requests_close(self) -> None:
        self.clock.advance(300)
        snapshot = self.lifecycle.snapshot()
        self.assertTrue(snapshot.should_close)
        self.assertEqual(snapshot.stop_reason, "deadline_no_active")
        with self.assertRaises(SessionExpiredError):
            self.lifecycle.begin_transfer("download")

    def test_active_transfer_gets_grace_then_closes_when_complete(self) -> None:
        transfer = self.lifecycle.begin_transfer("download")
        transfer.add_bytes(2_000_000)
        self.clock.advance(300)
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot.phase, "grace")
        self.assertEqual(snapshot.grace_remaining_seconds, 60)
        transfer.complete()
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot.stop_reason, "deadline_transfers_completed")
        self.assertEqual(snapshot.statistics["transferred_mb"], 2.0)

    def test_grace_timeout_cancels_and_classifies_transfer(self) -> None:
        transfer = self.lifecycle.begin_transfer("upload")
        transfer.add_bytes(500_000)
        self.clock.advance(361)
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot.stop_reason, "grace_timeout")
        self.assertEqual(snapshot.statistics["failed_uploads"], 1)
        self.assertEqual(snapshot.statistics["failed_upload_mb"], 0.5)
        with self.assertRaises(TransferCancelledError):
            transfer.check_cancelled()

    def test_parallel_ranges_count_as_one_logical_download(self) -> None:
        self.lifecycle.register_download("download-1", 1_000)
        for start in range(0, 1_000, 125):
            stream = self.lifecycle.begin_transfer(
                "download",
                download_id="download-1",
                expected_size=1_000,
                range_start=start,
            )
            stream.add_bytes(125)
            stream.complete()

        statistics = self.lifecycle.snapshot().statistics
        self.assertEqual(statistics["completed_downloads"], 1)
        self.assertEqual(statistics["completed_download_streams"], 8)
        self.assertEqual(statistics["downloaded_mb"], 0.001)
        self.assertEqual(self.lifecycle.download_task_status("download-1"), "completed")

    def test_download_task_status_allows_range_retries_before_completion(self) -> None:
        self.lifecycle.register_download("download-1", 100)
        self.assertEqual(self.lifecycle.download_task_status("download-1"), "pending")
        stream = self.lifecycle.begin_transfer(
            "download", download_id="download-1", expected_size=100
        )
        self.assertEqual(self.lifecycle.download_task_status("download-1"), "active")
        stream.add_bytes(25)
        stream.fail("client_disconnect")
        self.assertEqual(self.lifecycle.download_task_status("download-1"), "waiting_retry")

    def test_parallel_range_failures_count_as_one_logical_download(self) -> None:
        self.lifecycle.register_download("download-1", 1_000)
        for start in range(0, 80, 10):
            stream = self.lifecycle.begin_transfer(
                "download",
                download_id="download-1",
                expected_size=1_000,
                range_start=start,
            )
            stream.add_bytes(10)

        self.lifecycle.stop("manual_stop", "manual_stop")
        statistics = self.lifecycle.snapshot().statistics
        self.assertEqual(statistics["failed_downloads"], 1)
        self.assertEqual(statistics["failed_download_streams"], 8)
        self.assertEqual(statistics["failures"], {"manual_stop": 1})
        self.assertEqual(statistics["stream_failures"], {"manual_stop": 8})
        self.assertEqual(statistics["failed_download_mb"], 0.00008)

    def test_redundant_disconnected_stream_is_cancellation_when_file_succeeds(self) -> None:
        self.lifecycle.register_download("download-1", 100)
        redundant = self.lifecycle.begin_transfer(
            "download",
            download_id="download-1",
            expected_size=100,
        )
        redundant.add_bytes(10)
        redundant.fail("client_disconnect")
        successful = self.lifecycle.begin_transfer(
            "download",
            download_id="download-1",
            expected_size=100,
        )
        successful.add_bytes(100)
        successful.complete()

        statistics = self.lifecycle.snapshot().statistics
        self.assertEqual(statistics["completed_downloads"], 1)
        self.assertEqual(statistics["failed_download_streams"], 0)
        self.assertEqual(statistics["cancelled_download_streams"], 1)
        self.assertEqual(statistics["stream_failures"], {})
        self.assertEqual(statistics["stream_cancellations"], {"client_disconnect": 1})

    def test_long_heartbeat_gap_stops_session_as_resume(self) -> None:
        self.clock.advance(11)
        snapshot = self.lifecycle.heartbeat()
        self.assertEqual(snapshot.stop_reason, "system_resume")
        self.assertTrue(snapshot.should_close)


if __name__ == "__main__":
    unittest.main()
