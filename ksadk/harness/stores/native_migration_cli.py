"""Explicit offline Native Store inspection and migration-copy entry point."""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from ksadk.harness.native.state import RunScope

from .native_contract import StoreConflict
from .native_sqlite_migration import (
    READER_VERSION,
    LegacySessionHead,
    export_native_store,
    inspect_native_store,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查或离线导出 Native SQLite 迁移副本")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="只读检查 schema、摘要和 Session head 候选")
    inspect.add_argument("source", type=Path)
    export = commands.add_parser("export", help="根据明确 head 选择生成新副本，保留原库")
    export.add_argument("source", type=Path)
    export.add_argument("destination", type=Path)
    export.add_argument("--expected-source-digest", required=True)
    export.add_argument("--heads-json", type=Path)
    export.add_argument(
        "--offline",
        action="store_true",
        required=True,
        help="确认已停止所有使用原库的进程；不是热迁移",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_native_store(args.source)
            output = {
                "reader_version": READER_VERSION,
                "schema_version": result.schema_version,
                "source_digest": result.source_digest,
                "sessions": [
                    {
                        "scope": item.scope.model_dump(mode="json"),
                        "session_id": item.session_id,
                        "run_ids": item.run_ids,
                        "pinned_run_id": item.pinned_run_id,
                    }
                    for item in result.sessions
                ],
            }
        else:
            choices = json.loads(args.heads_json.read_text()) if args.heads_json else []
            if not isinstance(choices, list) or any(
                not isinstance(item, dict)
                or set(item) != {"scope", "session_id", "run_id"}
                or not isinstance(item["session_id"], str)
                or not isinstance(item["run_id"], str)
                for item in choices
            ):
                raise ValueError("heads JSON 必须为明确 scope/session_id/run_id 的数组")
            output = asdict(
                export_native_store(
                    args.source,
                    args.destination,
                    expected_source_digest=args.expected_source_digest,
                    session_heads=[
                        LegacySessionHead(
                            RunScope.model_validate(item["scope"]),
                            item["session_id"],
                            item["run_id"],
                        )
                        for item in choices
                    ],
                    offline_acknowledged=args.offline,
                )
            )
    except (StoreConflict, ValueError, OSError, sqlite3.Error) as error:
        # Administrative diagnostics contain no snapshot bodies or credentials.
        reason = str(error) if isinstance(error, StoreConflict) else type(error).__name__
        print(f"Native store migration rejected: {reason}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
