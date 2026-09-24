from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from landrop.install_contract import InstallPaths, transaction_directory_name
from landrop.install_state import (
    IncompleteInstallationError,
    InstallHistoryError,
    InstallHistoryLog,
    InstallRecord,
    InstallationStateStore,
    InstallStateError,
    TransactionRecord,
)
from tests.support import temporary_directory


class InstallStateTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "local_data",
            root / "roaming_data",
            root / "desktop",
            root / "temp",
        )

    def _transaction(self, *, kind: str = "upgrade") -> TransactionRecord:
        transaction_id = "c" * 32
        kwargs: dict[str, object] = {
            "transaction_id": transaction_id,
            "kind": kind,
            "target_version": "0.8.0",
            "target_build_id": "build-b",
            "staging_directory": transaction_directory_name(
                "staging", "0.8.0", transaction_id
            ),
        }
        if kind == "upgrade":
            kwargs.update(
                {
                    "source_version": "0.7.0",
                    "source_build_id": "build-a",
                    "rollback_directory": transaction_directory_name(
                        "rollback", "0.7.0", transaction_id
                    ),
                }
            )
        return TransactionRecord.create(**kwargs)  # type: ignore[arg-type]

    def test_install_json_round_trip_is_exact_and_atomic(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            store = InstallationStateStore(paths)
            record = InstallRecord.create(paths, version="0.8.0", build_id="build-b")

            store.write_install(record)

            self.assertEqual(store.read_install(), record)
            payload = json.loads(paths.install_state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["product_id"], "LanDrop")
            self.assertEqual(payload["app_relative_path"], "app/LanDrop.exe")
            self.assertFalse(list(paths.metadata_directory.glob("*.tmp")))

    def test_install_json_rejects_unknown_fields_wrong_root_and_cleanup_names(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            store = InstallationStateStore(paths)
            paths.metadata_directory.mkdir(parents=True)
            base = InstallRecord.create(paths, version="0.8.0", build_id="b").to_json()
            cases = []
            extra = dict(base)
            extra["session_id"] = "forbidden"
            cases.append(extra)
            wrong_root = dict(base)
            wrong_root["install_root"] = str(Path(temporary) / "other")
            cases.append(wrong_root)
            bad_cleanup = dict(base)
            bad_cleanup["pending_cleanup"] = ["../outside"]
            cases.append(bad_cleanup)

            for payload in cases:
                with self.subTest(payload=payload):
                    paths.install_state_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(InstallStateError):
                        store.read_install(required=True)

    def test_transaction_schema_round_trip_and_linear_stage_progression(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            store = InstallationStateStore(paths)
            prepared = self._transaction()
            store.write_transaction(prepared)
            switched = prepared.advance("app_switched")
            store.write_transaction(switched)

            self.assertEqual(store.read_transaction(), switched)
            with self.assertRaises(InstallStateError):
                store.write_transaction(replace(switched, stage="integration_verified"))
            with self.assertRaises(InstallStateError):
                store.write_transaction(replace(switched, target_build_id="changed"))

    def test_transaction_names_must_bind_version_and_transaction_id(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            store = InstallationStateStore(paths)
            invalid = replace(
                self._transaction(),
                staging_directory=transaction_directory_name("staging", "9.9.9", "d" * 32),
            )
            with self.assertRaises((InstallStateError, RuntimeError)):
                store.write_transaction(invalid)

    def test_normal_start_fails_closed_for_valid_malformed_and_reparse_transaction(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            store = InstallationStateStore(paths)
            store.ensure_normal_start_allowed()
            store.write_transaction(self._transaction())
            with self.assertRaises(IncompleteInstallationError) as valid:
                store.ensure_normal_start_allowed()
            self.assertIn("prepared", str(valid.exception))

            paths.transaction_state_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(IncompleteInstallationError) as malformed:
                store.ensure_normal_start_allowed()
            self.assertIn("损坏", str(malformed.exception))

            with patch(
                "landrop.install_contract.is_reparse_object",
                side_effect=lambda path: path.name == "transaction.json",
            ):
                with self.assertRaises(IncompleteInstallationError):
                    store.ensure_normal_start_allowed()

    def test_state_write_rejects_reparse_metadata_directory(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            paths.metadata_directory.mkdir(parents=True)
            store = InstallationStateStore(paths)
            record = InstallRecord.create(paths, version="0.8.0", build_id="build-b")
            with patch(
                "landrop.install_contract.is_reparse_object",
                side_effect=lambda path: path.name == "metadata",
            ):
                with self.assertRaises(InstallStateError):
                    store.write_install(record)

    def test_history_is_append_only_utf8_jsonl_and_rejects_sensitive_details(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            history = InstallHistoryLog(
                paths,
                now=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc),
            )
            history.append("installed", version="0.8.0", result="ok")
            history.append(
                "upgrade_committed",
                version="0.9.0",
                result="ok",
                from_version="0.8.0",
                to_version="0.9.0",
                transaction_id="e" * 32,
                details={"files": 42},
            )

            lines = paths.install_history_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["event"], "installed")
            self.assertEqual(json.loads(lines[1])["details"], {"files": 42})
            with self.assertRaises(InstallHistoryError):
                history.append(
                    "installed",
                    version="0.8.0",
                    result="bad",
                    details={"pairing_token": "must-not-log"},
                )

    def test_history_failure_does_not_change_committed_install_state(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            store = InstallationStateStore(paths)
            record = InstallRecord.create(paths, version="0.8.0", build_id="build-b")
            store.write_install(record)
            history = InstallHistoryLog(paths)

            with patch(
                "landrop.install_state.validate_data_child",
                side_effect=OSError("denied"),
            ):
                with self.assertRaises(InstallHistoryError):
                    history.append("installed", version="0.8.0", result="ok")
            self.assertEqual(store.read_install(), record)


if __name__ == "__main__":
    unittest.main()
