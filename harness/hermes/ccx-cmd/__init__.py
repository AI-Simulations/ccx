"""ccx's ``/ccx`` command for Hermes (``ccx --harness hermes ...``).

Runs ccx on this profile's launch with no model turn: ``/ccx`` alone opens ccx's model picker window;
``/ccx route <provider> <model>``, ``/ccx fallback ...``, ``/ccx helper auto ...`` and ``/ccx agents ...``
change this launch's parts. The command runs in the Hermes gateway, which does not have the launch's
environment, so ccx writes the launch beside this file (``ccx.json``) when it sets the profile up.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

SETTINGS = Path(__file__).with_name("ccx.json")


def _settings() -> dict:
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_ccx(raw_args: str = "", **_context) -> str:
    settings = _settings()
    if not settings.get("launch") or not settings.get("switch"):
        return "ccx: this profile has no ccx launch; start Hermes with `ccx --harness hermes ...`."
    try:
        words = shlex.split(raw_args or "", posix=os.name != "nt")
    except ValueError as exc:
        return f"ccx: {exc}"
    # non-POSIX splitting keeps a quoted word's quotes
    words = [w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in ("'", '"') else w for w in words]
    env = {**os.environ, "CCX_LAUNCH_ID": settings["launch"], "CCX_HARNESS": "hermes",
           "CCX_MAIN_PROVIDER": settings.get("provider") or ""}
    try:
        done = subprocess.run([settings.get("python") or "python", "-B", settings["switch"], *words],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ccx: {type(exc).__name__}: {exc}"
    text = "\n".join(part.strip() for part in (done.stdout, done.stderr) if part and part.strip())
    return text or ("Done." if done.returncode == 0 else "ccx failed.")


def register(ctx) -> None:
    ctx.register_command("ccx", handler=run_ccx, args_hint="[route|fallback|helper|agents|gui ...]",
                         description="ccx: this launch's models (route, fallback, helpers, subagents, picker)")
