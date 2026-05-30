import asyncio
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Annotated

import psutil
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError
from pydantic import Field

from _tool_utils import acknowledge


def _find_windows_executable(name: str) -> str | None:
    name_lower = name.lower().strip()
    if not name_lower.endswith(".exe"):
        name_lower += ".exe"

    candidates = set()

    def _add(p: Path) -> None:
        if p.exists() and p.is_file():
            candidates.add(str(p.resolve()))

    # 1. Direct path
    p = Path(name_lower)
    if p.is_absolute():
        _add(p)
    else:
        _add(Path.cwd() / name_lower)

    # 2. PATH lookup via where.exe
    try:
        result = subprocess.run(
            ["where.exe", name_lower],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and Path(line).is_file():
                candidates.add(line)
    except Exception:
        pass

    # 3. Start Menu / Common install locations
    search_roots = [
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")),
        Path(os.environ.get("LocalAppData", Path.home() / "AppData" / "Local")),
        Path(os.environ.get("AppData", Path.home() / "AppData" / "Roaming")),
        Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps",
    ]

    stem = Path(name_lower).stem
    for root in search_roots:
        if not root.exists():
            continue
        try:
            for exe in root.rglob("*.exe"):
                if stem in exe.stem.lower():
                    candidates.add(str(exe.resolve()))
                    if len(candidates) >= 10:
                        break
        except PermissionError:
            pass

    # 4. App Paths registry via PowerShell
    try:
        script = (
            f"$path = (Get-ItemProperty -Path 'Registry::HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{name_lower}' -ErrorAction SilentlyContinue).'(default)'; "
            f"if ($path) {{ Write-Output $path }}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=5,
        )
        reg_path = result.stdout.strip()
        if reg_path and Path(reg_path).is_file():
            candidates.add(reg_path)
    except Exception:
        pass

    if candidates:
        return min(candidates, key=lambda p: (
            0 if stem in p.lower() else 1,
            len(p),
        ))
    return None


def _window_search(title_pattern: str) -> list[int]:
    import ctypes
    from ctypes import wintypes

    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowText = ctypes.windll.user32.GetWindowTextW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW

    titles = []

    def _enum_callback(hwnd, _lparam):
        if IsWindowVisible(hwnd):
            length = GetWindowTextLength(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                GetWindowText(hwnd, buf, length + 1)
                if title_pattern.lower() in buf.value.lower():
                    titles.append(hwnd)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.LPARAM)
    EnumWindows(WNDENUMPROC(_enum_callback), 0)
    return titles


# ─── tool: launch_app ─────────────────────────────────────────────────────

@function_tool()
async def launch_app(
    name: Annotated[str, Field(description="Name of the application to launch (e.g. 'notepad', 'chrome', 'spotify'). Do NOT include the full path.")],
    context: RunContext,
) -> str:
    """Launch any application installed on the system by name. Searches PATH, Start Menu, and common install locations."""
    acknowledge(context)
    exe_path = _find_windows_executable(name)
    if exe_path is None:
        raise ToolError(
            f"I could not find '{name}' installed on this system, sir. "
            "Try providing the full path to the executable."
        )

    try:
        subprocess.Popen([exe_path], close_fds=True)
        return f"Launching {Path(exe_path).stem}, sir."
    except Exception as e:
        raise ToolError(f"Failed to launch {name}: {e}")


# ─── tool: list_processes ────────────────────────────────────────────────

@function_tool()
async def list_processes(
    context: RunContext,
    filter_name: Annotated[str | None, Field(description="Optional filter to find specific processes by name (e.g. 'chrome', 'python')")] = None,
) -> str:
    """List running processes, optionally filtered by name."""
    acknowledge(context)
    proc_list = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            if filter_name and filter_name.lower() not in p.info["name"].lower():
                continue
            proc_list.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not proc_list:
        msg = f"No running processes found"
        if filter_name:
            msg += f" matching '{filter_name}'"
        return msg + ", sir."

    proc_list.sort(key=lambda x: x["memory_percent"] or 0, reverse=True)
    top = proc_list[:20]
    lines = [f"{len(proc_list)} process{'es' if len(proc_list) > 1 else ''} running"
             f"{' matching ' + filter_name if filter_name else ''}, sir:"]
    for p in top:
        cpu = f"{p['cpu_percent'] or 0:.1f}%"
        mem = f"{p['memory_percent'] or 0:.1f}%"
        lines.append(f"  {p['pid']:>6}  {p['name']:<30}  CPU: {cpu:>6}  MEM: {mem:>6}")
    return "\n".join(lines)


# ─── tool: kill_process ──────────────────────────────────────────────────

@function_tool()
async def kill_process(
    context: RunContext,
    pid: Annotated[int | None, Field(description="Process ID to terminate")] = None,
    name: Annotated[str | None, Field(description="Process name to terminate (e.g. 'notepad.exe')")] = None,
) -> str:
    """Terminate a running process by PID or by name."""
    acknowledge(context)
    if pid is not None:
        try:
            p = psutil.Process(pid)
            p.terminate()
            return f"Process {pid} ({p.name()}) terminated, sir."
        except psutil.NoSuchProcess:
            raise ToolError(f"No process with PID {pid} found, sir.")
        except psutil.AccessDenied:
            raise ToolError(f"Access denied terminating PID {pid}, sir.")

    if name is not None:
        killed = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if name.lower() in p.info["name"].lower():
                    p.terminate()
                    killed.append(f"{p.info['name']} ({p.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed:
            return f"Terminated {len(killed)} process{'es' if len(killed) > 1 else ''}: {', '.join(killed)}, sir."
        raise ToolError(f"No running process found matching '{name}', sir.")

    raise ToolError("Specify either a PID or a process name to kill, sir.")


# ─── tool: switch_window ─────────────────────────────────────────────────

@function_tool()
async def switch_window(
    name: Annotated[str, Field(description="Application name or window title to bring to front (e.g. 'chrome', 'notepad')")],
    context: RunContext,
) -> str:
    """Bring an application window to the foreground."""
    acknowledge(context)
    import ctypes
    handles = _window_search(name)
    if not handles:
        raise ToolError(f"No visible window found matching '{name}', sir.")

    for hwnd in handles:
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return f"Switching to {name}, sir."

    raise ToolError(f"Could not switch to '{name}', sir.")


# ─── tool: window_minimize ──────────────────────────────────────────────

@function_tool()
async def window_minimize(context: RunContext) -> str:
    """Minimise the currently active window."""
    acknowledge(context)
    import ctypes
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    ctypes.windll.user32.ShowWindow(hwnd, 6)
    return "Window minimised, sir."


# ─── tool: window_maximize ──────────────────────────────────────────────

@function_tool()
async def window_maximize(context: RunContext) -> str:
    """Maximise the currently active window."""
    acknowledge(context)
    import ctypes
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    ctypes.windll.user32.ShowWindow(hwnd, 3)
    return "Window maximised, sir."


# ─── tool: window_close ─────────────────────────────────────────────────

@function_tool()
async def window_close(context: RunContext) -> str:
    """Close the currently active window."""
    acknowledge(context)
    import ctypes
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    return "Closing window, sir."


APP_TOOLS = [
    launch_app,
    list_processes,
    kill_process,
    switch_window,
    window_minimize,
    window_maximize,
    window_close,
]
