"""Conservative Linux process-group liveness after a successful killpg probe."""

import os
from pathlib import Path


def linux_group_has_live_members(process_group: int, *, proc_root: Path = Path("/proc")) -> bool:
    """Ignore only a proven zombie/dead-only group in a complete proc view.

    Linux keeps orphan zombies in their process group until their new parent
    reaps them. killpg(pgid, 0) still succeeds, although no signal can stop a
    zombie. Unknown visibility or a racing empty snapshot stays conservative.
    """
    try:
        mounts = (proc_root / "self" / "mountinfo").read_text().splitlines()
        candidates = [line.split() for line in mounts if line.split()[4] == "/proc"]
        if len(candidates) != 1:
            return True  # An overmount can hide members from the older view.
        proc_mount = candidates[0]
        separator = proc_mount.index("-")
        if proc_mount[separator + 1] != "proc":
            return True
        options = (proc_mount[5] + "," + proc_mount[separator + 3]).split(",")
        if any(option.startswith("hidepid=") and option != "hidepid=0" for option in options):
            return True
        own = (proc_root / "self" / "stat").read_text()
        own_fields = own.rsplit(")", 1)[1].split()
        if int(own.split(" ", 1)[0]) != os.getpid() or int(own_fields[2]) != os.getpgrp():
            return True  # An ancestor PID namespace can use different IDs.
        status = (proc_root / "self" / "status").read_text().splitlines()
        namespace_pids = next(line for line in status if line.startswith("NSpid:")).split()[1:]
        if namespace_pids != [str(os.getpid())]:
            return True  # Matching numbers alone cannot prove namespace identity.
        matched = False
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                # comm is parenthesized and can contain spaces and ')'.
                stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                group = int(stat[2])
            except FileNotFoundError:
                continue  # This process was reaped during the snapshot.
            if group == process_group:
                matched = True
                if stat[0] not in {"Z", "X", "x"}:
                    return True
        return not matched
    except (OSError, ValueError, IndexError, StopIteration):
        return True
