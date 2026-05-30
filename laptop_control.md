# JARVIS — Laptop Control Functionality

## Design Principles
- Every command returns a status report (success/failure + detail) in JARVIS's formal register
- Destructive actions require confirmation ("Are you certain, sir?")
- All operations run asynchronously — never block the voice conversation
- Sensitive operations (shutdown, file delete) include a dry-run or preview where possible

---

## 1. System Control

| Command | Description | Safety |
|---------|-------------|--------|
| `lock_screen` | Lock the workstation | Immediate |
| `shutdown` | Shut down the system | Confirmation required |
| `restart` | Reboot the system | Confirmation required |
| `sleep` | Put system to sleep | Immediate |
| `volume_get` | Read current volume level | — |
| `volume_set(level)` | Set volume (0–100) | Immediate |
| `volume_mute` | Toggle mute | Immediate |
| `brightness_set(level)` | Set display brightness (0–100) | Immediate |
| `system_status` | Report OS, uptime, CPU, RAM, disk usage | — |
| `battery_status` | Report battery percentage and charging state | — |

---

## 2. File Operations

| Command | Description | Safety |
|---------|-------------|--------|
| `list_directory(path)` | List files and folders | Read-only |
| `open_file(path)` | Open with default application | Read-only |
| `read_file(path)` | Display file contents | Read-only |
| `search_files(query, dir)` | Search by filename or content | Read-only |
| `create_file(path)` | Create a new empty file | Immediate |
| `create_folder(path)` | Create a new directory | Immediate |
| `copy_file(src, dst)` | Copy file or folder | Immediate |
| `move_file(src, dst)` | Move/rename file or folder | Immediate |
| `delete_file(path)` | Delete file (to recycle bin) | Confirmation required |
| `file_info(path)` | Get size, modified date, type | Read-only |

---

## 3. Application & Process Management

| Command | Description | Safety |
|---------|-------------|--------|
| `launch_app(name_or_path)` | Launch application by name or path | Immediate |
| `list_processes(filter)` | List running processes (optional filter) | Read-only |
| `kill_process(pid_or_name)` | Terminate a process | Confirmation required |
| `window_minimize()` | Minimise the active window | Immediate |
| `window_maximize()` | Maximise the active window | Immediate |
| `window_close()` | Close the active window | Immediate |
| `switch_window(app_name)` | Bring a specific application to focus | Immediate |

---

## 4. Clipboard

| Command | Description | Safety |
|---------|-------------|--------|
| `clipboard_read` | Read current clipboard text | Read-only |
| `clipboard_write(text)` | Set clipboard contents | Immediate |

---

## 5. Web & Media

| Command | Description | Safety |
|---------|-------------|--------|
| `open_url(url)` | Open URL in default browser | Immediate |
| `take_screenshot(path)` | Capture screenshot to file | Read-only |
| `media_play_pause` | Toggle play/pause | Immediate |
| `media_next` | Next track | Immediate |
| `media_previous` | Previous track | Immediate |

---

## 6. Input Simulation

| Command | Description | Safety |
|---------|-------------|--------|
| `type_text(text)` | Type text at current cursor position | Immediate |
| `press_key(keys)` | Press key combination (e.g. `Ctrl+C`) | Immediate |
| `mouse_move(x, y)` | Move cursor to coordinates | Immediate |
| `mouse_click(button)` | Click at current cursor position | Immediate |
| `mouse_scroll(amount)` | Scroll up/down | Immediate |

---

## 7. Scheduling & Alerts

| Command | Description | Safety |
|---------|-------------|--------|
| `set_timer(duration, label)` | Set a countdown timer | Immediate |
| `set_reminder(time, message)` | Schedule a reminder | Immediate |
| `list_timers` | Show active timers | Read-only |
| `cancel_timer(id)` | Cancel a timer | Immediate |

---

## 8. Development

| Command | Description | Safety |
|---------|-------------|--------|
| `run_command(command)` | Execute a shell command | Confirmation required for destructive ops |
| `run_script(path)` | Execute a script file | Confirmation required |

---

## 9. Network

| Command | Description | Safety |
|---------|-------------|--------|
| `get_ip` | Get local IP address | Read-only |
| `check_internet` | Test internet connectivity | Read-only |
| `wifi_status` | Get SSID and signal strength | Read-only |

---

## Implementation Architecture

```
┌──────────────────────┐
│   Google Realtime    │  (voice I/O)
│       Model          │
└────────┬─────────────┘
         │ LLM decides to call tool
         ▼
┌──────────────────────┐
│  LiveKit Function    │  (tool definitions sent to LLM)
│    Tools Layer       │
└────────┬─────────────┘
         │ async invocation
         ▼
┌──────────────────────┐
│  Python Backend      │  (pyautogui, os, subprocess,
│  Implementation      │   psutil, pygetwindow, etc.)
└────────┬─────────────┘
         │ OS API calls
         ▼
┌──────────────────────┐
│   Windows System     │
└──────────────────────┘
```

Tools are registered with the Agent as LiveKit function tools. The LLM calls them naturally through conversation. Each tool result flows back as a spoken status report from JARVIS.
