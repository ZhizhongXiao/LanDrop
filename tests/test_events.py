from __future__ import annotations

from pathlib import Path
import unittest

from landrop.events import SessionEventLog
from landrop.lifecycle import SessionLifecycle
from support import temporary_directory


class SessionEventLogTests(unittest.TestCase):
    def test_rotates_and_limits_backup_files(self) -> None:
        with temporary_directory() as temporary:
            event_log = SessionEventLog(
                Path(temporary),
                max_bytes=250,
                backup_count=3,
            )
            lifecycle = SessionLifecycle()
            lifecycle.stop("manual_stop")
            snapshot = lifecycle.snapshot()

            for _index in range(10):
                event_log.record(
                    snapshot,
                    started_at="2026-09-16T00:00:00+00:00",
                    duration_seconds=300,
                    grace_seconds=60,
                )

            files = sorted(event_log.directory.glob("sessions.jsonl*"))
            self.assertGreaterEqual(len(files), 2)
            self.assertLessEqual(len(files), 4)
            combined = "".join(path.read_text(encoding="utf-8") for path in files)
            self.assertNotIn("pairing_code", combined)
            self.assertNotIn("credentials", combined)


if __name__ == "__main__":
    unittest.main()
