from __future__ import annotations

import os
from pathlib import Path
import threading
import unittest
import uuid

from landrop.single_instance import DesktopSingleInstance
from support import temporary_directory


@unittest.skipUnless(os.name == "nt", "Windows-only single-instance behavior")
class DesktopSingleInstanceTests(unittest.TestCase):
    def test_second_instance_activates_primary_without_taking_ownership(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            mutex_name = rf"Local\LanDrop.Test.{uuid.uuid4().hex}"
            primary = DesktopSingleInstance(root, mutex_name=mutex_name)
            secondary = DesktopSingleInstance(root, mutex_name=mutex_name)
            activated = threading.Event()
            try:
                self.assertTrue(primary.acquire())
                primary.set_activate_callback(activated.set)
                self.assertFalse(secondary.acquire())
                self.assertTrue(secondary.notify_existing())
                self.assertTrue(activated.wait(1.0))
            finally:
                secondary.close()
                primary.close()
            self.assertFalse((root / "desktop-instance.json").exists())


if __name__ == "__main__":
    unittest.main()
