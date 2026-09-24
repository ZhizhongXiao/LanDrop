"""Assemble the exact Phase 8B payload manifest for Setup.spec."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from landrop.payload_manifest import build_payload_manifest, write_payload_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-id", required=True)
    args = parser.parse_args()
    manifest = build_payload_manifest(
        args.payload,
        version=args.version,
        build_id=args.build_id,
    )
    write_payload_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
