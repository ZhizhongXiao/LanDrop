"""File logging for the windowed LanDrop application."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
from types import TracebackType
from typing import Callable


LOGGER_NAME = "landrop"
LOG_FILENAME = "application.log"


def configure_application_logging(data_directory: Path, *, debug: bool = False) -> logging.Logger:
    log_directory = Path(data_directory) / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        log_directory / LOG_FILENAME,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.info("LanDrop application logging initialized")
    return logger


def install_exception_hooks(logger: logging.Logger) -> Callable[[], None]:
    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def sys_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        logger.critical(
            "Unhandled application exception",
            exc_info=(exception_type, exception, traceback),
        )

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.error(
            "Unhandled thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook

    def restore() -> None:
        sys.excepthook = previous_sys_hook
        threading.excepthook = previous_thread_hook

    return restore
