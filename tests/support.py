from __future__ import annotations

import tempfile
from pathlib import Path

TEST_TEMP_ROOT = Path(__file__).resolve().parent.parent / ".test-tmp"


def temporary_directory() -> tempfile.TemporaryDirectory[str]:
    TEST_TEMP_ROOT.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
