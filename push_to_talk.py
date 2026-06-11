import ctypes
from ctypes import wintypes
from typing import Callable

from pynput import keyboard

MIC_MUTED = True
_CALLBACK: Callable | None = None
_mic_interface = None


def _mute_mic(mute: bool) -> None:
    global MIC_MUTED, _mic_interface
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        if _mic_interface is None:
            mic = AudioUtilities.GetMicrophone()
            _mic_interface = mic.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            _mic_interface = _mic_interface.QueryInterface(IAudioEndpointVolume)
        _mic_interface.SetMute(1 if mute else 0, None)
        MIC_MUTED = mute
    except Exception:
        pass


def _on_press(k, key_name: str) -> None:
    global MIC_MUTED
    k_char = getattr(k, 'char', None)
    k_name = getattr(k, 'name', None)
    if k_char == key_name or k_name == key_name:
        MIC_MUTED = False
        _mute_mic(False)
        if _CALLBACK:
            _CALLBACK(True)


def _on_release(k, key_name: str) -> None:
    global MIC_MUTED
    k_char = getattr(k, 'char', None)
    k_name = getattr(k, 'name', None)
    if k_char == key_name or k_name == key_name:
        MIC_MUTED = True
        _mute_mic(True)
        if _CALLBACK:
            _CALLBACK(False)


class PushToTalk:
    def __init__(self, key: str = "caps_lock", callback: Callable | None = None):
        global _CALLBACK
        self.key = key
        self.callback = callback
        _CALLBACK = callback

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=lambda k: _on_press(k, self.key),
            on_release=lambda k: _on_release(k, self.key)
        )
        self._listener.start()

    def stop(self) -> None:
        if hasattr(self, '_listener'):
            self._listener.stop()
        _mute_mic(True)

    def is_pressed(self) -> bool:
        return not MIC_MUTED

    def is_mic_muted(self) -> bool:
        return MIC_MUTED