"""Desktop notification when a long run ends (macOS Notification Center via osascript; no-op elsewhere)."""
from __future__ import annotations

import subprocess
import sys


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')[:240]


def notify(title: str, message: str, *, enabled: bool = True) -> bool:
    if not enabled or sys.platform != "darwin":
        return False
    try:
        subprocess.run(["osascript", "-e", f'display notification "{_esc(message)}" with title "{_esc(title)}"'],
                       check=False, capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def run_finished(settings, kind: str, result: dict) -> None:
    status = result.get("status", "done")
    bits = [f"status {status}"]
    for k in ("completed", "processed_this_invocation", "reviewed", "with_sources", "checks"):
        if k in result:
            bits.append(f"{k.replace('_', ' ')} {result[k]}")
    if result.get("remaining"):
        bits.append(f"{result['remaining']} remaining")
    notify(f"NSMPA {kind} finished", ", ".join(bits), enabled=getattr(settings, "notify_on_finish", True))
