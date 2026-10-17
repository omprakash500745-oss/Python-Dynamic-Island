import ctypes
import ctypes.wintypes as wintypes
import base64
from collections import OrderedDict, deque
import json
import math
import os
import subprocess
import sys
import threading
import time
import uuid
import winreg
from datetime import datetime
from pathlib import Path

try:
    import psutil
except ImportError as exc:
    raise SystemExit("psutil is required. Install it with: python -m pip install psutil") from exc

import tkinter as tk
from tkinter import messagebox


MEDIA_QUERY = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
function Await-WinRt($operation, $resultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}
$managerType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$manager = Await-WinRt $managerType::RequestAsync() $managerType
$propertiesType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType=WindowsRuntime]
$tracks = @()
foreach ($session in $manager.GetSessions()) {
    $title = ''
    $artist = ''
    try {
        $properties = Await-WinRt $session.TryGetMediaPropertiesAsync() $propertiesType
        $title = [string]$properties.Title
        $artist = [string]$properties.Artist
    } catch {}
    $source = [string]$session.SourceAppUserModelId
    $app = switch -Regex ($source) {
        '(?i)edge' { 'Microsoft Edge'; break }
        '(?i)chrome' { 'Google Chrome'; break }
        '(?i)opera' { 'Opera'; break }
        '(?i)firefox' { 'Firefox'; break }
        '(?i)brave' { 'Brave'; break }
        default { if ($source) { ($source -split '[.!]')[0] } else { 'Media player' } }
    }
    $tracks += [pscustomobject]@{
        title = $title
        artist = $artist
        app = [string]$app
        playback = [string]$session.GetPlaybackInfo().PlaybackStatus
    }
}
$playing = @($tracks | Where-Object playback -eq 'Playing')
$browserProcesses = @(Get-Process msedge, chrome, opera, firefox, brave -ErrorAction SilentlyContinue)
if ($playing.Count) {
    $playing[0] | ConvertTo-Json -Compress
} elseif ($browserProcesses.Count) {
    $browserName = switch -Regex ($browserProcesses[0].ProcessName) {
        '(?i)msedge' { 'Microsoft Edge'; break }
        '(?i)chrome' { 'Google Chrome'; break }
        '(?i)opera' { 'Opera'; break }
        '(?i)firefox' { 'Firefox'; break }
        '(?i)brave' { 'Brave'; break }
    }
    @{ title = ''; artist = ''; app = $browserName; playback = 'Enable browser media-key handling' } | ConvertTo-Json -Compress
} elseif ($tracks.Count) {
    $tracks[0] | ConvertTo-Json -Compress
} else {
    @{ title = ''; artist = ''; app = ''; playback = 'No media session active' } | ConvertTo-Json -Compress
}
"""

APP_NOTIFICATIONS_QUERY = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
function Await-WinRt($operation, $resultType) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
        $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
    return $task.GetAwaiter().GetResult()
}
$listenerType = [Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications, ContentType=WindowsRuntime]
$listener = $listenerType::Current
$status = [string]$listener.GetAccessStatus()
if ($status -ne 'Allowed') {
    @{ status = $status; items = @() } | ConvertTo-Json -Compress -Depth 4
    exit 0
}
$notificationType = [System.Collections.Generic.IReadOnlyList[Windows.UI.Notifications.UserNotification]]
$notifications = Await-WinRt $listener.GetNotificationsAsync([Windows.UI.Notifications.NotificationKinds]::Toast) $notificationType
$bindingType = [Windows.UI.Notifications.KnownNotificationBindings, Windows.UI.Notifications, ContentType=WindowsRuntime]
$items = @(foreach ($notification in $notifications) {
    $binding = $notification.Notification.Visual.GetBinding($bindingType::ToastGeneric)
    if ($binding) {
        $text = @($binding.GetTextElements() | ForEach-Object { $_.Text } | Where-Object { $_ }) -join ' '
        if ($text) {
            [pscustomobject]@{
                id = [string]$notification.Id
                app = [string]$notification.AppInfo.DisplayInfo.DisplayName
                text = [string]$text
            }
        }
    }
})
@{ status = $status; items = $items } | ConvertTo-Json -Compress -Depth 4
"""

WINDOWS_NOTIFICATIONS_SETTINGS = "ms-settings:notifications"


class _POINT(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))


class _SIZE(ctypes.Structure):
    _fields_ = (("cx", ctypes.c_long), ("cy", ctypes.c_long))


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = (
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    )


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    )


class _BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 1))


class SoftGlow:
    PADDING = 30
    def __init__(self, root):
        self.root = root
        self.visible = False
        self.bounds = None
        self.phase = 0.0
        self.paused = False
        self._mask_cache = {}
        self.speed = 0.008
        self._color_table = []
        self.set_palette("Aurora")

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user32.CreateWindowExW.argtypes = (
            ctypes.c_uint, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        )
        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.hwnd = self.user32.CreateWindowExW(
            0x00080000 | 0x00000020 | 0x00000080 | 0x08000000,
            "STATIC", "", 0x80000000,
            0, 0, 1, 1, None, None, None, None,
        )
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.user32.SetWindowPos.argtypes = (
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        )
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.DestroyWindow.argtypes = (wintypes.HWND,)
        self.user32.UpdateLayeredWindow.argtypes = (
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
            wintypes.HDC, ctypes.POINTER(_POINT), wintypes.COLORREF,
            ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
        )
        self.user32.UpdateLayeredWindow.restype = wintypes.BOOL
        self.gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateDIBSection.argtypes = (
            wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        )
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
        self.gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        self.gdi32.DeleteDC.argtypes = (wintypes.HDC,)
        self.user32.GetDC.argtypes = (wintypes.HWND,)
        self.user32.GetDC.restype = wintypes.HDC
        self.user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)

    def set_palette(self, name):
        self.colors = DynamicIslandApp.GLOW_PALETTES[name]
        self._color_table = []
        for color_index in range(256):
            position = color_index / 256 * (len(self.colors) - 1)
            stop = int(position)
            blend = position - stop
            first, second = self.colors[stop], self.colors[stop + 1]
            red = round(first[0] + (second[0] - first[0]) * blend)
            green = round(first[1] + (second[1] - first[1]) * blend)
            blue = round(first[2] + (second[2] - first[2]) * blend)
            self._color_table.extend(
                (alpha << 24)
                | (red * alpha // 255 << 16)
                | (green * alpha // 255 << 8)
                | (blue * alpha // 255)
                for alpha in range(256)
            )
    def _alpha_mask(self, width, height):
        key = (width, height)
        if key in self._mask_cache:
            return self._mask_cache[key]

        pad = self.PADDING
        image_width, image_height = width + 2 * pad, height + 2 * pad
        radius = min(22, height / 2)
        half_width, half_height = width / 2, height / 2
        x_distances = [
            abs(px - pad - half_width) - (half_width - radius)
            for px in range(image_width)
        ]
        y_distances = [
            abs(py - pad - half_height) - (half_height - radius)
            for py in range(image_height)
        ]
        alpha_values = bytes(
            round(245 * math.exp(-distance / 12))
            for distance in (i / 4 for i in range((self.PADDING + 1) * 4))
        )
        top_length = width - 2 * radius
        side_length = height - 2 * radius
        quarter = math.pi * radius / 2
        perimeter = 2 * (top_length + side_length) + 4 * quarter
        pixels = []
        for py, qy in enumerate(y_distances):
            for px, qx in enumerate(x_distances):
                local_x, local_y = px - pad, py - pad
                dx, dy = local_x - half_width, local_y - half_height
                if qx <= 0:
                    if qy <= 0:
                        continue
                    distance = max(qy - radius, 0)
                elif qy <= 0:
                    distance = max(qx - radius, 0)
                else:
                    distance = max(math.hypot(qx, qy) - radius, 0)
                alpha = alpha_values[min(round(distance * 4), len(alpha_values) - 1)]

                if qx > 0 and qy > 0:
                    angle = math.atan2(qy, qx)
                    if dx >= 0 and dy < 0:
                        along = top_length + quarter - angle * radius
                    elif dx >= 0:
                        along = top_length + quarter + side_length + angle * radius
                    elif dy >= 0:
                        along = top_length + 2 * quarter + side_length + top_length + angle * radius
                    else:
                        along = 2 * top_length + 3 * quarter + side_length + angle * radius
                elif qx > qy:
                    along_y = min(max(local_y, radius), height - radius)
                    if dx >= 0:
                        along = top_length + quarter + along_y - radius
                    else:
                        along = top_length + 3 * quarter + side_length + top_length + height - radius - along_y
                else:
                    along_x = min(max(local_x, radius), width - radius)
                    if dy < 0:
                        along = along_x - radius
                    else:
                        along = top_length + quarter + side_length + quarter + width - radius - along_x
                position = round(along / perimeter * 255) & 255
                pixels.append((py * image_width + px, alpha, position))
        if len(self._mask_cache) >= 12:
            self._mask_cache.clear()
        self._mask_cache[key] = pixels
        return pixels

    def update(self, x, y, width, height):
        self.bounds = (x, y, width, height)
        pad = self.PADDING
        image_width, image_height = width + 2 * pad, height + 2 * pad
        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(
            ctypes.sizeof(_BITMAPINFOHEADER), image_width, -image_height,
            1, 32, 0, 0, 0, 0, 0, 0,
        )
        screen_dc = self.user32.GetDC(None)
        memory_dc = self.gdi32.CreateCompatibleDC(screen_dc)
        pixels = ctypes.c_void_p()
        bitmap = self.gdi32.CreateDIBSection(
            memory_dc, ctypes.byref(info), 0, ctypes.byref(pixels), None, 0,
        )
        if not bitmap or not pixels:
            self.gdi32.DeleteDC(memory_dc)
            self.user32.ReleaseDC(None, screen_dc)
            raise ctypes.WinError(ctypes.get_last_error())
        previous = self.gdi32.SelectObject(memory_dc, bitmap)
        buffer = (ctypes.c_uint32 * (image_width * image_height)).from_address(pixels.value)
        pixels = self._alpha_mask(width, height)
        phase = round(self.phase * 256) & 255
        color_table = (
            self._color_table[(256 - phase) * 256:] + self._color_table[:(256 - phase) * 256]
            if phase else self._color_table
        )
        for pixel, alpha, position in pixels:
            buffer[pixel] = color_table[position * 256 + alpha]

        source = _POINT(0, 0)
        destination = _POINT(x - pad, y - pad)
        size = _SIZE(image_width, image_height)
        blend = _BLENDFUNCTION(0, 0, 255, 1)
        updated = self.user32.UpdateLayeredWindow(
            self.hwnd, screen_dc, ctypes.byref(destination), ctypes.byref(size),
            memory_dc, ctypes.byref(source), 0, ctypes.byref(blend), 2,
        )
        self.gdi32.SelectObject(memory_dc, previous)
        self.gdi32.DeleteObject(bitmap)
        self.gdi32.DeleteDC(memory_dc)
        self.user32.ReleaseDC(None, screen_dc)
        if not updated:
            raise ctypes.WinError(ctypes.get_last_error())

        flags = 0x0010 | (0x0040 if self.visible else 0)
        if not self.user32.SetWindowPos(
            self.hwnd, self.root.winfo_id(), x - pad, y - pad,
            image_width, image_height, flags,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def move(self, x, y, width, height):
        self.bounds = (x, y, width, height)
        pad = self.PADDING
        flags = 0x0010 | (0x0040 if self.visible else 0)
        if not self.user32.SetWindowPos(
            self.hwnd, self.root.winfo_id(), x - pad, y - pad,
            width + pad * 2, height + pad * 2, flags,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def animate(self):
        if self.visible and self.bounds is not None and not self.paused:
            self.phase = (self.phase + self.speed) % 1
            self.update(*self.bounds)
        self.root.after(80, self.animate)

    def show(self):
        self.visible = True
        self.user32.ShowWindow(self.hwnd, 4)

    def hide(self):
        self.visible = False
        self.user32.ShowWindow(self.hwnd, 0)

    def close(self):
        self.user32.DestroyWindow(self.hwnd)


class _GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, value):
        parsed = uuid.UUID(value)
        return cls(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8)(*parsed.bytes[8:]),
        )


def _com_method(interface, index, result_type, *arg_types):
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *arg_types)(vtable[index])


class AudioVolume:
    def __init__(self):
        self._ole32 = ctypes.OleDLL("ole32")
        self._ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._ole32.CoCreateInstance.argtypes = (
            ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        )
        self._ole32.CoCreateInstance.restype = ctypes.c_long
        self._initialized = False
        self._interfaces = []

        result = self._ole32.CoInitializeEx(None, 0)
        if result < 0:
            raise OSError(result, "Could not initialize Windows audio controls")
        self._initialized = True
        try:
            enumerator = self._create(
                "BCDE0395-E52F-467C-8E3D-C4579291692E",
                "A95664D2-9614-4F35-A746-DE8DB63617E6",
            )
            device = ctypes.c_void_p()
            result = _com_method(
                enumerator, 4, ctypes.c_long, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_void_p),
            )(enumerator, 0, 1, ctypes.byref(device))
            self._check(result, "Could not find the default audio device")
            self._interfaces.append(device)

            endpoint = ctypes.c_void_p()
            result = _com_method(
                device, 3, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.c_ulong,
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            )(
                device,
                ctypes.byref(_GUID.parse("5CDF2C82-841E-4546-9722-0CF74078229A")),
                23, None, ctypes.byref(endpoint),
            )
            self._check(result, "Could not access the default audio volume")
            self._interfaces.append(endpoint)
            self._endpoint = endpoint
        except OSError:
            self.close()
            raise

    def _create(self, class_id, interface_id):
        instance = ctypes.c_void_p()
        result = self._ole32.CoCreateInstance(
            ctypes.byref(_GUID.parse(class_id)), None, 23,
            ctypes.byref(_GUID.parse(interface_id)), ctypes.byref(instance),
        )
        self._check(result, "Could not initialize Windows audio controls")
        self._interfaces.append(instance)
        return instance

    @staticmethod
    def _check(result, message):
        if result < 0:
            raise OSError(result, message)

    def get(self):
        value = ctypes.c_float()
        result = _com_method(
            self._endpoint, 9, ctypes.c_long, ctypes.POINTER(ctypes.c_float),
        )(self._endpoint, ctypes.byref(value))
        self._check(result, "Could not read the system volume")
        return value.value

    def set(self, value):
        result = _com_method(
            self._endpoint, 7, ctypes.c_long, ctypes.c_float, ctypes.POINTER(_GUID),
        )(self._endpoint, value, None)
        self._check(result, "Could not change the system volume")

    def close(self):
        for interface in reversed(self._interfaces):
            _com_method(interface, 2, ctypes.c_ulong)(interface)
        self._interfaces.clear()
        if self._initialized:
            self._ole32.CoUninitialize()
            self._initialized = False


class DynamicIslandApp(tk.Tk):
    CONFIG_PATH = Path(os.environ.get("APPDATA", Path.home())) / "DynamicIsland" / "settings.json"
    DEFAULT_CONFIG = {
        "glow_color": "Aurora",
        "glow_speed": 0.008,
        "cpu_alert": 85,
        "memory_alert": 88,
        "battery_alert": 15,
        "disk_alert": 10,
        "launch_on_startup": False,
        "app_notifications": True,
        "notification_permission_prompted": False,
        "position": None,
        "position_preset": "Top Center",
    }
    GLOW_PALETTES = {
        "Aurora": ((75, 235, 255), (80, 125, 255), (185, 90, 255), (255, 90, 210), (75, 235, 255)),
        "Ocean": ((50, 240, 220), (20, 140, 255), (50, 90, 220), (50, 240, 220)),
        "Sunset": ((255, 205, 70), (255, 110, 65), (245, 70, 155), (255, 205, 70)),
        "Mono": ((240, 248, 255), (90, 150, 210), (240, 248, 255)),
    }

    def __init__(self):
        super().__init__()
        self.config = self.load_config()
        if self.config["glow_color"] not in self.GLOW_PALETTES:
            self.config["glow_color"] = self.DEFAULT_CONFIG["glow_color"]
        self.withdraw()
        self.title("Dynamic Island")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.98)
        self.configure(bg="#000000")

        self.min_width = 230
        self.min_height = 42
        self.max_width = 440
        self.max_height = 220
        self.current_width = self.min_width
        self.current_height = self.min_height
        self.expanded = False
        self.notification_active = False
        self._dismissed_alert_key = None
        self._active_alert_key = None
        self._last_notification = 0
        self._external_notifications = deque()
        self._seen_notification_ids = OrderedDict()
        self._notification_scan_running = False
        self._notification_scan_result = None
        self._notification_baselined = False
        self._notification_status = "Checking Windows notification access..."
        self._animation_job = None
        self._hold_started_at = None
        self._pending_notification = ""
        self._position = self.config["position"]
        self._position_changed = self._position is not None
        self._press = None
        self._dragging = False
        self._volume_dragging = False
        self._settings_window = None
        self._usb_open = False
        self._usb_closing = False
        self._usb_devices = []
        self._usb_status = "USB device list not loaded"
        self._usb_scan_token = 0
        self._usb_scan_result = None
        self._quick_open = False
        self._quick_closing = False
        self._media_track = "Media keys ready; metadata unavailable"
        self._media_scan_running = False
        self._media_scan_result = None
        self._network_previous = None
        self.audio = None
        self._volume = 0.0
        try:
            self.audio = AudioVolume()
            self._volume = self.audio.get()
        except OSError:
            if self.audio is not None:
                self.audio.close()
            self.audio = None
        self._get_async_key_state = ctypes.windll.user32.GetAsyncKeyState
        self._get_async_key_state.argtypes = [ctypes.c_int]
        self._get_async_key_state.restype = ctypes.c_short
        self._get_cursor_pos = ctypes.windll.user32.GetCursorPos
        self._get_cursor_pos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self._get_cursor_pos.restype = wintypes.BOOL

        self.glow = SoftGlow(self)
        self.glow.set_palette(self.config["glow_color"])
        self.glow.speed = self.config["glow_speed"]
        self._metrics = self.read_metrics()
        self.canvas = tk.Canvas(
            self,
            width=self.max_width,
            height=self.max_height,
            bg="#000000",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self.update_position()
        self.draw_ui()
        self.deiconify()
        self.lift()
        self.glow.show()
        self.update_position()
        self.after(100, self.glow.animate)
        self.after(300, self.update_loop)
        self.after(50, self.check_reopen_gesture)
        self.after(600, self.refresh_media_async)
        if self.config["app_notifications"]:
            self.after(900, self.refresh_app_notifications_async)

    @classmethod
    def load_config(cls):
        try:
            with cls.CONFIG_PATH.open(encoding="utf-8") as file:
                stored = json.load(file)
            if not isinstance(stored, dict):
                raise ValueError("Settings file must contain a JSON object")
            config = {**cls.DEFAULT_CONFIG, **stored}
            config["glow_color"] = config["glow_color"] if config["glow_color"] in cls.GLOW_PALETTES else "Aurora"
            config["glow_speed"] = max(0.002, min(0.03, float(config["glow_speed"])))
            for key, minimum, maximum in (
                ("cpu_alert", 50, 100), ("memory_alert", 50, 100),
                ("battery_alert", 5, 50), ("disk_alert", 1, 50),
            ):
                config[key] = max(minimum, min(maximum, int(config[key])))
            config.pop("temperature_alert", None)
            if config["position"] is not None:
                position = config["position"]
                if not isinstance(position, (list, tuple)) or len(position) != 2:
                    raise ValueError("Position setting must contain two coordinates")
                config["position"] = tuple(int(value) for value in position)
            config["launch_on_startup"] = bool(config["launch_on_startup"])
            config["app_notifications"] = bool(config["app_notifications"])
            config["notification_permission_prompted"] = bool(
                config.get("notification_permission_prompted", False)
            )
            valid_positions = {
                "Top Left", "Top Center", "Top Right", "Center Left", "Center",
                "Center Right", "Bottom Left", "Bottom Center", "Bottom Right", "Custom",
            }
            if config["position_preset"] not in valid_positions:
                config["position_preset"] = "Top Center"
            if config["position"] is None and config["position_preset"] == "Center":
                config["position_preset"] = "Top Center"
            return config
        except FileNotFoundError:
            return dict(cls.DEFAULT_CONFIG)
        except (OSError, json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
            messagebox.showerror("Settings", f"Could not load settings:\n{exc}")
            return dict(cls.DEFAULT_CONFIG)

    def save_config(self):
        try:
            self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.CONFIG_PATH.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(self.config, file, indent=2)
            temporary.replace(self.CONFIG_PATH)
        except OSError as exc:
            messagebox.showerror("Settings", f"Could not save settings:\n{exc}", parent=self)

    def get_work_area(self):
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        return rect.left, rect.top, rect.right, rect.bottom

    def update_position(self):
        left, top, right, bottom = self.get_work_area()
        width, height = self.current_width, self.current_height
        if self._usb_open or self._quick_open:
            x, y = self.panel_origin(width, height)
        else:
            x, y = self.main_origin(width, height)
        x = max(left, min(x, right - width))
        y = max(top, min(y, bottom - height))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.apply_rounded_window_region(width, height)
        self.glow.update(x, y, width, height)

    def panel_origin(self, width, height):
        left, top, right, bottom = self.get_work_area()
        preset = self.config.get("position_preset")
        base_x, base_y = self._position or self.preset_anchor(preset)
        if preset.endswith("Center"):
            panel_x = base_x + (self.min_width - width) // 2
        else:
            panel_x = base_x + self.min_width - width if base_x + self.min_width / 2 > (left + right) / 2 else base_x
        above = self.panel_is_above(height)
        panel_y = base_y + self.min_height - height if above else base_y
        return max(left, min(panel_x, right - width)), max(top, min(panel_y, bottom - height))

    def main_origin(self, width, height):
        left, top, right, bottom = self.get_work_area()
        preset = self.config.get("position_preset")
        base_x, base_y = self._position or self.preset_anchor(preset)
        if preset == "Center":
            x = left + (right - left - width) // 2
            y = top + (bottom - top - height) // 2
            return x, y
        if preset.endswith("Center"):
            x = base_x + (self.min_width - width) // 2
        else:
            x = base_x + self.min_width - width if base_x + self.min_width / 2 > (left + right) / 2 else base_x
        if preset.startswith("Center"):
            y = base_y + (self.min_height - height) // 2
        else:
            y = base_y + self.min_height - height if base_y + self.min_height / 2 > (top + bottom) / 2 else base_y
        return max(left, min(x, right - width)), max(top, min(y, bottom - height))

    def preset_anchor(self, preset):
        left, top, right, bottom = self.get_work_area()
        if preset == "Center":
            return (
                left + (right - left - self.min_width) // 2,
                top + (bottom - top - self.min_height) // 2,
            )
        horizontal = preset.split()[-1] if preset else "Center"
        vertical = preset.split()[0] if preset and preset.startswith(("Top", "Bottom")) else "Center"
        x = (
            left if horizontal == "Left"
            else right - self.min_width - 24 if horizontal == "Right"
            else left + (right - left - self.min_width) // 2
        )
        y = (
            top if vertical == "Top"
            else bottom - self.min_height - 12 if vertical == "Bottom"
            else top + (bottom - top - self.min_height) // 2
        )
        return x, y

    def panel_is_above(self, height):
        left, top, right, bottom = self.get_work_area()
        _, base_y = self._position or self.preset_anchor(self.config.get("position_preset"))
        space_above = base_y - top
        space_below = bottom - (base_y + self.min_height)
        return space_above >= height - self.min_height or space_above >= space_below

    def panel_island_y(self, height):
        return height - self.min_height if self.panel_is_above(height) else 0

    def apply_rounded_window_region(self, width, height):
        create_region = ctypes.windll.gdi32.CreateRoundRectRgn
        create_region.argtypes = [ctypes.c_int] * 6
        create_region.restype = wintypes.HANDLE
        set_region = ctypes.windll.user32.SetWindowRgn
        set_region.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        set_region.restype = ctypes.c_int
        delete_object = ctypes.windll.gdi32.DeleteObject
        delete_object.argtypes = [wintypes.HANDLE]
        delete_object.restype = wintypes.BOOL

        hwnd = self.winfo_id()
        radius = min(24, height // 2)
        region = create_region(0, 0, width + 1, height + 1, radius * 2, radius * 2)
        if not set_region(hwnd, region, True):
            delete_object(region)

    def draw_ui(self):
        self.canvas.delete("all")
        fill = "#0f172a"
        glow = "#8bd4ff"
        if self._usb_open or self._quick_open:
            if self._usb_open:
                self.draw_usb_panel()
            else:
                self.draw_quick_panel()
            return

        points = self.rounded_points(
            0, 0, self.current_width, self.current_height,
            min(24, self.current_height // 2),
        )
        self.canvas.create_polygon(
            points, smooth=True, splinesteps=12, fill=fill, outline=fill,
        )
        self.canvas.create_rectangle(0, self.current_height - 2, self.current_width, self.current_height, fill="#111827", outline="")
        self.draw_reset_button()
        self.draw_close_button()

        if not self.expanded and not self.notification_active:
            self.canvas.create_oval(16, 14, 27, 25, fill=glow, outline="")
            self.canvas.create_text(38, 20, text="Live", fill="#edf5ff", font=("Segoe UI", 9, "bold"), anchor="w")
            self.draw_usb_button()
            self.draw_settings_button()
            self.draw_quick_button()
            return

        self.canvas.create_oval(14, 14, 25, 25, fill="#7dd3fc", outline="")
        self.canvas.create_text(35, 20, text="Live", fill="#edf5ff", font=("Segoe UI", 9, "bold"), anchor="w")
        self.draw_usb_button()
        self.draw_settings_button()
        self.draw_quick_button()

        if self.notification_active:
            self.canvas.create_text(18, 46, text="⚠", fill="#ffd369", font=("Segoe UI", 11, "bold"), anchor="w")
            self.canvas.create_text(
                42, 46, text=self._pending_notification, fill="#fff3c4",
                font=("Segoe UI", 9, "bold"), anchor="w",
                width=self.current_width - 80,
            )
            self.canvas.create_text(self.current_width - 18, 46, text="×", fill="#dbeafe", font=("Segoe UI", 10, "bold"), anchor="e")
        else:
            self.canvas.create_text(18, 46, text="CPU", fill="#bfe8ff", font=("Segoe UI", 8, "bold"), anchor="w")
            self.canvas.create_text(62, 46, text=f"{self._metrics['cpu']:.0f}%", fill="#edf5ff", font=("Segoe UI", 9, "bold"), anchor="w")
            self.canvas.create_text(140, 46, text="RAM", fill="#bfe8ff", font=("Segoe UI", 8, "bold"), anchor="w")
            self.canvas.create_text(185, 46, text=f"{self._metrics['memory']:.0f}%", fill="#edf5ff", font=("Segoe UI", 9, "bold"), anchor="w")

        self.canvas.create_line(20, 62, self.current_width - 20, 62, fill="#1f2f42", width=1)
        self.canvas.create_text(18, 78, text=datetime.now().strftime("%H:%M:%S"), fill="#ebf5ff", font=("Segoe UI", 10, "bold"), anchor="w")
        battery = self._metrics["battery"]
        if battery:
            self.canvas.create_text(self.current_width - 112, 78, text=f"Batt {battery.percent}%", fill="#b8f5c9", font=("Segoe UI", 9, "bold"), anchor="e")
        else:
            self.canvas.create_text(self.current_width - 112, 78, text="Battery N/A", fill="#b8f5c9", font=("Segoe UI", 9, "bold"), anchor="e")

        self.canvas.create_text(self.current_width - 18, 78, text=f"Disk {self._metrics['disk']:.0f}%", fill="#d9e7ff", font=("Segoe UI", 9, "bold"), anchor="e")
        self.canvas.create_text(
            18, 100, text=self._metrics["network"],
            fill="#bfe8ff", font=("Segoe UI", 8), anchor="w",
        )
        self.draw_media_controls()
        self.draw_volume_control()

    def draw_usb_panel(self):
        width, height = self.current_width, self.current_height
        island_y = self.panel_island_y(height)
        panel_top = 0 if island_y else self.min_height
        panel_bottom = island_y if island_y else height
        panel_points = self.rounded_points(0, 0, width, height, min(24, height // 2))
        self.canvas.create_polygon(
            panel_points, smooth=True, splinesteps=12, fill="#0f172a", outline="#0f172a",
        )
        self.canvas.create_rectangle(
            0, panel_top + 22, width, panel_bottom, fill="#111c2e", outline="",
        )
        self.canvas.create_text(
            18, panel_top + 42, text="USB DEVICES", fill="#edf5ff",
            font=("Segoe UI", 10, "bold"), anchor="w",
        )
        self.canvas.create_text(
            width - 18, panel_top + 42, text=f"{len(self._usb_devices)} connected",
            fill="#9fb2c8", font=("Segoe UI", 8), anchor="e",
        )
        if self._usb_devices:
            for index, device in enumerate(self._usb_devices[:6]):
                row_y = panel_top + 70 + index * 27
                self.canvas.create_text(
                    20, row_y, text="▣", fill="#7dd3fc",
                    font=("Segoe UI Symbol", 10), anchor="w",
                )
                self.canvas.create_text(
                    42, row_y, text=device[:48], fill="#dbeafe",
                    font=("Segoe UI", 8), anchor="w", width=width - 58,
                )
        else:
            self.canvas.create_text(
                20, panel_top + 82, text=self._usb_status, fill="#9fb2c8",
                font=("Segoe UI", 9), anchor="w",
            )

        self.canvas.create_text(
            width - 55, panel_bottom - 16, text="Refresh",
            fill="#7dd3fc", font=("Segoe UI", 8, "bold"),
            tags=("usb_refresh",),
        )
        self.canvas.create_line(
            16, island_y, width - 16, island_y, fill="#263449",
        )
        self.canvas.create_rectangle(
            0, island_y, width, island_y + self.min_height, fill="#0f172a", outline="",
        )
        self.canvas.create_oval(16, island_y + 14, 27, island_y + 25, fill="#7dd3fc", outline="")
        self.canvas.create_text(
            38, island_y + 20, text="Live", fill="#edf5ff",
            font=("Segoe UI", 9, "bold"), anchor="w",
        )
        self.draw_reset_button(island_y + 20)
        self.draw_usb_button(island_y + 20)
        self.draw_settings_button(island_y + 20)
        self.draw_close_button(island_y + 20)

    def draw_quick_panel(self):
        width, height = self.current_width, self.current_height
        island_y = self.panel_island_y(height)
        panel_top = 0 if island_y else self.min_height
        panel_bottom = island_y if island_y else height
        self.canvas.create_polygon(
            self.rounded_points(0, 0, width, height, min(24, height // 2)),
            smooth=True, splinesteps=12, fill="#0f172a", outline="#0f172a",
        )
        self.canvas.create_rectangle(0, panel_top + 22, width, panel_bottom, fill="#111c2e", outline="")
        self.canvas.create_text(18, panel_top + 42, text="QUICK ACTIONS", fill="#edf5ff", font=("Segoe UI", 10, "bold"), anchor="w")
        for index, action in enumerate((
            "Take screenshot", "Open Notepad", "Open Calculator",
            "Open browser", "Focus Assist settings",
        )):
            row_y = panel_top + 73 + index * 31
            self.canvas.create_text(22, row_y, text="›", fill="#7dd3fc", font=("Segoe UI", 12, "bold"), anchor="w")
            self.canvas.create_text(
                42, row_y, text=action, fill="#dbeafe", font=("Segoe UI", 9),
                anchor="w", tags=(f"quick_{index}",),
            )
        self.canvas.create_line(16, island_y, width - 16, island_y, fill="#263449")
        self.canvas.create_rectangle(0, island_y, width, island_y + self.min_height, fill="#0f172a", outline="")
        self.canvas.create_oval(16, island_y + 14, 27, island_y + 25, fill="#7dd3fc", outline="")
        self.canvas.create_text(38, island_y + 20, text="Live", fill="#edf5ff", font=("Segoe UI", 9, "bold"), anchor="w")
        self.draw_reset_button(island_y + 20)
        self.draw_usb_button(island_y + 20)
        self.draw_settings_button(island_y + 20)
        self.draw_quick_button(island_y + 20)
        self.draw_close_button(island_y + 20)

    @staticmethod
    def rounded_points(x1, y1, x2, y2, radius):
        points = []
        for center_x, center_y, start in (
            (x2 - radius, y1 + radius, -90),
            (x2 - radius, y2 - radius, 0),
            (x1 + radius, y2 - radius, 90),
            (x1 + radius, y1 + radius, 180),
        ):
            for index in range(7):
                angle = math.radians(start + index * 90 / 6)
                points.extend((
                    center_x + radius * math.cos(angle),
                    center_y + radius * math.sin(angle),
                ))
        return points

    def draw_volume_control(self):
        if not self.expanded:
            return
        if self.audio is None:
            self.canvas.create_text(
                18, 188, text="VOL N/A", fill="#bfe8ff",
                font=("Segoe UI", 8, "bold"), anchor="w",
            )
            return
        start, end, y = 100, self.current_width - 20, 188
        self.canvas.create_text(
            18, y, text=f"VOL {round(self._volume * 100)}%",
            fill="#bfe8ff", font=("Segoe UI", 8, "bold"), anchor="w",
            tags=("volume_label",),
        )
        self.canvas.create_line(
            start, y, end, y, fill="#334155", width=5, capstyle=tk.ROUND,
            tags=("volume_track",),
        )
        self.canvas.create_line(
            start, y, start + (end - start) * self._volume, y,
            fill="#7dd3fc", width=5, capstyle=tk.ROUND,
            tags=("volume_fill",),
        )

    def set_volume_from_x(self, x):
        if self.audio is None:
            return
        start, end = 100, self.current_width - 20
        value = max(0.0, min(1.0, (x - start) / (end - start)))
        if abs(value - self._volume) < 0.005:
            return
        try:
            self.audio.set(value)
        except OSError as exc:
            self.disable_audio(exc)
            return
        self._volume = value
        self.canvas.itemconfigure("volume_label", text=f"VOL {round(value * 100)}%")
        self.canvas.coords("volume_fill", start, 188, start + (end - start) * value, 188)

    def disable_audio(self, error):
        if self.audio is not None:
            self.audio.close()
            self.audio = None
        messagebox.showerror("Volume control", f"System volume control stopped:\n{error}", parent=self)

    def draw_close_button(self, y=20):
        x = self.current_width - 21
        self.canvas.create_oval(
            x - 10, y - 10, x + 10, y + 10,
            fill="#263449",
            outline="#52647c",
            tags=("close_button",),
        )
        self.canvas.create_text(
            x, y,
            text="×",
            fill="#edf5ff",
            font=("Segoe UI", 11, "bold"),
            tags=("close_button",),
        )

    def draw_settings_button(self, y=20):
        self.canvas.create_text(
            82, y, text="⚙", fill="#b9cde4",
            font=("Segoe UI Symbol", 10), tags=("settings_button",),
        )

    def draw_usb_button(self, y=20):
        self.canvas.create_text(
            106, y, text="▣", fill="#b9cde4",
            font=("Segoe UI Symbol", 10), tags=("usb_button",),
        )

    def draw_quick_button(self, y=20):
        self.canvas.create_text(
            134, y, text="⚡", fill="#b9cde4",
            font=("Segoe UI Symbol", 10), tags=("quick_button",),
        )

    def draw_media_controls(self):
        self.canvas.create_text(
            18, 116, text=self._media_track[:64],
            fill="#dbeafe", font=("Segoe UI", 8), anchor="w",
        )
        for x, text, tag in (
            (250, "◀", "media_previous"),
            (290, "▶/Ⅱ", "media_play"),
            (340, "▶", "media_next"),
        ):
            self.canvas.create_text(
                x, 160, text=text, fill="#7dd3fc",
                font=("Segoe UI", 10, "bold"), tags=(tag,),
            )

    def open_settings(self):
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self.close_settings()
            return

        window = tk.Toplevel(self)
        self._settings_window = window
        window.title("Dynamic Island Settings")
        window.resizable(False, False)
        window.configure(bg="#0f172a")
        window.transient(self)
        window.geometry(self.settings_window_geometry(360, 670))
        window.protocol("WM_DELETE_WINDOW", self.close_settings)

        canvas = tk.Canvas(window, bg="#0f172a", highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(window, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg="#0f172a")
        content_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind(
            "<Configure>",
            lambda event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(content_id, width=event.width),
        )
        window = content
        label = {"bg": "#0f172a", "fg": "#edf5ff", "font": ("Segoe UI", 9)}
        tk.Label(window, text="Island settings", **{**label, "font": ("Segoe UI", 12, "bold")}).pack(anchor="w", padx=14, pady=(12, 8))
        tk.Label(window, text="Glow palette", **label).pack(anchor="w", padx=14)
        self._setting_option(window, self.config["glow_color"], tuple(self.GLOW_PALETTES), self.set_glow_palette)
        tk.Label(window, text="Glow speed", **label).pack(anchor="w", padx=14, pady=(8, 0))
        self._glow_speed_var = tk.DoubleVar(value=self.config["glow_speed"])
        tk.Scale(
            window, from_=0.002, to=0.03, resolution=0.001, orient="horizontal",
            variable=self._glow_speed_var, command=self.set_glow_speed,
            bg="#0f172a", fg="#edf5ff", troughcolor="#263449", highlightthickness=0,
        ).pack(fill="x", padx=14)
        tk.Label(window, text="Position preset", **label).pack(anchor="w", padx=14)
        current_position = self.config.get("position_preset", "Top Center")
        self._setting_option(
            window, current_position,
            ("Top Left", "Top Center", "Top Right", "Center Left", "Center",
             "Center Right", "Bottom Left", "Bottom Center", "Bottom Right"),
            self.set_position_preset,
        )
        tk.Label(
            window,
            text="If browser video has no title, enable Hardware Media Key Handling in the browser's flags and restart it.",
            wraplength=320, justify="left", **label,
        ).pack(anchor="w", padx=14, pady=(8, 4))
        tk.Label(window, text="Alert thresholds (%)", **label).pack(anchor="w", padx=14, pady=(4, 0))
        for key, caption, low, high in (
            ("cpu_alert", "CPU", 50, 100),
            ("memory_alert", "Memory", 50, 100),
            ("battery_alert", "Low battery", 5, 50),
            ("disk_alert", "Low disk space", 1, 50),
        ):
            row = tk.Frame(window, bg="#0f172a")
            row.pack(fill="x", padx=14, pady=2)
            tk.Label(row, text=caption, width=15, anchor="w", **label).pack(side="left")
            variable = tk.IntVar(value=self.config[key])
            setattr(self, f"_{key}_var", variable)
            tk.Scale(
                row, from_=low, to=high, orient="horizontal", variable=variable,
                command=lambda value, config_key=key: self.set_alert_threshold(config_key, value),
                showvalue=True, length=190, bg="#0f172a", fg="#edf5ff",
                troughcolor="#263449", highlightthickness=0,
            ).pack(side="right")
        self._startup_var = tk.BooleanVar(value=self.config["launch_on_startup"])
        tk.Checkbutton(
            window, text="Launch when I sign in (off by default)",
            variable=self._startup_var, command=self.set_launch_on_startup,
            bg="#0f172a", fg="#edf5ff", selectcolor="#263449",
            activebackground="#0f172a", activeforeground="#edf5ff",
        ).pack(anchor="w", padx=10, pady=8)
        self._app_notifications_var = tk.BooleanVar(value=self.config["app_notifications"])
        tk.Checkbutton(
            window, text="Show notifications from other apps",
            variable=self._app_notifications_var, command=self.set_app_notifications,
            bg="#0f172a", fg="#edf5ff", selectcolor="#263449",
            activebackground="#0f172a", activeforeground="#edf5ff",
        ).pack(anchor="w", padx=10, pady=(4, 2))
        self._notification_status_text = tk.StringVar(value=self._notification_status)
        tk.Label(
            window, textvariable=self._notification_status_text,
            wraplength=320, justify="left", **label,
        ).pack(anchor="w", padx=14, pady=(0, 4))
        tk.Button(
            window, text="Windows notification settings",
            command=self.open_windows_notification_settings,
            bg="#1e3a5f", fg="#dbeafe", activebackground="#274b76",
            activeforeground="#ffffff", relief="flat",
        ).pack(anchor="w", padx=14, pady=(0, 8))

    def settings_window_geometry(self, requested_width, requested_height):
        left, top, right, bottom = self.get_work_area()
        width = min(requested_width, right - left)
        height = min(requested_height, bottom - top)
        anchor_x = self.winfo_x()
        anchor_y = self.winfo_y()
        x = anchor_x - width - 12
        if x < left:
            x = anchor_x + self.current_width + 12
        x = max(left, min(x, right - width))
        y = max(top, min(anchor_y, bottom - height))
        return f"{width}x{height}+{x}+{y}"

    def set_app_notifications(self):
        enabled = self._app_notifications_var.get()
        self.config["app_notifications"] = enabled
        self._notification_baselined = False
        if not enabled:
            self._external_notifications.clear()
        self._notification_status = (
            "Checking Windows notification access..." if enabled
            else "Notifications from other apps are off."
        )
        if self._settings_window is not None and hasattr(self, "_notification_status_text"):
            self._notification_status_text.set(self._notification_status)
        self.save_config()
        if enabled:
            self.refresh_app_notifications_async()

    def open_windows_notification_settings(self):
        try:
            os.startfile(WINDOWS_NOTIFICATIONS_SETTINGS)
        except OSError as exc:
            messagebox.showerror(
                "Windows notifications",
                f"Could not open notification settings:\n{exc}",
                parent=self._settings_window or self,
            )

    def _setting_option(self, parent, value, choices, callback):
        variable = tk.StringVar(value=value)
        if not hasattr(parent, "_option_vars"):
            parent._option_vars = []
        parent._option_vars.append(variable)
        tk.OptionMenu(parent, variable, *choices, command=callback).pack(anchor="w", padx=14)

    def set_glow_palette(self, value):
        self.config["glow_color"] = value
        self.glow.set_palette(value)
        self.glow.update(*self.glow.bounds)
        self.save_config()

    def set_glow_speed(self, value):
        self.config["glow_speed"] = float(value)
        self.glow.speed = float(value)
        self.save_config()

    def set_alert_threshold(self, key, value):
        self.config[key] = int(float(value))
        self.save_config()

    def set_position_preset(self, value):
        left, top, right, bottom = self.get_work_area()
        width, height = self.min_width, self.min_height
        horizontal = value.split()[-1]
        vertical = value.split()[0] if value.startswith(("Top", "Bottom")) else (
            value.split()[0] if value == "Center" else "Center"
        )
        x = left if horizontal == "Left" else right - width - 24 if horizontal == "Right" else left + (right - left - width) // 2
        y = top if vertical == "Top" else bottom - height - 12 if vertical == "Bottom" else top + (bottom - top - height) // 2
        self._position = None if value in ("Center", "Top Center") else (x, y)
        self._position_changed = value not in ("Center", "Top Center")
        self.config["position"] = self._position
        self.config["position_preset"] = value
        self.save_config()
        self.update_position()
        self.draw_ui()

    def set_launch_on_startup(self):
        enabled = self._startup_var.get()
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    executable = Path(sys.executable)
                    python = executable.with_name("pythonw.exe")
                    if not python.exists():
                        python = executable
                    command = f'"{python}" "{Path(__file__).resolve()}"'
                    winreg.SetValueEx(key, "DynamicIsland", 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, "DynamicIsland")
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            self._startup_var.set(not enabled)
            messagebox.showerror("Startup setting", f"Could not update sign-in startup:\n{exc}", parent=self._settings_window)
            return
        self.config["launch_on_startup"] = enabled
        self.save_config()

    def open_usb_menu(self):
        if self._usb_open:
            self.close_usb_menu()
            return
        if self._quick_open:
            self.close_quick_menu(animate=False)

        self._usb_open = True
        self._usb_status = "Scanning connected USB devices..."
        self._usb_devices = []
        self.draw_ui()
        self.animate_to(self.max_width, 270)
        self.after(80, self.refresh_usb_devices)

    def open_quick_menu(self):
        if self._quick_open:
            self.close_quick_menu()
            return
        if self._usb_open:
            self.close_usb_menu(animate=False)
        self._quick_open = True
        self.animate_to(self.max_width, 270)

    def close_quick_menu(self, animate=True):
        if not self._quick_open:
            return
        if not animate:
            self._quick_open = False
            self._quick_closing = False
            return
        self._quick_closing = True
        target_height = self.max_height if self.expanded or self.notification_active else self.min_height
        self.animate_to(self.max_width if target_height == self.max_height else self.min_width, target_height)

    def run_quick_action(self, index):
        try:
            if index == 0:
                user32 = ctypes.windll.user32
                user32.keybd_event(0x5B, 0, 0, 0)
                user32.keybd_event(0x10, 0, 0, 0)
                user32.keybd_event(0x53, 0, 0, 0)
                user32.keybd_event(0x53, 0, 2, 0)
                user32.keybd_event(0x10, 0, 2, 0)
                user32.keybd_event(0x5B, 0, 2, 0)
            elif index == 1:
                subprocess.Popen(["notepad.exe"])
            elif index == 2:
                subprocess.Popen(["calc.exe"])
            elif index == 3:
                os.startfile("https://www.google.com")
            elif index == 4:
                os.startfile("ms-settings:quiethours")
        except OSError as exc:
            messagebox.showerror("Quick action", f"Could not run this action:\n{exc}", parent=self)
        self.close_quick_menu()

    def send_media_key(self, key):
        key_codes = {"previous": 0xB1, "play": 0xB3, "next": 0xB0}
        ctypes.windll.user32.keybd_event(key_codes[key], 0, 0, 0)
        ctypes.windll.user32.keybd_event(key_codes[key], 0, 2, 0)
        self._media_track = "Checking media session..."
        self.draw_ui()
        self.after(800, self.refresh_media_async)

    def refresh_app_notifications_async(self):
        if not self.config["app_notifications"] or self._notification_scan_running:
            return
        self._notification_scan_running = True
        self._notification_scan_result = None

        def scan():
            try:
                data = self.run_powershell_json(APP_NOTIFICATIONS_QUERY, timeout=12)
                status = str(data.get("status") or "Unknown")
                items = data.get("items") or []
                if isinstance(items, dict):
                    items = [items]
                if not isinstance(items, list):
                    raise ValueError("Windows returned an invalid notification list")
                notifications = [
                    {
                        "id": str(item.get("id", "")),
                        "app": str(item.get("app") or "App"),
                        "text": str(item.get("text") or "").strip(),
                    }
                    for item in items if isinstance(item, dict)
                ]
                result = (status, notifications, None)
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
                result = ("Error", [], str(exc))
            self._notification_scan_result = result

        threading.Thread(target=scan, daemon=True).start()
        self.after(100, self.poll_app_notifications)

    def poll_app_notifications(self):
        if not self.config["app_notifications"]:
            self._notification_scan_running = False
            return
        result = self._notification_scan_result
        if result is None:
            self.after(100, self.poll_app_notifications)
            return
        self._notification_scan_running = False
        status, notifications, error = result
        if error:
            self._notification_status = f"Windows notification listener error: {error[:120]}"
        elif status != "Allowed":
            self._notification_status = (
                f"Windows notification access is {status}. Enable Dynamic Island in "
                "Windows notification settings."
            )
            if not self.config["notification_permission_prompted"]:
                self.config["notification_permission_prompted"] = True
                self.save_config()
                self.after(100, self.ask_notification_permission)
        else:
            self._notification_status = "Listening for notifications from other apps."
            if not self._notification_baselined:
                for item in notifications:
                    self._remember_notification(item)
                self._notification_baselined = True
            else:
                for item in notifications:
                    if not self._remember_notification(item):
                        continue
                    text = item["text"]
                    if text:
                        message = f"{item['app']}: {text}"[:240]
                        self._external_notifications.append((item["id"], message))
                while len(self._external_notifications) > 30:
                    self._external_notifications.popleft()
                self.show_next_external_notification()
        if hasattr(self, "_notification_status_text"):
            self._notification_status_text.set(self._notification_status)
        if self.config["app_notifications"]:
            self.after(3000, self.refresh_app_notifications_async)

    def ask_notification_permission(self):
        open_settings = messagebox.askyesno(
            "Allow notification access",
            "Dynamic Island can show notifications from other apps. "
            "Open Windows notification settings to review access?",
            parent=self,
        )
        if open_settings:
            self.open_windows_notification_settings()

    def _remember_notification(self, item):
        notification_id = item.get("id")
        if not notification_id:
            return False
        key = f"{item.get('app', '')}:{notification_id}"
        if key in self._seen_notification_ids:
            return False
        self._seen_notification_ids[key] = None
        while len(self._seen_notification_ids) > 1000:
            self._seen_notification_ids.popitem(last=False)
        return True

    def show_next_external_notification(self):
        if self.notification_active or not self._external_notifications:
            return
        notification_id, message = self._external_notifications.popleft()
        self._pending_notification = message
        self._active_alert_key = f"app-notification:{notification_id}"
        self._last_notification = time.monotonic()
        self.notification_active = True
        self.expanded = True
        self._usb_open = self._quick_open = False
        self._usb_closing = self._quick_closing = False
        self.deiconify()
        self.lift()
        self.glow.show()
        self.animate_to(self.max_width, self.max_height)

    def show_queued_notification(self):
        if self._external_notifications and not self.notification_active:
            self.show_next_external_notification()

    @staticmethod
    def run_powershell_json(script, timeout=15):
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=True, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = result.stdout.strip()
        if not output:
            raise RuntimeError(result.stderr.strip() or "PowerShell returned no data")
        return json.loads(output)

    def refresh_media_async(self):
        if self._media_scan_running:
            return
        self._media_scan_running = True
        self._media_scan_result = None

        def scan():
            try:
                data = self.run_powershell_json(MEDIA_QUERY)
                title = str(data.get("title") or "").strip()
                artist = str(data.get("artist") or "").strip()
                app = str(data.get("app") or "").strip()
                playback = str(data.get("playback") or "").strip()
                if title and artist:
                    track = f"{title} — {artist} · {playback}"
                else:
                    track = (
                        f"{title or artist} · {app} · {playback}".strip(" ·")
                        if title or artist or app
                        else playback or "No media session active"
                    )
                result = track[:100]
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError, RuntimeError) as exc:
                result = f"Media info unavailable: {str(exc)[:55]}"
            self._media_scan_result = result

        threading.Thread(target=scan, daemon=True).start()
        self.after(100, self.poll_media_scan)

    def poll_media_scan(self):
        result = self._media_scan_result
        if result is None:
            self.after(100, self.poll_media_scan)
            return
        self._media_scan_running = False
        self._media_track = result
        if self.state() != "withdrawn":
            self.draw_ui()
        self.after(3000, self.refresh_media_async)

    def refresh_usb_devices(self):
        if not self._usb_open:
            return
        command = (
            "Get-PnpDevice -PresentOnly | "
            "Where-Object { $_.InstanceId -like 'USB\\*' -or "
            "$_.InstanceId -like 'USBSTOR\\*' } | "
            "Select-Object FriendlyName, Class, Status | ConvertTo-Json -Compress"
        )
        self._usb_status = "Scanning connected USB devices..."
        self.draw_ui()
        self._usb_scan_token += 1
        token = self._usb_scan_token
        self._usb_scan_result = None

        def scan():
            try:
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                    check=True, capture_output=True, text=True, timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                devices = json.loads(result.stdout) if result.stdout.strip() else []
                if isinstance(devices, dict):
                    devices = [devices]
                if not isinstance(devices, list):
                    devices = []
                names = [
                    f"{item.get('FriendlyName') or 'Unknown device'}"
                    f"  |  {item.get('Class') or 'Unknown class'}"
                    f"  |  {item.get('Status') or 'Unknown status'}"
                    for item in devices if isinstance(item, dict)
                ]
                status = (
                    f"{len(names)} connected USB device(s) found."
                    if names else "No connected USB devices found."
                )
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                names, status = [], f"Could not read USB devices: {detail.strip()}"
            if token == self._usb_scan_token:
                self._usb_scan_result = (token, names, status)

        threading.Thread(target=scan, daemon=True).start()
        self.after(50, self.poll_usb_scan)

    def poll_usb_scan(self):
        if not self._usb_open:
            return
        result = self._usb_scan_result
        if result is None:
            self.after(50, self.poll_usb_scan)
            return
        token, devices, status = result
        if token == self._usb_scan_token and self._usb_open:
            self._usb_devices, self._usb_status = devices, status
            self.draw_ui()

    def close_settings(self):
        if self._settings_window is not None:
            self._settings_window.destroy()
        self._settings_window = None

    def close_usb_menu(self, animate=True):
        if not self._usb_open:
            return
        if not animate:
            self._usb_open = False
            self._usb_closing = False
            return
        self._usb_closing = True
        target_height = self.max_height if self.expanded or self.notification_active else self.min_height
        self.animate_to(self.max_width if target_height == self.max_height else self.min_width, target_height)

    def draw_reset_button(self, y=20):
        if not self._position_changed:
            return
        x = self.current_width - 49
        self.canvas.create_oval(
            x - 9, y - 9, x + 9, y + 9, fill="#263449", outline="#52647c",
            tags=("reset_button",),
        )
        self.canvas.create_text(
            x, y, text="↺", fill="#edf5ff", font=("Segoe UI", 10, "bold"),
            tags=("reset_button",),
        )

    def close_app(self, event=None):
        if self._animation_job is not None:
            self.after_cancel(self._animation_job)
            self._animation_job = None
        self.glow.paused = False
        self.close_settings()
        self.close_usb_menu(animate=False)
        self.close_quick_menu(animate=False)
        if self.audio is not None:
            self.audio.close()
        self.glow.close()
        self.destroy()
        return "break"

    def animate_to(self, target_w, target_h):
        if self._animation_job is not None:
            self.after_cancel(self._animation_job)
            self._animation_job = None

        left, top, right, bottom = self.get_work_area()
        target_w = min(target_w, right - left)
        target_h = min(target_h, bottom - top)
        self.glow.paused = True
        start_w = self.current_width
        start_h = self.current_height
        step_count = 6

        def _step(i):
            if i >= step_count:
                self.current_width = target_w
                self.current_height = target_h
                self.glow.paused = False
                if self._usb_closing:
                    self._usb_open = False
                    self._usb_closing = False
                if self._quick_closing:
                    self._quick_open = False
                    self._quick_closing = False
                self.update_position()
                self.draw_ui()
                self._animation_job = None
                return

            progress = (i + 1) / step_count
            eased = 1 - (1 - progress) ** 3
            self.current_width = round(start_w + (target_w - start_w) * eased)
            self.current_height = round(start_h + (target_h - start_h) * eased)
            if self._usb_open or self._quick_open:
                x, y = self.panel_origin(self.current_width, self.current_height)
            else:
                x, y = self.main_origin(self.current_width, self.current_height)
            self.geometry(f"{self.current_width}x{self.current_height}+{x}+{y}")
            self.apply_rounded_window_region(self.current_width, self.current_height)
            self.glow.bounds = (x, y, self.current_width, self.current_height)
            self.draw_ui()
            self._animation_job = self.after(16, lambda: _step(i + 1))

        _step(0)

    def on_press(self, event):
        x = event.x if event else 0
        y = event.y if event else 0
        island_y = self.panel_island_y(self.current_height) if self._usb_open or self._quick_open else 0
        local_y = y - island_y
        if self._quick_open and y != island_y and not island_y <= y < island_y + self.min_height:
            panel_top = 0 if island_y else self.min_height
            index = round((y - (panel_top + 73)) / 31)
            if 0 <= index <= 4 and abs(y - (panel_top + 73 + index * 31)) <= 15:
                self.run_quick_action(index)
            return "break"
        if self._usb_open and not island_y <= y < island_y + self.min_height:
            panel_bottom = island_y if island_y else self.current_height
            if abs(y - (panel_bottom - 16)) <= 12 and x >= self.current_width - 100:
                self.refresh_usb_devices()
            return "break"
        if local_y <= 32 and x >= self.current_width - 34:
            self.close_app()
            return "break"
        if self._position_changed and local_y <= 32 and self.current_width - 63 <= x < self.current_width - 35:
            self.reset_position()
            return "break"
        if local_y <= 34 and 68 <= x < 94:
            self.open_settings()
            return "break"
        if local_y <= 34 and 94 <= x < 120:
            self.open_usb_menu()
            return "break"
        if local_y <= 34 and 120 <= x < 150:
            self.open_quick_menu()
            return "break"
        if self.expanded and 145 <= local_y <= 173:
            if 225 <= x < 270:
                self.send_media_key("previous")
            elif 270 <= x < 315:
                self.send_media_key("play")
            elif 315 <= x < 365:
                self.send_media_key("next")
            return "break"
        if self.audio is not None and self.expanded and event and 176 <= local_y <= 202 and 88 <= x <= self.current_width - 12:
            self._volume_dragging = True
            self.set_volume_from_x(x)
            return "break"
        self._press = (
            event.x_root, event.y_root, self.winfo_x(), self.winfo_y(),
        )
        self._dragging = False

    def on_release(self, event=None):
        if self._volume_dragging:
            self._volume_dragging = False
            return
        if self._press is None:
            return
        if self._dragging:
            self.glow.paused = False
            left, top, right, bottom = self.get_work_area()
            default = (
                left + (right - left - self.min_width) // 2,
                top,
            )
            self._position_changed = self._position != default
            if not self._position_changed:
                self._position = None
                self.update_position()
            self.config["position"] = self._position
            self.config["position_preset"] = "Custom" if self._position_changed else "Top Center"
            self.save_config()
            self.draw_ui()
        else:
            self.on_click()
        self._press = None
        self._dragging = False

    def on_click(self, event=None):
        if self._animation_job is not None:
            self.after_cancel(self._animation_job)
            self._animation_job = None
        self.glow.paused = False
        self._dismissed_alert_key = self._active_alert_key
        self._active_alert_key = None
        self.notification_active = False
        self.expanded = False
        self.current_width = self.min_width
        self.current_height = self.min_height
        self.update_position()
        self.draw_ui()
        self.glow.hide()
        self.withdraw()
        self.after(150, self.show_queued_notification)

    def on_drag(self, event):
        if self._volume_dragging:
            self.set_volume_from_x(event.x)
            return
        if self._press is None:
            return
        press_x, press_y, window_x, window_y = self._press
        offset_x, offset_y = event.x_root - press_x, event.y_root - press_y
        if not self._dragging and abs(offset_x) + abs(offset_y) >= 5:
            self._dragging = True
            self.glow.paused = True
        if self._dragging:
            left, top, right, bottom = self.get_work_area()
            x = max(left, min(window_x + offset_x, right - self.current_width))
            y = max(top, min(window_y + offset_y, bottom - self.current_height))
            position_x = x
            position_y = y
            if self._usb_open or self._quick_open:
                preset = self.config.get("position_preset")
                if preset == "Center" or preset.endswith("Center"):
                    position_x += (self.current_width - self.min_width) // 2
                elif x + self.current_width / 2 > (left + right) / 2:
                    position_x += self.current_width - self.min_width
                if self.panel_island_y(self.current_height):
                    position_y += self.current_height - self.min_height
            else:
                preset = self.config.get("position_preset")
                if self.current_width > self.min_width:
                    if preset == "Center" or preset.endswith("Center"):
                        position_x += (self.current_width - self.min_width) // 2
                    elif x + self.current_width / 2 > (left + right) / 2:
                        position_x += self.current_width - self.min_width
                if self.current_height > self.min_height:
                    if preset == "Center" or preset.startswith("Center"):
                        position_y += (self.current_height - self.min_height) // 2
                    elif y + self.current_height / 2 > (top + bottom) / 2:
                        position_y += self.current_height - self.min_height
            changed = (position_x, position_y) != self._position
            self.geometry(f"{self.current_width}x{self.current_height}+{x}+{y}")
            self._position = (position_x, position_y)
            self._position_changed = self._position != (
                left + (right - left - self.min_width) // 2,
                top + (bottom - top - self.min_height) // 2,
            )
            if changed:
                self.glow.move(x, y, self.current_width, self.current_height)
                self.canvas.delete("reset_button")
                self.draw_reset_button()

    def reset_position(self, _event=None):
        self._position = None
        self._position_changed = False
        self.config["position"] = None
        self.config["position_preset"] = "Top Center"
        self.save_config()
        self.update_position()
        self.draw_ui()
        return "break"

    def check_reopen_gesture(self):
        if self.state() == "withdrawn":
            point = wintypes.POINT()
            left, top, right, bottom = self.get_work_area()
            x, y = self.main_origin(self.min_width, self.min_height)
            is_held = bool(self._get_async_key_state(0x01) & 0x8000)
            is_over_island = (
                self._get_cursor_pos(ctypes.byref(point))
                and x <= point.x < x + self.min_width
                and y <= point.y < y + self.min_height
            )

            if is_held and is_over_island:
                if self._hold_started_at is None:
                    self._hold_started_at = time.monotonic()
                elif time.monotonic() - self._hold_started_at >= 0.5:
                    self._hold_started_at = None
                    self.expanded = True
                    self.deiconify()
                    self.lift()
                    self.glow.show()
                    self.update_position()
                    self.animate_to(self.max_width, self.max_height)
            else:
                self._hold_started_at = None
        else:
            self._hold_started_at = None

        self.after(50, self.check_reopen_gesture)

    def update_loop(self):
        self.refresh_metrics()
        self.after(1000, self.update_loop)

    def refresh_metrics(self):
        self._metrics = self.read_metrics()
        if self.audio is not None:
            try:
                self._volume = self.audio.get()
            except OSError as exc:
                self.disable_audio(exc)
        cpu = self._metrics["cpu"]
        memory = self._metrics["memory"]
        battery = self._metrics["battery"]
        alert_msg = ""
        if battery and battery.percent <= self.config["battery_alert"]:
            alert_msg = f"Battery low: {battery.percent}%"
        elif cpu >= self.config["cpu_alert"]:
            alert_msg = f"High CPU: {cpu:.0f}%"
        elif memory >= self.config["memory_alert"]:
            alert_msg = f"Memory high: {memory:.0f}%"
        elif self._metrics["disk_free"] <= self.config["disk_alert"]:
            alert_msg = f"Low disk space: {self._metrics['disk_free']:.0f}% free"
        elif self._metrics["network"] == "Offline":
            alert_msg = "Network disconnected"

        if (
            self.notification_active
            and str(self._active_alert_key).startswith("app-notification:")
        ):
            if time.monotonic() - self._last_notification > 6:
                self.dismiss_notification(self._active_alert_key)
        elif alert_msg:
            alert_key = alert_msg.split(":", 1)[0]
            if self.notification_active:
                if alert_key != self._active_alert_key:
                    self._active_alert_key = alert_key
                    self._last_notification = time.monotonic()
                self._pending_notification = alert_msg
                if time.monotonic() - self._last_notification > 6:
                    self.dismiss_notification(alert_key)
            elif alert_key != self._dismissed_alert_key:
                self._pending_notification = alert_msg
                self.notification_active = True
                self._active_alert_key = alert_key
                self._last_notification = time.monotonic()
                self.expanded = True
                self._usb_open = self._quick_open = False
                self._usb_closing = self._quick_closing = False
                self.animate_to(self.max_width, self.max_height)
        else:
            self._dismissed_alert_key = None
            if self.notification_active and time.monotonic() - self._last_notification > 6:
                self.dismiss_notification(self._active_alert_key)

        if self.state() != "withdrawn":
            self.draw_ui()

    def read_metrics(self):
        try:
            battery = psutil.sensors_battery()
        except AttributeError:
            battery = None
        try:
            counters = psutil.net_io_counters()
            now = time.monotonic()
            previous = self._network_previous
            self._network_previous = (counters.bytes_sent, counters.bytes_recv, now)
            if_stats = psutil.net_if_stats()
            has_network = any(
                stats.isup and "loopback" not in name.lower()
                for name, stats in if_stats.items()
            )
            if previous is None:
                network = "Online" if has_network else "Offline"
            else:
                elapsed = max(now - previous[2], 0.001)
                sent_rate = max(0, counters.bytes_sent - previous[0]) / elapsed / 1024
                recv_rate = max(0, counters.bytes_recv - previous[1]) / elapsed / 1024
                network = (
                    f"Network ↑{sent_rate:.0f} ↓{recv_rate:.0f} KB/s"
                    if has_network else "Offline"
                )
        except (OSError, AttributeError):
            network, has_network = "Network unavailable", None
        disk = psutil.disk_usage(Path.home().anchor or "/")
        return {
            "cpu": psutil.cpu_percent(interval=None),
            "memory": psutil.virtual_memory().percent,
            "battery": battery,
            "disk": disk.percent,
            "disk_free": 100 - disk.percent,
            "network": network,
            "network_online": has_network,
        }

    def dismiss_notification(self, alert_key):
        self._dismissed_alert_key = alert_key
        self.notification_active = False
        self._active_alert_key = None
        self.expanded = False
        self.current_width = self.min_width
        self.current_height = self.min_height
        self.update_position()
        self.draw_ui()
        self.glow.hide()
        self.withdraw()
        self.after(150, self.show_queued_notification)


def relaunch_without_console():
    if sys.platform != "win32" or Path(sys.executable).name.lower() == "pythonw.exe":
        return False

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        return False

    script = Path(__file__).resolve()
    subprocess.Popen(
        [str(pythonw), str(script), *sys.argv[1:]],
        cwd=str(script.parent),
        close_fds=True,
    )
    return True


if __name__ == "__main__":
    if relaunch_without_console():
        raise SystemExit(0)
    app = DynamicIslandApp()
    app.mainloop()
