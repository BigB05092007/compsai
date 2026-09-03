"""
Test helper: make LibreOffice recalculate a workbook so tests can read formula results.

Why: openpyxl writes formulas as text and never evaluates them. To prove that
``=MEDIAN(...)`` on the Comps sheet really produces the peer median (and that it
changes when an Inputs cell changes), something has to run the formulas. LibreOffice
Calc recalculates every formula when it loads an .xlsx file and writes the cached
values back out when it saves, so a headless ``--convert-to xlsx`` round-trip gives
us a copy of the workbook whose cells carry both the formula and its computed value
(``openpyxl.load_workbook(path, data_only=True)`` then returns the values).

Tests that rely on this must ``pytest.skip`` when ``recalc`` returns ``None``: it is a
verification aid, not a hard dependency, so the structural tests still run on a
machine without LibreOffice.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("compsai")


def _soffice_env() -> dict:
    """Environment for a headless soffice run.

    ``SAL_USE_VCLPLUGIN=svp`` picks the headless "svp" backend so no display is needed.
    Some sandboxes block AF_UNIX sockets, which LibreOffice needs for its internal pipe;
    the xlsx skill shipped with Claude Code carries an LD_PRELOAD shim for that case, so
    reuse it when it exists (``get_soffice_env`` builds the shim on first use).
    """
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"
    if _af_unix_blocked():
        shim_env = _shim_env_from_skill()
        if shim_env is not None:
            env.update(shim_env)
        else:
            log.warning("AF_UNIX sockets are blocked and no soffice shim was found; "
                        "LibreOffice may fail to start")
    return env


def _af_unix_blocked() -> bool:
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
        return False
    except OSError:
        return True


def _shim_env_from_skill() -> dict | None:
    """Load ``get_soffice_env`` from the xlsx skill's soffice.py if it is installed."""
    import importlib.util

    for candidate in Path("/root/.claude/skills/synced").glob("*/xlsx/scripts/office/soffice.py"):
        try:
            spec = importlib.util.spec_from_file_location("lo_soffice_shim", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            return module.get_soffice_env()
        except Exception as exc:  # noqa: BLE001 - best effort only
            log.warning("could not load soffice shim from %s: %s", candidate, exc)
    return None


def recalc(path: Path | str, timeout: int = 300) -> Path | None:
    """Return a recalculated copy of ``path`` produced by headless LibreOffice, or None.

    None means "could not verify here" (soffice missing, non-zero exit, timeout, or no
    output file). The copy lives in a fresh temporary directory that is left in place
    for the caller to read; a fresh ``UserInstallation`` profile is used for every run
    because LibreOffice refuses to start when another instance owns the default profile.
    """
    soffice = shutil.which("soffice")
    if soffice is None:
        log.info("soffice not on PATH; skipping recalculation")
        return None

    src = Path(path).resolve()
    work = Path(tempfile.mkdtemp(prefix="compsai-recalc-"))
    profile = work / "profile"
    out_dir = work / "out"
    out_dir.mkdir()

    cmd = [
        soffice,
        f"-env:UserInstallation={profile.as_uri()}",
        "--headless",
        "--norestore",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(out_dir),
        str(src),
    ]
    try:
        result = subprocess.run(
            cmd, env=_soffice_env(), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        log.warning("soffice timed out after %ss", timeout)
        return None
    except OSError as exc:
        log.warning("soffice could not be started: %s", exc)
        return None

    if result.returncode != 0:
        log.warning("soffice exited %s: %s", result.returncode, result.stderr.strip())
        return None

    produced = out_dir / src.name
    if not produced.exists():
        log.warning("soffice produced no file (stdout=%r)", result.stdout.strip())
        return None
    return produced
