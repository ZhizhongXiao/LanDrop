from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
import uuid

from landrop.install_lock import InstallLifecycleLock, InstallLifecycleLockError


@unittest.skipUnless(os.name == "nt", "Windows-only install lifecycle mutex")
class InstallLifecycleLockTests(unittest.TestCase):
    def test_other_thread_cannot_enter_until_owner_releases(self) -> None:
        name = rf"Global\LanDrop.InstallLifecycle.Test.{uuid.uuid4().hex}"
        primary = InstallLifecycleLock(mutex_name=name)
        self.assertTrue(primary.acquire(0.0))
        result: list[bool] = []

        def contend() -> None:
            secondary = InstallLifecycleLock(mutex_name=name)
            try:
                result.append(secondary.acquire(0.05))
            finally:
                secondary.close()

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        primary.close()

        replacement = InstallLifecycleLock(mutex_name=name)
        try:
            self.assertTrue(replacement.acquire(0.1))
        finally:
            replacement.close()

    def test_same_lock_object_cannot_be_acquired_twice(self) -> None:
        lock = InstallLifecycleLock(
            mutex_name=rf"Global\LanDrop.InstallLifecycle.Test.{uuid.uuid4().hex}"
        )
        try:
            self.assertTrue(lock.acquire(0.0))
            with self.assertRaises(InstallLifecycleLockError):
                lock.acquire(0.0)
        finally:
            lock.close()

    def test_named_mutex_excludes_another_process(self) -> None:
        name = rf"Global\LanDrop.InstallLifecycle.Test.{uuid.uuid4().hex}"
        probe = (
            "import sys; "
            "from landrop.install_lock import InstallLifecycleLock; "
            "lock=InstallLifecycleLock(mutex_name=sys.argv[1]); "
            "acquired=lock.acquire(0.1); "
            "print('acquired' if acquired else 'blocked'); "
            "lock.close()"
        )
        primary = InstallLifecycleLock(mutex_name=name)
        try:
            self.assertTrue(primary.acquire(0.0))
            blocked = subprocess.run(
                [sys.executable, "-c", probe, name],
                cwd=Path(__file__).resolve().parent.parent,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(blocked.stdout.strip(), "blocked")
        finally:
            primary.close()

        acquired = subprocess.run(
            [sys.executable, "-c", probe, name],
            cwd=Path(__file__).resolve().parent.parent,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(acquired.stdout.strip(), "acquired")


if __name__ == "__main__":
    unittest.main()
