"""Background worker processes (backtests, learning analysis): each runs `python -m agent.cli <command>` on its own,
so it never shares the live agent's clock, quotes or orders. Output goes to runtime/logs/<name>.log."""
from __future__ import annotations

import os
import subprocess
import sys

from .settings import ROOT, RUNTIME_DIR

_procs: dict[str, subprocess.Popen] = {}


def spawn(key: str, args: list[str], log_name: str) -> None:
    logs = RUNTIME_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / f"{log_name}.log", "w", encoding="utf-8")  # noqa: SIM115 - owned by the child
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: no console pops up on Windows
    _procs[key] = subprocess.Popen([sys.executable, "-m", "agent.cli", *args], cwd=str(ROOT), stdout=out,
                                   stderr=subprocess.STDOUT, creationflags=flags)


def alive(key: str) -> bool:
    p = _procs.get(key)
    return p is not None and p.poll() is None


def any_alive(prefix: str) -> bool:
    return any(alive(k) for k in list(_procs) if k.startswith(prefix))
