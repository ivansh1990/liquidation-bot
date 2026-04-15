"""
Health-check primitives for the /health command.

Everything here is tolerant of the "not available" case:
  - `systemctl` / `journalctl` missing (local dev on Darwin) → returns graceful
    "unknown"/[] instead of raising.
  - API ping times out → (ok=False, ms=<elapsed>) rather than propagating.
  - /proc/meminfo missing (Darwin) → mem_* keys are None.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------
async def _run(cmd: list[str], *, timeout: float = 3.0) -> tuple[int, str, str]:
    """Run a subprocess, capture stdout+stderr. Returns (rc, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace").strip(),
        err.decode("utf-8", errors="replace").strip(),
    )


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


async def check_systemd_unit(unit: str) -> dict[str, Any]:
    """
    Returns {unit, state, uptime, raw}
      state ∈ {"active", "inactive", "failed", "activating", "unknown"}
      uptime: human-readable "4h 12m" or None
    """
    if not _systemctl_available():
        return {"unit": unit, "state": "unknown", "uptime": None,
                "raw": "systemctl not installed"}
    try:
        rc, active, _ = await _run(["systemctl", "is-active", unit])
        state = active or "unknown"
    except Exception as e:
        return {"unit": unit, "state": "unknown", "uptime": None,
                "raw": f"is-active error: {e}"}

    uptime: str | None = None
    try:
        rc2, show_out, _ = await _run(
            ["systemctl", "show", unit,
             "-p", "ActiveEnterTimestamp", "-p", "SubState"]
        )
        # Parse `ActiveEnterTimestamp=Tue 2026-04-14 12:00:00 UTC`
        for line in show_out.splitlines():
            if line.startswith("ActiveEnterTimestamp="):
                ts_str = line.split("=", 1)[1].strip()
                uptime = _uptime_from_systemd_ts(ts_str)
                break
    except Exception as e:
        log.debug("systemctl show %s failed: %s", unit, e)

    return {"unit": unit, "state": state, "uptime": uptime, "raw": active}


def _uptime_from_systemd_ts(ts_str: str) -> str | None:
    """Parse systemd's ActiveEnterTimestamp and return elapsed as 'Xd Yh Zm'."""
    if not ts_str or ts_str == "n/a" or ts_str == "0":
        return None
    # systemd typically formats as 'Tue 2026-04-14 12:00:00 UTC'
    # We parse best-effort; on failure return None.
    import datetime as _dt
    from datetime import timezone as _tz
    # Strip leading weekday if present
    parts = ts_str.split(None, 1)
    if len(parts) == 2 and len(parts[0]) == 3:  # 'Tue '
        ts_str = parts[1]
    # Try a few formats
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = _dt.datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            elapsed = _dt.datetime.now(_tz.utc) - dt
            return _fmt_duration(elapsed.total_seconds())
        except ValueError:
            continue
    return None


def _fmt_duration(secs: float) -> str:
    secs = int(max(0, secs))
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    mins, _ = divmod(secs, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    return " ".join(parts)


async def recent_errors(unit: str, hours: int = 1, limit: int = 10) -> list[str]:
    """Best-effort journalctl error tail. [] when journalctl missing/fails."""
    if shutil.which("journalctl") is None:
        return []
    try:
        _, out, _ = await _run(
            ["journalctl", "-u", unit,
             "--since", f"{hours} hour ago",
             "-p", "err", "--no-pager", "-n", str(limit)],
            timeout=5.0,
        )
    except Exception as e:
        log.debug("journalctl %s failed: %s", unit, e)
        return []
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # Drop the "-- Logs begin at ..." banner if present
    return [ln for ln in lines if not ln.startswith("--")]


# ---------------------------------------------------------------------------
# HTTP pings
# ---------------------------------------------------------------------------
API_ENDPOINTS: list[dict[str, Any]] = [
    {
        "name": "Binance",
        "url": "https://fapi.binance.com/fapi/v1/ping",
        "method": "GET",
    },
    {
        "name": "CoinGlass",
        "url": "https://open-api-v4.coinglass.com/api/futures/supported-coins",
        "method": "GET",
    },
    {
        "name": "Hyperliquid",
        "url": "https://api.hyperliquid.xyz/info",
        "method": "POST",
        "json": {"type": "meta"},
    },
    {
        "name": "Bitget",
        "url": "https://api.bitget.com/api/v2/public/time",
        "method": "GET",
    },
]


async def ping_endpoint(
    session: aiohttp.ClientSession,
    spec: dict[str, Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    name = spec["name"]
    url = spec["url"]
    method = spec.get("method", "GET")
    body = spec.get("json")
    start = time.monotonic()
    try:
        if method == "POST":
            async with session.post(
                url, json=body, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                await resp.read()
                ok = 200 <= resp.status < 300
        else:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                await resp.read()
                ok = 200 <= resp.status < 300
        ms = int((time.monotonic() - start) * 1000)
        return {"name": name, "ok": ok, "ms": ms}
    except Exception as e:
        ms = int((time.monotonic() - start) * 1000)
        log.debug("ping %s failed: %s", name, e)
        return {"name": name, "ok": False, "ms": ms}


async def ping_all(timeout: float = 5.0) -> list[dict[str, Any]]:
    async with aiohttp.ClientSession() as session:
        tasks = [ping_endpoint(session, s, timeout) for s in API_ENDPOINTS]
        return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Host stats
# ---------------------------------------------------------------------------
def host_stats() -> dict[str, Any]:
    out: dict[str, Any] = {
        "cpu_pct": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "disk_pct": None,
        "load_1m": None,
    }
    # CPU via load average — cheap, cross-platform.
    try:
        load1, _, _ = __import__("os").getloadavg()
        out["load_1m"] = load1
        cpu_count = __import__("os").cpu_count() or 1
        out["cpu_pct"] = min(100.0, load1 / cpu_count * 100.0)
    except (AttributeError, OSError):
        pass

    # Disk
    try:
        du = shutil.disk_usage("/")
        out["disk_pct"] = du.used / du.total * 100.0 if du.total else None
    except Exception:
        pass

    # Memory (Linux /proc/meminfo; skip on Darwin).
    try:
        import os
        meminfo_path = "/proc/meminfo"
        if os.path.exists(meminfo_path):
            total_kb = avail_kb = None
            with open(meminfo_path, "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    if total_kb is not None and avail_kb is not None:
                        break
            if total_kb is not None and avail_kb is not None:
                out["mem_total_mb"] = total_kb / 1024.0
                out["mem_used_mb"] = (total_kb - avail_kb) / 1024.0
    except Exception:
        pass

    return out
