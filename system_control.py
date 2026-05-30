import asyncio
import ctypes
import os
import subprocess
import time
from typing import Annotated

import psutil
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError
from pydantic import Field

from _tool_utils import acknowledge

try:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    _audio_available = True
except Exception:
    _audio_available = False


# ─── helpers ───────────────────────────────────────────────────────────────

def _run_powershell(script: str) -> str:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()
    except Exception as e:
        return str(e)


def _get_audio_interface():
    if not _audio_available:
        raise ToolError("Audio control is not available on this system.")
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return interface.QueryInterface(IAudioEndpointVolume)


# ─── tool: system_status ──────────────────────────────────────────────────

@function_tool()
async def system_status(context: RunContext) -> str:
    """Report the current system status including OS, uptime, CPU, memory, and disk usage."""
    acknowledge(context)
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = psutil.boot_time()
    uptime_seconds = time.time() - boot
    uptime_hours = uptime_seconds // 3600
    uptime_minutes = (uptime_seconds % 3600) // 60

    return (
        f"System status, sir. OS: Windows. "
        f"Uptime: {int(uptime_hours)} hours and {int(uptime_minutes)} minutes. "
        f"CPU utilisation: {cpu} percent. "
        f"Memory: {mem.percent} percent used ({mem.used // (1024**3)} of {mem.total // (1024**3)} gigabytes). "
        f"Disk: {disk.percent} percent used ({disk.free // (1024**3)} gigabytes free)"
    )


# ─── tool: battery_status ─────────────────────────────────────────────────

@function_tool()
async def battery_status(context: RunContext) -> str:
    """Report the battery percentage and charging status."""
    acknowledge(context)
    batt = psutil.sensors_battery()
    if batt is None:
        return "No battery detected, sir. The system is running on direct power."
    charging = "charging" if batt.power_plugged else "on battery"
    return f"Battery at {batt.percent} percent, currently {charging}, sir."


# ─── tool: lock_screen ────────────────────────────────────────────────────

@function_tool()
async def lock_screen(context: RunContext) -> str:
    """Lock the workstation immediately."""
    acknowledge(context)
    ctypes.windll.user32.LockWorkStation()
    return "Workstation locked, sir."


# ─── tool: shutdown ───────────────────────────────────────────────────────

@function_tool()
async def shutdown(context: RunContext) -> str:
    """Shut down the system. User will be asked to confirm via conversation."""
    acknowledge(context)
    subprocess.Popen(["shutdown", "/s", "/t", "3"], close_fds=True)
    return "Shutting down in 3 seconds, sir."


# ─── tool: restart ────────────────────────────────────────────────────────

@function_tool()
async def restart(context: RunContext) -> str:
    """Restart the system. User will be asked to confirm via conversation."""
    acknowledge(context)
    subprocess.Popen(["shutdown", "/r", "/t", "3"], close_fds=True)
    return "Restarting in 3 seconds, sir."


# ─── tool: hibernate ──────────────────────────────────────────────────────

@function_tool()
async def hibernate(context: RunContext) -> str:
    """Hibernate the system. Saves the current session to disk and powers off."""
    acknowledge(context)
    ctypes.windll.powrprof.SetSuspendState(1, 1, 0)
    return "Hibernating, sir."


# ─── tool: volume_get ────────────────────────────────────────────────────

@function_tool()
async def volume_get(context: RunContext) -> str:
    """Read the current system volume level."""
    acknowledge(context)
    try:
        vol = _get_audio_interface()
        level = vol.GetMasterVolumeLevelScalar() * 100
        muted = vol.GetMute()
        state = "muted" if muted else "unmuted"
        return f"Volume is at {int(level)} percent and {state}, sir."
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Unable to read volume: {e}")


# ─── tool: volume_set ────────────────────────────────────────────────────

@function_tool()
async def volume_set(
    level: Annotated[int, Field(description="Volume level from 0 to 100", ge=0, le=100)],
    context: RunContext,
) -> str:
    """Set the system volume to a specific level between 0 and 100."""
    acknowledge(context)
    try:
        vol = _get_audio_interface()
        vol.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"Volume set to {level} percent, sir."
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Unable to set volume: {e}")


# ─── tool: volume_mute ───────────────────────────────────────────────────

@function_tool()
async def volume_mute(context: RunContext) -> str:
    """Toggle the system mute state on or off."""
    acknowledge(context)
    try:
        vol = _get_audio_interface()
        current = vol.GetMute()
        vol.SetMute(0 if current else 1, None)
        state = "unmuted" if current else "muted"
        return f"Volume {state}, sir."
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"Unable to toggle mute: {e}")


# ─── tool: brightness_set ────────────────────────────────────────────────

@function_tool()
async def brightness_set(
    level: Annotated[int, Field(description="Brightness level from 0 to 100", ge=0, le=100)],
    context: RunContext,
) -> str:
    """Set the display brightness to a specific level between 0 and 100."""
    acknowledge(context)
    try:
        script = (
            f"$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods; "
            f"$m.WmiSetBrightness(1, {level})"
        )
        _run_powershell(script)
        return f"Brightness set to {level} percent, sir."
    except Exception as e:
        raise ToolError(f"Unable to set brightness: {e}")


# ─── tool: brightness_get ────────────────────────────────────────────────

@function_tool()
async def brightness_get(context: RunContext) -> str:
    """Read the current display brightness level."""
    acknowledge(context)
    try:
        result = _run_powershell(
            "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
        )
        if result and result.isdigit():
            return f"Brightness is at {result} percent, sir."
        return "Unable to read brightness level, sir."
    except Exception as e:
        raise ToolError(f"Unable to read brightness: {e}")


SYSTEM_TOOLS = [
    system_status,
    battery_status,
    lock_screen,
    shutdown,
    restart,
    hibernate,
    volume_get,
    volume_set,
    volume_mute,
    brightness_set,
    brightness_get,
]


# ─── proactive system monitor ────────────────────────────────────────────

async def run_system_monitor(session, ctx) -> None:
    check_interval = 60
    while True:
        await asyncio.sleep(check_interval)
        try:
            alerts = []
            batt = psutil.sensors_battery()
            if batt and not batt.power_plugged and batt.percent <= 20:
                alerts.append(
                    f"Battery level has dropped to {batt.percent} percent, sir. "
                    "You may want to connect the power adapter."
                )

            mem = psutil.virtual_memory()
            if mem.percent > 90:
                alerts.append(
                    f"Memory utilisation is at {mem.percent} percent, sir. "
                    "Consider closing some applications."
                )

            disk = psutil.disk_usage("/")
            if disk.percent > 90:
                alerts.append(
                    f"Disk usage is at {disk.percent} percent, sir. "
                    f"Only {disk.free // (1024**3)} gigabytes remain."
                )

            cpu = psutil.cpu_percent(interval=1.0)
            if cpu > 90:
                alerts.append(
                    f"CPU utilisation is at {cpu} percent, sir. "
                    "A process may be consuming excessive resources."
                )

            for alert in alerts:
                await session.say(alert, allow_interruptions=True)
                await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            raise
        except Exception:
            pass
