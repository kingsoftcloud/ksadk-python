"""Standalone Linux rlimit smoke test run in the Base Agent image.

This file intentionally uses plain assertions so the deployment image does not
need pytest or uv. Run it as a non-root UID with the checkout mounted read-only.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ksadk.sandbox.local_controls import LocalControlSettings, run_bounded_process


def _run(source: str, *, settings: LocalControlSettings):
    with tempfile.TemporaryDirectory() as directory:
        return run_bounded_process(
            [sys.executable, "-c", source],
            cwd=Path(directory),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            timeout=6,
            settings=settings,
            start_new_session=True,
        )


def main() -> int:
    base = dict(
        cpu_seconds=2,
        address_space_bytes=256 * 1024 * 1024,
        max_processes=24,
        max_open_files=64,
        max_file_bytes=1024 * 1024,
        max_output_bytes=64 * 1024,
        wall_seconds=6,
    )

    cpu = _run("while True: pass", settings=LocalControlSettings(**{**base, "cpu_seconds": 1}))
    assert cpu.controls["status"] == "applied", cpu.controls
    assert cpu.exit_code != 0 and not cpu.timed_out, cpu

    memory = _run(
        "try:\n b = bytearray(200 * 1024 * 1024)\nexcept MemoryError:\n print('MEMORY_LIMIT')\n",
        settings=LocalControlSettings(**{**base, "address_space_bytes": 320 * 1024 * 1024}),
    )
    assert memory.controls["status"] == "applied", memory.controls
    assert "MEMORY_LIMIT" in memory.stdout, memory

    file_size = _run(
        "try:\n open('large.bin', 'wb').write(b'x' * (2 * 1024 * 1024))\n"
        "except OSError:\n print('FILE_LIMIT')\n",
        settings=LocalControlSettings(**{**base, "max_file_bytes": 512 * 1024}),
    )
    assert file_size.controls["status"] == "applied", file_size.controls
    assert "FILE_LIMIT" in file_size.stdout or file_size.exit_code != 0, file_size

    descriptors = _run(
        "opened=[]\ntry:\n"
        " while True: opened.append(open('/dev/null'))\n"
        "except OSError:\n print('FD_LIMIT')\n",
        settings=LocalControlSettings(**{**base, "max_open_files": 32}),
    )
    assert descriptors.controls["status"] == "applied", descriptors.controls
    assert "FD_LIMIT" in descriptors.stdout, descriptors

    processes = _run(
        "import subprocess, time\nchildren=[]\ntry:\n"
        " while True: children.append(subprocess.Popen(['sleep', '5']))\n"
        "except (OSError, BlockingIOError): print('PROCESS_LIMIT')\n",
        settings=LocalControlSettings(**{**base, "max_processes": 12}),
    )
    assert processes.controls["status"] == "applied", processes.controls
    assert "PROCESS_LIMIT" in processes.stdout, processes

    output = _run(
        "print('x' * 200000)",
        settings=LocalControlSettings(**{**base, "max_output_bytes": 4096}),
    )
    assert output.output_limit_exceeded and output.stdout_truncated, output
    assert len(output.stdout.encode()) <= 4096, len(output.stdout.encode())

    print("linux local-process controls: cpu,memory,file,fd,process,output passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
