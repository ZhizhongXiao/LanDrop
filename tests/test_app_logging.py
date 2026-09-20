from __future__ import annotations

import logging
from pathlib import Path
import unittest

from landrop.app_logging import configure_application_logging
from support import temporary_directory


class ApplicationLoggingTests(unittest.TestCase):
    def test_writes_utf8_application_log_in_user_data_directory(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            logger = configure_application_logging(root, debug=False)

            logger.info("中文诊断")
            for handler in logger.handlers:
                handler.flush()

            log_path = root / "logs" / "application.log"
            self.assertTrue(log_path.is_file())
            self.assertIn("中文诊断", log_path.read_text(encoding="utf-8"))
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
