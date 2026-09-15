"""Isolated Python entrypoint that applies rlimits before exec.

It is started with ``python -I -S`` so untrusted workspace modules cannot replace
the imported launcher or the standard-library ``resource`` module.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--limits", required=True)
    parser.add_argument("--report-fd", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    command = list(arguments.command)
    if command[:1] == ["--"]:
        command = command[1:]
    report = _apply_limits(json.loads(arguments.limits))
    os.write(arguments.report_fd, json.dumps(report, sort_keys=True).encode("utf-8"))
    os.close(arguments.report_fd)
    status = report["status"]
    if status != "applied" and not (status == "partial" and sys.platform == "darwin"):
        return 125
    if not command:
        return 125
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError as exc:
        print(f"local-process launcher exec failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 126


def _apply_limits(configured: dict[str, object]) -> dict[str, object]:
    try:
        import resource
    except ImportError:
        return {"status": "unsupported", "applied": {}, "unsupported": ["resource_module"]}

    names = {
        "cpu_seconds": "RLIMIT_CPU",
        "address_space_bytes": "RLIMIT_AS",
        "max_processes": "RLIMIT_NPROC",
        "max_open_files": "RLIMIT_NOFILE",
        "max_file_bytes": "RLIMIT_FSIZE",
    }
    applied: dict[str, int] = {}
    unsupported: list[str] = []
    failed: list[str] = []
    for name, constant_name in names.items():
        limit = configured.get(name)
        if type(limit) is not int or limit < 1:
            failed.append(f"{name}:invalid")
            continue
        if name == "address_space_bytes" and sys.platform == "darwin":
            # Darwin exposes RLIMIT_AS but rejects useful values on supported
            # Python/macOS combinations. Linux is the deployment target.
            unsupported.append(name)
            continue
        if name == "max_processes" and sys.platform == "darwin":
            # RLIMIT_NPROC is per UID. Developer desktops commonly already have
            # more processes than the requested Skill budget.
            unsupported.append(name)
            continue
        if not hasattr(resource, constant_name):
            unsupported.append(name)
            continue
        resource_id = getattr(resource, constant_name)
        try:
            _, hard = resource.getrlimit(resource_id)
            effective = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
            resource.setrlimit(resource_id, (effective, effective))
            soft_after, hard_after = resource.getrlimit(resource_id)
            if soft_after != effective or hard_after != effective:
                failed.append(f"{name}:verification")
            else:
                applied[name] = effective
        except (OSError, ValueError) as exc:
            failed.append(f"{name}:{type(exc).__name__}")
    status = "failed" if failed else ("partial" if unsupported else "applied")
    return {"status": status, "applied": applied, "unsupported": unsupported, "failed": failed}


if __name__ == "__main__":  # pragma: no cover - exercised through subprocesses
    raise SystemExit(main())
