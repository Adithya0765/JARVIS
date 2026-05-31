import asyncio
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated

import psutil
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError
from pydantic import Field

_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="app_search")
_app_map: dict[str, str] | None = None
_app_map_lock = asyncio.Lock()

_START_MENU_PS1 = r'''
$shortcuts = @{}
$paths = @(
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs",
    "$env:ProgramData\Microsoft\Windows\Start Menu\Programs"
)
foreach ($path in $paths) {
    if (Test-Path $path) {
        Get-ChildItem -Path $path -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($_.FullName)
            $target = $shortcut.TargetPath
            if ($target -and $target -like '*.exe') {
                $shortcuts[$_.BaseName.ToLower()] = $target
            }
        }
    }
}
Write-Output (ConvertTo-Json $shortcuts)
'''

_REGISTRY_PS1 = r'''
$apps = @{}
$paths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
)
foreach ($path in $paths) {
    Get-ItemProperty -Path $path -ErrorAction SilentlyContinue | ForEach-Object {
        $name = $_.DisplayName
        $loc = $_.InstallLocation
        $icon = $_.DisplayIcon
        if ($name) {
            if ($loc -and (Test-Path $loc)) {
                $loc = $loc.TrimEnd('\')
                if ($loc -like '*.exe') {
                    $apps[$name.ToLower()] = $loc
                } elseif (Test-Path $loc -PathType Container) {
                    $exes = Get-ChildItem -Path $loc -Filter *.exe -ErrorAction SilentlyContinue | Where-Object { -not $_.PSIsContainer }
                    if ($exes) {
                        $words = $name.ToLower() -split '\s+|[-_]'
                        $best = $exes | Sort-Object Length | Select-Object -First 1
                        foreach ($e in $exes) {
                            $estem = $e.BaseName.ToLower()
                            foreach ($w in $words) {
                                if ($w.Length -gt 2 -and $estem -match [regex]::Escape($w)) {
                                    $best = $e
                                    break
                                }
                            }
                        }
                        $apps[$name.ToLower()] = $best.FullName
                    }
                }
            } elseif ($icon -and $icon -like '*.exe*') {
                $exePath = $icon -replace ',.*$', ''
                if (Test-Path $exePath) { $apps[$name.ToLower()] = $exePath }
            }
        }
    }
}
Write-Output (ConvertTo-Json $apps)
'''


def _run_ps(script: str, timeout: int = 15) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        out = result.stdout.strip()
        if not out:
            return {}
        return json.loads(out)
    except Exception:
        return {}


def _build_app_map() -> dict[str, str]:
    sm = _run_ps(_START_MENU_PS1)
    reg = _run_ps(_REGISTRY_PS1)
    merged = {}
    merged.update(reg)
    merged.update(sm)
    return merged


async def _ensure_app_map():
    global _app_map
    if _app_map is not None:
        return
    async with _app_map_lock:
        if _app_map is not None:
            return
        loop = asyncio.get_event_loop()
        _app_map = await loop.run_in_executor(_EXECUTOR, _build_app_map)


def _search_directories(stem: str) -> set[str]:
    candidates = set()
    search_roots = [
        Path(os.environ.get("ProgramFiles", "C:\\Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")),
        Path(os.environ.get("LocalAppData", Path.home() / "AppData" / "Local")),
        Path(os.environ.get("AppData", Path.home() / "AppData" / "Roaming")),
    ]
    for root in search_roots:
        if not root.exists():
            continue
        try:
            for entry in root.iterdir():
                if entry.is_file() and entry.suffix.lower() == ".exe" and stem in entry.stem.lower():
                    candidates.add(str(entry.resolve()))
                elif entry.is_dir():
                    try:
                        for sub in entry.iterdir():
                            if sub.is_file() and sub.suffix.lower() == ".exe" and stem in sub.stem.lower():
                                candidates.add(str(sub.resolve()))
                    except PermissionError:
                        pass
        except PermissionError:
            pass
    return candidates


async def _find_exe(name: str) -> str | None:
    name_lower = name.lower().strip()
    has_ext = name_lower.endswith(".exe")
    exe_name = name_lower if has_ext else name_lower + ".exe"
    stem = Path(exe_name).stem

    candidates: set[str] = set()

    def _add(p: Path):
        if p.exists() and p.is_file():
            candidates.add(str(p.resolve()))

    loop = asyncio.get_event_loop()

    # 1. Direct path
    p = Path(exe_name)
    if p.is_absolute():
        _add(p)
    else:
        _add(Path.cwd() / exe_name)

    # 2. App name mapping (fuzzy match display names from Start Menu + Registry)
    await _ensure_app_map()

    fuzzy_path = None
    if _app_map:
        nl = name_lower
        if nl in _app_map:
            fuzzy_path = _app_map[nl]
        else:
            for key, val in _app_map.items():
                if nl in key or key in nl:
                    fuzzy_path = val
                    break
            if fuzzy_path is None:
                nl_words = set(nl.split())
                best_ratio = 0.6
                best_key = None
                for key in _app_map:
                    key_words = set(key.split())
                    overlap = len(nl_words & key_words)
                    if overlap > 0 and overlap >= len(nl_words) * 0.5:
                        fuzzy_path = _app_map[key]
                        break
                    ratio = SequenceMatcher(None, nl, key).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_key = key
                if fuzzy_path is None and best_key:
                    fuzzy_path = _app_map[best_key]
        if fuzzy_path:
            _add(Path(fuzzy_path))

    # 3. where.exe (PATH lookup) + directory search in parallel
    def _where():
        try:
            result = subprocess.run(
                ["where.exe", exe_name],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and Path(line).is_file():
                    candidates.add(line)
        except Exception:
            pass

    def _windowsapps():
        winapps = Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps"
        if winapps.exists():
            try:
                for entry in winapps.iterdir():
                    if entry.is_file() and entry.suffix.lower() == ".exe" and stem in entry.stem.lower():
                        candidates.add(str(entry.resolve()))
            except (PermissionError, OSError):
                pass

    def _apppaths():
        try:
            script = (
                f"$path = (Get-ItemProperty -Path 'Registry::HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{exe_name}' -ErrorAction SilentlyContinue).'(default)'; "
                f"if ($path) {{ Write-Output $path }}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=3,
            )
            reg_path = result.stdout.strip()
            if reg_path and Path(reg_path).is_file():
                candidates.add(reg_path)
        except Exception:
            pass

    if not has_ext:
        dir_task = loop.run_in_executor(_EXECUTOR, _search_directories, stem)
        where_task = loop.run_in_executor(_EXECUTOR, _where)
    else:
        dir_task = loop.run_in_executor(_EXECUTOR, _search_directories, stem)
        where_task = loop.run_in_executor(_EXECUTOR, _where)

    winapps_task = loop.run_in_executor(_EXECUTOR, _windowsapps)
    apppaths_task = loop.run_in_executor(_EXECUTOR, _apppaths)

    await asyncio.gather(dir_task, where_task, winapps_task, apppaths_task)

    if candidates:
        return min(candidates, key=lambda p: (
            0 if stem in p.lower() else 1,
            len(p),
        ))

    return fuzzy_path if fuzzy_path and Path(fuzzy_path).is_file() else None


# ─── tool: launch_app ─────────────────────────────────────────────────────

@function_tool()
async def launch_app(
    name: Annotated[str, Field(description="App name")],
    context: RunContext,
) -> str:
    """Launch any installed app by name."""
    exe_path = await _find_exe(name)
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
    filter_name: Annotated[str | None, Field(description="Process name filter")] = None,
) -> str:
    """List running processes."""
    proc_list = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            if filter_name and filter_name.lower() not in p.info["name"].lower():
                continue
            proc_list.append(p.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if not proc_list:
        msg = "No running processes found"
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
    pid: Annotated[int | None, Field(description="PID to kill")] = None,
    name: Annotated[str | None, Field(description="Process name to kill")] = None,
) -> str:
    """Kill process by PID or name."""
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


def _window_search(title_pattern: str, visible_only: bool = True) -> list[int]:
    import ctypes
    from ctypes import wintypes

    EnumWindows = ctypes.windll.user32.EnumWindows
    GetWindowText = ctypes.windll.user32.GetWindowTextW
    IsWindowVisible = ctypes.windll.user32.IsWindowVisible
    GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW

    titles = []
    def _enum_callback(hwnd, _lparam):
        if visible_only and not IsWindowVisible(hwnd):
            return True
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


# ─── tool: switch_window ─────────────────────────────────────────────────

@function_tool()
async def switch_window(
    name: Annotated[str, Field(description="Window/app name")],
    context: RunContext,
) -> str:
    """Bring window to foreground."""
    import ctypes
    handles = _window_search(name, visible_only=True)
    if not handles:
        handles = _window_search(name, visible_only=False)
    if not handles:
        raise ToolError(f"No window found matching '{name}', sir.")
    for hwnd in handles:
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return f"Switching to {name}, sir."
    raise ToolError(f"Could not switch to '{name}', sir.")


# ─── tool: window_minimize ──────────────────────────────────────────────

@function_tool()
async def window_minimize(
    context: RunContext,
    name: Annotated[str | None, Field(description="Window/app name")] = None,
) -> str:
    """Minimise a window by name, or active window if no name given."""
    import ctypes
    if name:
        handles = _window_search(name, visible_only=True)
        if not handles:
            handles = _window_search(name, visible_only=False)
        if not handles:
            raise ToolError(f"No window found matching '{name}', sir.")
        for hwnd in handles:
            ctypes.windll.user32.ShowWindow(hwnd, 6)
        return f"Window '{name}' minimised, sir."
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    ctypes.windll.user32.ShowWindow(hwnd, 6)
    return "Window minimised, sir."


# ─── tool: window_maximize ──────────────────────────────────────────────

@function_tool()
async def window_maximize(
    context: RunContext,
    name: Annotated[str | None, Field(description="Window/app name")] = None,
) -> str:
    """Maximise a window by name, or active window if no name given."""
    import ctypes
    if name:
        handles = _window_search(name, visible_only=True)
        if not handles:
            handles = _window_search(name, visible_only=False)
        if not handles:
            raise ToolError(f"No window found matching '{name}', sir.")
        for hwnd in handles:
            ctypes.windll.user32.ShowWindow(hwnd, 3)
        return f"Window '{name}' maximised, sir."
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    ctypes.windll.user32.ShowWindow(hwnd, 3)
    return "Window maximised, sir."


# ─── tool: window_close ─────────────────────────────────────────────────

@function_tool()
async def window_close(
    context: RunContext,
    name: Annotated[str | None, Field(description="Window/app name")] = None,
) -> str:
    """Close a window by name, or active window if no name given."""
    import ctypes
    if not name:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return "Closing window, sir."

    handles = _window_search(name, visible_only=True)
    if not handles:
        handles = _window_search(name, visible_only=False)
    if not handles:
        killed = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if name.lower() in p.info["name"].lower():
                    p.terminate()
                    killed.append(f"{p.info['name']} ({p.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed:
            return f"Closed '{name}' by terminating {len(killed)} process{'es' if len(killed) > 1 else ''}, sir."
        raise ToolError(f"No window or process found matching '{name}', sir.")

    for hwnd in handles:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    return f"Closing '{name}', sir."


APP_TOOLS = [
    launch_app,
    list_processes,
    kill_process,
    switch_window,
    window_minimize,
    window_maximize,
    window_close,
]
