"""Find and kill a browser's processes (Linux /proc; a no-op elsewhere).

A browser session is a Playwright driver process with Chromium below it. Closing the
session normally ends them all. This module is the backstop for when it doesn't: take a
snapshot of the tree before closing, and kill whatever is still there afterwards.

A process is identified by (pid, start time), so a pid that the kernel has handed to a
new process in the meantime is never mistaken for the old one.
"""

import os
import signal
from typing import Optional

Proc = tuple[int, int]  # (pid, start time in clock ticks since boot)


def _stat(pid: int) -> Optional[tuple[str, int, int]]:
    """(state, parent pid, start time) of a process, or None if it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()  # the command name may contain spaces
        return fields[0], int(fields[1]), int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def identify(pid: Optional[int]) -> Optional[Proc]:
    stat = _stat(pid) if pid else None
    return (pid, stat[2]) if pid and stat else None


def tree(root: Optional[Proc]) -> list[Proc]:
    """The root process and all its descendants, as they are right now."""
    if root is None or identify(root[0]) != root:
        return []
    children: dict[int, list[Proc]] = {}
    for name in os.listdir("/proc"):
        if name.isdigit() and (stat := _stat(int(name))):
            children.setdefault(stat[1], []).append((int(name), stat[2]))
    found, queue = [root], [root[0]]
    while queue:
        for child in children.get(queue.pop(), []):
            found.append(child)
            queue.append(child[0])
    return found


def alive(procs: list[Proc]) -> list[Proc]:
    """Those of the given processes that still run (zombies are already dead)."""
    return [p for p in procs if (stat := _stat(p[0])) and stat[2] == p[1] and stat[0] != "Z"]


def kill(procs: list[Proc]) -> int:
    """SIGKILL the given processes that still run. Returns how many were signalled."""
    killed = 0
    for pid, _ in alive(procs):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return killed
