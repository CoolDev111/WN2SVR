from __future__ import annotations

import asyncio, ctypes, io, json, os, pathlib, sys, threading, time, tkinter as tk, winreg
from dataclasses import dataclass, field
from tkinter import scrolledtext, ttk
from typing import Any, Optional

import pystray
from PIL import Image, ImageDraw, ImageFont

try:
    import openvr
except ImportError:
    print("openvr not installed: pip install openvr")
    sys.exit(1)

try:
    from winsdk.windows.ui.notifications.management import UserNotificationListener
    from winsdk.windows.ui.notifications import NotificationKinds
    from winsdk.windows.storage.streams import DataReader, Buffer
    WINSDK_OK = True
except ImportError:
    print("winsdk not installed: pip install winrt-Windows.UI.Notifications")
    sys.exit(1)

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
MANIFEST_PATH = os.path.join(BASE_DIR, "vr_notifier.vrmanifest")

DEFAULTS = {
    "pos_x": 0.3, "pos_y": 0.39999999999999997, "pos_z": -1.0000000000000002,
    "dnd_enabled": False, "overlay_width_m": 0.55, "overlay_alpha": 1.0,
    "display_seconds": 3.0, "fade_duration": 0.2, "history_max": 50,
}
POS_X_RANGE = (-1.0, 1.0)
POS_Y_RANGE = (-1.0, 1.0)
POS_Z_RANGE = (-2.0, -0.1)
TEXTURE_W, TEXTURE_H = 600, 200
OVERLAY_KEY = "python.windows.toast.notifier"
OVERLAY_NAME = "Windows VR Notifier"

@dataclass
class Notification:
    app_name: str
    title: str
    body: str
    timestamp: float = field(default_factory=time.time)
    icon_bytes: Optional[bytes] = None

class ConfigStore:
    def __init__(self, path: str = CONFIG_FILE) -> None:
        self.path = path
        self.pos_x = DEFAULTS["pos_x"]
        self.pos_y = DEFAULTS["pos_y"]
        self.pos_z = DEFAULTS["pos_z"]
        self.dnd_enabled = DEFAULTS["dnd_enabled"]
        self.overlay_width_m = DEFAULTS["overlay_width_m"]
        self.overlay_alpha = DEFAULTS["overlay_alpha"]
        self.display_seconds = DEFAULTS["display_seconds"]
        self.fade_duration = DEFAULTS["fade_duration"]
        self.history_max = DEFAULTS["history_max"]
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        self.pos_x = self._clamp(data.get("pos_x", self.pos_x), POS_X_RANGE)
        self.pos_y = self._clamp(data.get("pos_y", self.pos_y), POS_Y_RANGE)
        self.pos_z = self._clamp(data.get("pos_z", self.pos_z), POS_Z_RANGE)
        self.dnd_enabled = bool(data.get("dnd_enabled", self.dnd_enabled))
        self.overlay_width_m = self._clamp(float(data.get("overlay_width_m", self.overlay_width_m)), (0.05, 2.0))
        self.overlay_alpha = self._clamp(float(data.get("overlay_alpha", self.overlay_alpha)), (0.0, 1.0))
        self.display_seconds = max(1.0, float(data.get("display_seconds", self.display_seconds)))
        self.fade_duration = max(0.0, float(data.get("fade_duration", self.fade_duration)))
        self.history_max = max(1, int(data.get("history_max", self.history_max)))

    def save(self) -> None:
        data = {
            "pos_x": self.pos_x, "pos_y": self.pos_y, "pos_z": self.pos_z,
            "dnd_enabled": self.dnd_enabled, "overlay_width_m": self.overlay_width_m,
            "overlay_alpha": self.overlay_alpha, "display_seconds": self.display_seconds,
            "fade_duration": self.fade_duration, "history_max": self.history_max,
        }
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    @staticmethod
    def _clamp(value, bounds):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return bounds[0]
        lo, hi = bounds
        return max(lo, min(hi, v))

class NotificationHistory:
    def __init__(self, max_items: int = 50) -> None:
        self._lock = threading.Lock()
        self._items: list[Notification] = []
        self._max = max_items

    def add(self, n: Notification) -> None:
        with self._lock:
            self._items.insert(0, n)
            if len(self._items) > self._max:
                self._items = self._items[: self._max]

    def snapshot(self) -> list[Notification]:
        with self._lock:
            return list(self._items)

class OverlayRenderer:
    def __init__(self, overlay, overlay_handle, config: ConfigStore) -> None:
        self.overlay = overlay
        self.overlay_handle = overlay_handle
        self.config = config
        self._icon_cache: dict = {}
        self.overlay.setOverlayWidthInMeters(self.overlay_handle, config.overlay_width_m)
        self.overlay.setOverlayAlpha(self.overlay_handle, config.overlay_alpha)
        self.apply_transform()

    def apply_transform(self) -> None:
        transform = openvr.HmdMatrix34_t()
        transform.m[0][0] = 1.0
        transform.m[1][1] = 1.0
        transform.m[2][2] = 1.0
        transform.m[0][3] = self.config.pos_x
        transform.m[1][3] = self.config.pos_y
        transform.m[2][3] = self.config.pos_z
        try:
            self.overlay.setOverlayTransformTrackedDeviceRelative(
                self.overlay_handle, openvr.k_unTrackedDeviceIndex_Hmd, transform)
        except Exception:
            pass

    @staticmethod
    def _load_font(size: int):
        for name in ("seguisym.ttf", "segoeui.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except IOError:
                continue
        return ImageFont.load_default()

    def render(self, n: Notification, queue_depth: int = 0) -> None:
        img = Image.new("RGBA", (TEXTURE_W, TEXTURE_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [10, 10, TEXTURE_W - 10, TEXTURE_H - 10], radius=15,
            fill=(30, 30, 40, 230), outline=(80, 80, 100, 255), width=2)
        font_title = self._load_font(22)
        font_body = self._load_font(18)

        icon = self._get_icon(n)
        icon_size = 40
        icon_x, icon_y = 20, 18
        text_x = 70
        if icon is not None:
            try:
                img.paste(icon, (icon_x, icon_y), icon)
            except Exception:
                text_x = 30
        else:
            text_x = 30

        draw.text((text_x, 25), str(n.app_name).upper()[:32], font=font_title, fill=(0, 162, 255, 255))
        draw.text((30, 65), str(n.title)[:60], font=font_title, fill=(255, 255, 255, 255))
        body_lines = self._wrap_text(draw, str(n.body), font_body, max_width=TEXTURE_W - 60, max_lines=3)
        y = 105
        for line in body_lines:
            draw.text((30, y), line, font=font_body, fill=(210, 210, 210, 255))
            y += 24
        if queue_depth > 0:
            draw.text((TEXTURE_W - 110, TEXTURE_H - 32), f"+{queue_depth} more",
                      font=font_body, fill=(255, 180, 80, 255))
        self._upload(img)

    def _get_icon(self, n: Notification) -> Optional[Image.Image]:
        if n.app_name in self._icon_cache:
            return self._icon_cache[n.app_name]
        icon = None
        if n.icon_bytes:
            try:
                raw = Image.open(io.BytesIO(n.icon_bytes))
                raw.load()
                icon = raw.convert("RGBA").resize((40, 40), Image.LANCZOS)
            except Exception:
                icon = None
        self._icon_cache[n.app_name] = icon
        return icon

    def _upload(self, img: Image.Image) -> None:
        try:
            data = img.tobytes()
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            self.overlay.setOverlayRaw(
                self.overlay_handle, buf, img.width, img.height, 4)
        except Exception:
            raise

    def show(self) -> None:
        try:
            self.overlay.showOverlay(self.overlay_handle)
        except Exception:
            pass

    def hide(self) -> None:
        try:
            self.overlay.hideOverlay(self.overlay_handle)
        except Exception:
            pass

    def _set_alpha(self, alpha: float) -> None:
        try:
            self.overlay.setOverlayAlpha(self.overlay_handle, alpha)
        except Exception:
            pass

    async def fade_in(self) -> None:
        target = self.config.overlay_alpha
        dur = self.config.fade_duration
        self._set_alpha(0.0)
        self.show()
        if dur <= 0:
            self._set_alpha(target)
            return
        steps = 10
        step_dur = dur / steps
        for i in range(1, steps + 1):
            self._set_alpha(target * (i / steps))
            await asyncio.sleep(step_dur)
        self._set_alpha(target)

    async def fade_out(self) -> None:
        start = self.config.overlay_alpha
        dur = self.config.fade_duration
        if dur <= 0:
            self._set_alpha(0.0)
            self.hide()
            return
        steps = 10
        step_dur = dur / steps
        for i in range(1, steps + 1):
            self._set_alpha(start * (1 - i / steps))
            await asyncio.sleep(step_dur)
        self._set_alpha(0.0)
        self.hide()

    def show_preview(self) -> None:
        try:
            self.render(Notification(
                app_name="Preview", title="Adjust your sliders!",
                body="This card stays visible while you tweak position, alpha, and width."),
                queue_depth=0)
            self._set_alpha(self.config.overlay_alpha)
            self.show()
        except Exception:
            pass

    def hide_preview(self) -> None:
        try:
            self.hide()
        except Exception:
            pass

    def update_live_alpha(self) -> None:
        try:
            self._set_alpha(self.config.overlay_alpha)
        except Exception:
            pass

    @staticmethod
    def _wrap_text(draw, text: str, font, max_width: int, max_lines: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            try:
                width = draw.textlength(test, font=font)
            except Exception:
                width = len(test) * 8
            if width <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
                if len(lines) >= max_lines:
                    break
        if current and len(lines) < max_lines:
            lines.append(current)
        if lines and len(lines) == max_lines:
            lines[-1] = lines[-1][:60] + ("..." if len(lines[-1]) >= 60 else "")
        return lines

class DashboardPanel:
    PANEL_W, PANEL_H = 900, 550
    THUMB_W, THUMB_H = 64, 64
    TAB_BAR_Y = 95
    TAB_HEIGHT = 40
    TAB_WIDTH = 200
    TABS = ["Home", "Position", "Appearance", "History"]
    BTN_DND = (50, 200, 430, 280)
    BTN_TEST = (470, 200, 850, 280)
    BTN_PREVIEW = (50, 310, 850, 380)
    POS_ROWS = [
        ("X (Left/Right)", 240, (300, 240, 360, 290), (500, 240, 560, 290), 400),
        ("Y (Up/Down)",   310, (300, 310, 360, 360), (500, 310, 560, 360), 400),
        ("Z (Distance)",  380, (300, 380, 360, 430), (500, 380, 560, 430), 400),
    ]
    POS_STEP = 0.05
    APP_ROWS = [
        ("Width (m)",    220, (300, 220, 360, 270), (500, 220, 560, 270), 400, 0.05),
        ("Alpha",        280, (300, 280, 360, 330), (500, 280, 560, 330), 400, 0.05),
        ("Display (s)",  340, (300, 340, 360, 390), (500, 340, 560, 390), 400, 0.5),
        ("Fade (s)",     400, (300, 400, 360, 450), (500, 400, 560, 450), 400, 0.05),
    ]

    def __init__(self, overlay, config, app: "VRNotificationApp") -> None:
        self.overlay = overlay
        self.config = config
        self.app = app
        self.main_handle, self.thumb_handle = overlay.createDashboardOverlay(
            OVERLAY_KEY + "_dashboard", "VR Notifier Settings")
        scale = openvr.HmdVector2_t()
        scale.v[0] = self.PANEL_W
        scale.v[1] = self.PANEL_H
        overlay.setOverlayMouseScale(self.main_handle, scale)
        overlay.setOverlayWidthInMeters(self.main_handle, 1.5)
        self._active_tab = 0
        self._y_flip = True
        self._render_lock = threading.Lock()
        self._last_render_time = 0.0
        self._render_pending = False
        self.render_thumbnail()
        self.render_panel()

    def render_panel(self) -> None:
        now = time.time()
        if now - self._last_render_time < 0.2:
            if not self._render_pending:
                self._render_pending = True
                threading.Timer(0.2, self._render_panel_debounced).start()
            return
        self._render_panel_now()

    def _render_panel_debounced(self) -> None:
        self._render_pending = False
        self._render_panel_now()

    def _render_panel_now(self) -> None:
        if not self._render_lock.acquire(blocking=False):
            if not self._render_pending:
                self._render_pending = True
                threading.Timer(0.15, self._render_panel_debounced).start()
            return
        try:
            self._last_render_time = time.time()
            img = self._draw_panel()
            try:
                data = img.tobytes()
                buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
                self.overlay.setOverlayRaw(
                    self.main_handle, buf, self.PANEL_W, self.PANEL_H, 4)
            except Exception:
                pass
        finally:
            self._render_lock.release()

    def _draw_panel(self) -> Image.Image:
        img = Image.new("RGBA", (self.PANEL_W, self.PANEL_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [5, 5, self.PANEL_W - 5, self.PANEL_H - 5], radius=20,
            fill=(30, 30, 40, 245), outline=(80, 80, 100, 255), width=3)
        font_title = self._load_font(32)
        font_sub = self._load_font(20)
        font_btn = self._load_font(22)
        font_body = self._load_font(18)
        font_tab = self._load_font(20)
        draw.text((50, 30), "Windows VR Notifier", font=font_title, fill=(0, 162, 255, 255))
        draw.text((50, 70), "Settings & Controls", font=font_sub, fill=(150, 150, 160, 255))
        self._draw_tab_bar(draw, font_tab)
        if self._active_tab == 0:
            self._draw_home_tab(draw, font_btn, font_sub, font_body)
        elif self._active_tab == 1:
            self._draw_position_tab(draw, font_btn, font_sub, font_body)
        elif self._active_tab == 2:
            self._draw_appearance_tab(draw, font_btn, font_sub, font_body)
        elif self._active_tab == 3:
            self._draw_history_tab(draw, font_sub, font_body)
        return img

    def _draw_tab_bar(self, draw, font) -> None:
        y = self.TAB_BAR_Y
        for i, name in enumerate(self.TABS):
            x1 = 50 + i * self.TAB_WIDTH
            x2 = x1 + self.TAB_WIDTH - 10
            y2 = y + self.TAB_HEIGHT
            if i == self._active_tab:
                draw.rounded_rectangle([x1, y, x2, y2], radius=8, fill=(0, 100, 180, 255))
                text_color = (255, 255, 255, 255)
            else:
                draw.rounded_rectangle([x1, y, x2, y2], radius=8,
                    fill=(50, 50, 65, 255), outline=(80, 80, 100, 255))
                text_color = (180, 180, 190, 255)
            text_w = draw.textlength(name, font=font)
            text_x = x1 + (self.TAB_WIDTH - 10 - text_w) / 2
            draw.text((text_x, y + 8), name, font=font, fill=text_color)
        draw.line([(50, y + self.TAB_HEIGHT + 5), (self.PANEL_W - 50, y + self.TAB_HEIGHT + 5)],
            fill=(60, 60, 80, 255), width=2)

    def _draw_home_tab(self, draw, font_btn, font_sub, font_body) -> None:
        if self.app.dnd_enabled:
            dnd_bg = (200, 100, 50, 255)
            dnd_label = "DND: ON"
            dnd_sub = "Click to disable"
        else:
            dnd_bg = (50, 150, 80, 255)
            dnd_label = "DND: OFF"
            dnd_sub = "Click to enable"
        draw.rounded_rectangle(self.BTN_DND, radius=12, fill=dnd_bg)
        tw = draw.textlength(dnd_label, font=font_btn)
        draw.text((self.BTN_DND[0] + (self.BTN_DND[2] - self.BTN_DND[0] - tw) / 2, self.BTN_DND[1] + 15),
            dnd_label, font=font_btn, fill=(255, 255, 255, 255))
        tw2 = draw.textlength(dnd_sub, font=font_body)
        draw.text((self.BTN_DND[0] + (self.BTN_DND[2] - self.BTN_DND[0] - tw2) / 2, self.BTN_DND[1] + 50),
            dnd_sub, font=font_body, fill=(220, 220, 220, 255))
        draw.rounded_rectangle(self.BTN_TEST, radius=12, fill=(0, 100, 180, 255))
        test_label = "Send Test"
        test_sub = "Notification"
        tw = draw.textlength(test_label, font=font_btn)
        draw.text((self.BTN_TEST[0] + (self.BTN_TEST[2] - self.BTN_TEST[0] - tw) / 2, self.BTN_TEST[1] + 15),
            test_label, font=font_btn, fill=(255, 255, 255, 255))
        tw2 = draw.textlength(test_sub, font=font_body)
        draw.text((self.BTN_TEST[0] + (self.BTN_TEST[2] - self.BTN_TEST[0] - tw2) / 2, self.BTN_TEST[1] + 50),
            test_sub, font=font_body, fill=(220, 220, 220, 255))
        if self.app.preview_active:
            preview_bg = (100, 60, 160, 255)
            preview_label = "Stop Preview"
            preview_sub = "Preview is ON — click to hide"
        else:
            preview_bg = (60, 100, 60, 255)
            preview_label = "Preview Overlay"
            preview_sub = "Show a static card for adjusting settings"
        draw.rounded_rectangle(self.BTN_PREVIEW, radius=12, fill=preview_bg)
        tw = draw.textlength(preview_label, font=font_btn)
        draw.text((self.BTN_PREVIEW[0] + (self.BTN_PREVIEW[2] - self.BTN_PREVIEW[0] - tw) / 2,
            self.BTN_PREVIEW[1] + 12), preview_label, font=font_btn, fill=(255, 255, 255, 255))
        tw2 = draw.textlength(preview_sub, font=font_body)
        draw.text((self.BTN_PREVIEW[0] + (self.BTN_PREVIEW[2] - self.BTN_PREVIEW[0] - tw2) / 2,
            self.BTN_PREVIEW[1] + 45), preview_sub, font=font_body, fill=(220, 220, 220, 255))
        status = "DND active" if self.app.dnd_enabled else "Listening for notifications"
        color = (255, 180, 80, 255) if self.app.dnd_enabled else (100, 220, 100, 255)
        draw.text((50, 405), "Status:", font=font_sub, fill=(180, 180, 190, 255))
        draw.text((160, 405), status, font=font_sub, fill=color)
        history = self.app.history.snapshot()
        draw.text((50, 440), f"Recent notifications: {len(history)}", font=font_body, fill=(200, 200, 200, 255))
        draw.text((50, 490), "Use the other tabs to adjust position and appearance.",
            font=font_body, fill=(150, 150, 160, 255))

    def _draw_position_tab(self, draw, font_btn, font_sub, font_body) -> None:
        draw.text((50, 170), "Overlay Position", font=font_sub, fill=(180, 180, 190, 255))
        draw.text((50, 200), "Use - and + to adjust each axis", font=font_body, fill=(150, 150, 160, 255))
        cfg = self.config
        values = [cfg.pos_x, cfg.pos_y, cfg.pos_z]
        for i, (label, y, minus, plus, val_x) in enumerate(self.POS_ROWS):
            draw.text((50, y + 10), label, font=font_body, fill=(200, 200, 200, 255))
            draw.rounded_rectangle(minus, radius=8, fill=(180, 60, 60, 255))
            draw.text((minus[0] + 18, minus[1] + 10), "-", font=font_btn, fill=(255, 255, 255, 255))
            val_text = f"{values[i]:.2f}"
            tw = draw.textlength(val_text, font=font_btn)
            draw.text((val_x - tw / 2, y + 10), val_text, font=font_btn, fill=(255, 255, 255, 255))
            draw.rounded_rectangle(plus, radius=8, fill=(60, 150, 80, 255))
            draw.text((plus[0] + 18, plus[1] + 10), "+", font=font_btn, fill=(255, 255, 255, 255))
        draw.text((50, 470), "Tip: Use the Preview Overlay to see", font=font_body, fill=(150, 150, 160, 255))
        draw.text((50, 495), "changes in real-time while adjusting here.", font=font_body, fill=(150, 150, 160, 255))

    def _draw_appearance_tab(self, draw, font_btn, font_sub, font_body) -> None:
        draw.text((50, 150), "Overlay Appearance", font=font_sub, fill=(180, 180, 190, 255))
        cfg = self.config
        values = [cfg.overlay_width_m, cfg.overlay_alpha, cfg.display_seconds, cfg.fade_duration]
        for i, (label, y, minus, plus, val_x, step) in enumerate(self.APP_ROWS):
            draw.text((50, y + 10), label, font=font_body, fill=(200, 200, 200, 255))
            draw.rounded_rectangle(minus, radius=8, fill=(180, 60, 60, 255))
            draw.text((minus[0] + 18, minus[1] + 10), "-", font=font_btn, fill=(255, 255, 255, 255))
            val_text = f"{values[i]:.2f}" if step < 1 else f"{values[i]:.1f}"
            tw = draw.textlength(val_text, font=font_btn)
            draw.text((val_x - tw / 2, y + 10), val_text, font=font_btn, fill=(255, 255, 255, 255))
            draw.rounded_rectangle(plus, radius=8, fill=(60, 150, 80, 255))
            draw.text((plus[0] + 18, plus[1] + 10), "+", font=font_btn, fill=(255, 255, 255, 255))
        draw.text((50, 475), "Alpha: opacity (0.1=faint, 1.0=solid)", font=font_body, fill=(150, 150, 160, 255))
        draw.text((50, 500), "Display: seconds each notification stays  •  Fade: animation duration",
            font=font_body, fill=(150, 150, 160, 255))

    def _draw_history_tab(self, draw, font_sub, font_body) -> None:
        draw.text((50, 150), "Recent Notifications", font=font_sub, fill=(180, 180, 190, 255))
        items = self.app.history.snapshot()[:8]
        if not items:
            draw.text((50, 200), "No notifications yet.", font=font_body, fill=(150, 150, 160, 255))
            return
        y = 190
        for n in items:
            ts = time.strftime("%H:%M:%S", time.localtime(n.timestamp))
            app = n.app_name[:20]
            title = n.title[:40]
            draw.text((50, y), f"[{ts}] {app}", font=font_body, fill=(0, 162, 255, 255))
            draw.text((50, y + 22), f"  {title}", font=font_body, fill=(220, 220, 220, 255))
            y += 50
            if y > 500:
                break

    def render_thumbnail(self) -> None:
        img = Image.new("RGBA", (self.THUMB_W, self.THUMB_H), (0, 0, 0, 0))
        dc = ImageDraw.Draw(img)
        dc.rounded_rectangle((4, 4, 60, 60), radius=8, fill=(30, 30, 40, 255))
        dc.rounded_rectangle((10, 22, 54, 42), radius=4, fill=(0, 162, 255, 255))
        dc.rounded_rectangle((18, 27, 28, 37), radius=2, fill=(20, 20, 30, 255))
        dc.rounded_rectangle((36, 27, 46, 37), radius=2, fill=(20, 20, 30, 255))
        dc.ellipse((42, 10, 56, 24), fill=(255, 80, 80, 255))
        try:
            data = img.tobytes()
            buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            self.overlay.setOverlayRaw(
                self.thumb_handle, buf, self.THUMB_W, self.THUMB_H, 4)
        except Exception:
            pass

    def handle_click(self, x: float, y: float) -> bool:
        if self._y_flip:
            y = self.PANEL_H - y
        if self.TAB_BAR_Y <= y <= self.TAB_BAR_Y + self.TAB_HEIGHT:
            for i in range(len(self.TABS)):
                x1 = 50 + i * self.TAB_WIDTH
                x2 = x1 + self.TAB_WIDTH - 10
                if x1 <= x <= x2:
                    if self._active_tab != i:
                        self._active_tab = i
                        self.render_panel()
                    return True
        if self._active_tab == 0:
            return self._handle_home_click(x, y)
        elif self._active_tab == 1:
            return self._handle_position_click(x, y)
        elif self._active_tab == 2:
            return self._handle_appearance_click(x, y)
        return False

    def _handle_home_click(self, x: float, y: float) -> bool:
        if self._in_rect(x, y, self.BTN_DND):
            self.app.set_dnd(not self.app.dnd_enabled)
            return True
        if self._in_rect(x, y, self.BTN_TEST):
            self.app.send_test_notification()
            return True
        if self._in_rect(x, y, self.BTN_PREVIEW):
            self.app.toggle_preview()
            return True
        return False

    def _handle_position_click(self, x: float, y: float) -> bool:
        cfg = self.config
        for i, (label, y_pos, minus, plus, val_x) in enumerate(self.POS_ROWS):
            if self._in_rect(x, y, minus):
                if i == 0:
                    cfg.pos_x = max(POS_X_RANGE[0], cfg.pos_x - self.POS_STEP)
                elif i == 1:
                    cfg.pos_y = max(POS_Y_RANGE[0], cfg.pos_y - self.POS_STEP)
                elif i == 2:
                    cfg.pos_z = max(POS_Z_RANGE[0], cfg.pos_z - self.POS_STEP)
                self._apply_position_change()
                return True
            if self._in_rect(x, y, plus):
                if i == 0:
                    cfg.pos_x = min(POS_X_RANGE[1], cfg.pos_x + self.POS_STEP)
                elif i == 1:
                    cfg.pos_y = min(POS_Y_RANGE[1], cfg.pos_y + self.POS_STEP)
                elif i == 2:
                    cfg.pos_z = min(POS_Z_RANGE[1], cfg.pos_z + self.POS_STEP)
                self._apply_position_change()
                return True
        return False

    def _handle_appearance_click(self, x: float, y: float) -> bool:
        cfg = self.config
        for i, (label, y_pos, minus, plus, val_x, step) in enumerate(self.APP_ROWS):
            if self._in_rect(x, y, minus):
                if i == 0:
                    cfg.overlay_width_m = max(0.1, cfg.overlay_width_m - step)
                elif i == 1:
                    cfg.overlay_alpha = max(0.1, cfg.overlay_alpha - step)
                elif i == 2:
                    cfg.display_seconds = max(1.0, cfg.display_seconds - step)
                elif i == 3:
                    cfg.fade_duration = max(0.0, cfg.fade_duration - step)
                self._apply_appearance_change()
                return True
            if self._in_rect(x, y, plus):
                if i == 0:
                    cfg.overlay_width_m = min(1.5, cfg.overlay_width_m + step)
                elif i == 1:
                    cfg.overlay_alpha = min(1.0, cfg.overlay_alpha + step)
                elif i == 2:
                    cfg.display_seconds = min(15.0, cfg.display_seconds + step)
                elif i == 3:
                    cfg.fade_duration = min(1.0, cfg.fade_duration + step)
                self._apply_appearance_change()
                return True
        return False

    def _apply_position_change(self) -> None:
        try:
            if self.app.renderer is not None:
                self.app.renderer.apply_transform()
        except Exception:
            pass
        self.config.save()
        self.render_panel()

    def _apply_appearance_change(self) -> None:
        try:
            if self.app.renderer is not None:
                self.app.renderer.apply_transform()
                self.app.renderer.overlay.setOverlayWidthInMeters(
                    self.app.renderer.overlay_handle, self.config.overlay_width_m)
                self.app.renderer.update_live_alpha()
        except Exception:
            pass
        self.config.save()
        self.render_panel()

    @staticmethod
    def _in_rect(x: float, y: float, rect) -> bool:
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    @staticmethod
    def _load_font(size: int):
        for name in ("seguisym.ttf", "segoeui.ttf", "arial.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except IOError:
                continue
        return ImageFont.load_default()

class NotificationListener:
    POLL_INTERVAL = 1.0

    @staticmethod
    def _safe_iter(collection):
        if collection is None:
            return
        try:
            count = len(collection)
        except Exception:
            return
        for i in range(count):
            try:
                yield collection[i]
            except Exception:
                continue

    def __init__(self, app: "VRNotificationApp") -> None:
        self.app = app
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._dashboard_poll_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None

    async def run(self) -> None:
        self.queue = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        listener = UserNotificationListener.current
        try:
            access_status = await asyncio.wait_for(listener.request_access_async(), timeout=10.0)
        except asyncio.TimeoutError:
            return
        if access_status != 1:
            return
        self._worker_task = asyncio.create_task(self._worker())
        self._dashboard_poll_task = asyncio.create_task(self._poll_dashboard_events())
        self._watchdog_task = asyncio.create_task(self._steamvr_watchdog())
        known_ids: set = set()
        try:
            initial_notes = await asyncio.wait_for(
                listener.get_notifications_async(NotificationKinds.TOAST), timeout=5.0)
            for note in self._safe_iter(initial_notes):
                try:
                    known_ids.add(note.id)
                except Exception:
                    pass
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        try:
            while self.app.running.is_set():
                await asyncio.sleep(self.POLL_INTERVAL)
                try:
                    current_notes = await asyncio.wait_for(
                        listener.get_notifications_async(NotificationKinds.TOAST), timeout=3.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    await asyncio.sleep(2.0)
                    continue
                current_ids: set = set()
                new_notes: list = []
                for note in self._safe_iter(current_notes):
                    try:
                        nid = note.id
                        current_ids.add(nid)
                        if nid not in known_ids:
                            known_ids.add(nid)
                            new_notes.append(note)
                    except Exception:
                        continue
                for note in new_notes:
                    await self._dispatch(note)
                known_ids &= current_ids
        finally:
            if self._watchdog_task:
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if self._dashboard_poll_task:
                self._dashboard_poll_task.cancel()
                try:
                    await self._dashboard_poll_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if self._worker_task:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

    async def _poll_dashboard_events(self) -> None:
        while self.app.running.is_set():
            dashboard = getattr(self.app, "dashboard", None)
            if dashboard is not None and self.app.overlay is not None:
                try:
                    event = openvr.VREvent_t()
                    has_event, event = self.app.overlay.pollNextOverlayEvent(
                        dashboard.main_handle, event)
                    while has_event:
                        if event.eventType == openvr.VREvent_MouseButtonDown:
                            try:
                                x = event.data.mouse.x
                                y = event.data.mouse.y
                                dashboard.handle_click(x, y)
                            except Exception:
                                pass
                        elif event.eventType == openvr.VREvent_Quit:
                            self.app._steamvr_shutdown()
                            break
                        has_event, event = self.app.overlay.pollNextOverlayEvent(
                            dashboard.main_handle, event)
                except Exception:
                    pass
            await asyncio.sleep(0.05)

    async def _steamvr_watchdog(self) -> None:
        while self.app.running.is_set():
            await asyncio.sleep(5.0)
            try:
                vr_system = openvr.VRSystem()
                if vr_system is None:
                    self.app._steamvr_shutdown()
                    return
            except Exception:
                self.app._steamvr_shutdown()
                return

    async def _dispatch(self, note) -> None:
        app_name = "System App"
        title = "New Notification"
        body = ""
        icon_bytes = None
        try:
            display_info = note.app_info.display_info
            app_name = (getattr(display_info, "display_name", None)
                or getattr(display_info, "displayName", None) or "System App")
            app_name = str(app_name)
            icon_bytes = await self._extract_icon(display_info)
            if icon_bytes is None:
                aumid = getattr(note.app_info, "app_user_model_id", "")
                icon_bytes = await self._extract_icon_from_registry(aumid)
        except Exception:
            app_name = "System App"
        try:
            bindings = note.notification.visual.bindings
            binding = None
            try:
                binding = bindings[0] if len(bindings) > 0 else None
            except Exception:
                pass
            if binding is not None:
                text_elements = binding.get_text_elements()
                try:
                    count = len(text_elements)
                    if count > 0:
                        title = str(text_elements[0].text)
                    if count > 1:
                        body = str(text_elements[1].text)
                except Exception:
                    title = "New Notification"
                    body = ""
        except Exception:
            title = "System Alert"
            body = "Check your desktop for details."
        try:
            await self.queue.put((app_name, title, body, icon_bytes))
        except Exception:
            pass

    @staticmethod
    async def _extract_icon(display_info) -> Optional[bytes]:
        for sz in (256, 48, 32, 16):
            try:
                logo_ref = display_info.get_logo(Size(sz, sz)) # type: ignore
                if logo_ref is not None:
                    stream = await asyncio.wait_for(logo_ref.open_read_async(), timeout=2.0)
                    try:
                        size = stream.size
                        if size <= 0 or size > 1024 * 1024:
                            continue
                        buf = Buffer(int(size))
                        await asyncio.wait_for(stream.read_async(buf, int(size), 0), timeout=2.0)
                        reader = DataReader.from_buffer(buf)
                        data = reader.read_bytes(int(size))
                        result = bytes(data)
                        return result
                    finally:
                        try:
                            stream.close()
                        except Exception:
                            pass
            except Exception:
                continue
        return None

    @staticmethod
    async def _extract_icon_from_registry(aumid: str) -> Optional[bytes]:
        if not aumid:
            return None
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                f"Software\\Classes\\AppUserModelId\\{aumid}")
            try:
                icon_uri, _ = winreg.QueryValueEx(key, "IconUri")
                if icon_uri.startswith("file:///"):
                    icon_path = icon_uri[8:].replace("/", "\\")
                elif icon_uri.startswith("ms-appx"):
                    return None
                else:
                    icon_path = icon_uri
                with open(icon_path, "rb") as f:
                    data = f.read()
                return data
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            return None
        except Exception as e:
            return None

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while self.app.running.is_set():
            try:
                app_name, title, body, icon_bytes = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self.app.history.add(Notification(app_name=app_name, title=title, body=body))
            if self.app.dashboard is not None:
                try:
                    self.app.dashboard.render_panel()
                except Exception:
                    pass
            if self.app.dnd_enabled:
                self.queue.task_done()
                continue
            queue_depth = self.queue.qsize()
            shown = False
            try:
                n = Notification(app_name=app_name, title=title, body=body, icon_bytes=icon_bytes)
                render_ok = True
                try:
                    await loop.run_in_executor(None,
                        lambda: self.app.renderer.render(n, queue_depth=queue_depth))
                except Exception:
                    render_ok = False
                if not render_ok:
                    continue
                await self.app.renderer.fade_in()
                shown = True
                last_depth = queue_depth
                remaining = self.app.config.display_seconds
                while remaining > 0 and self.app.running.is_set():
                    await asyncio.sleep(min(0.5, remaining))
                    remaining -= 0.5
                    current_depth = self.queue.qsize()
                    if current_depth != last_depth:
                        last_depth = current_depth
                        try:
                            await loop.run_in_executor(None,
                                lambda d=current_depth: self.app.renderer.render(n, queue_depth=d))
                        except Exception:
                            pass
                await self.app.renderer.fade_out()
                shown = False
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                if shown:
                    try:
                        self.app.renderer.hide()
                    except Exception:
                        pass
                self.queue.task_done()

class TrayController:
    def __init__(self, app: "VRNotificationApp") -> None:
        self.app = app
        self.icon: Optional[Any] = None

    @staticmethod
    def _make_image() -> Image.Image:
        size = 32
        img = Image.new("RGBA", (size, size), color=(0, 0, 0, 0))
        dc = ImageDraw.Draw(img)
        dc.rounded_rectangle((2, 2, size - 2, size - 2), radius=6, fill=(30, 30, 40, 255))
        dc.rounded_rectangle((5, 11, 27, 21), radius=3, fill=(0, 162, 255, 255))
        dc.rounded_rectangle((9, 14, 14, 19), radius=1, fill=(20, 20, 30, 255))
        dc.rounded_rectangle((18, 14, 23, 19), radius=1, fill=(20, 20, 30, 255))
        dc.ellipse((21, 5, 28, 12), fill=(255, 80, 80, 255))
        return img

    def start(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Open Control Panel", self._show_window, default=True),
            pystray.MenuItem("Toggle DND", self._toggle_dnd),
            pystray.MenuItem("Quit", self.app.quit_app),
        )
        self.icon = pystray.Icon("VRNotifier", self._make_image(), "Windows VR Notifier", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def stop(self) -> None:
        if self.icon is not None:
            self.icon.stop()

    def _show_window(self, icon=None, item=None) -> None:
        try:
            self.app.gui.root.after(0, self.app.gui.show_window)
        except Exception:
            pass

    def _toggle_dnd(self, icon=None, item=None) -> None:
        try:
            self.app.gui.root.after(0, lambda: self.app.set_dnd(not self.app.dnd_enabled))
        except Exception:
            pass

class ControlPanel:
    def __init__(self, app: "VRNotificationApp") -> None:
        self.app = app
        self._initializing = True
        self.root = tk.Tk()
        self.root.title("Windows VR Notifier")
        self.root.geometry("440x560")
        self.root.resizable(False, False)
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._initializing = False
        self.root.after(100, self._refresh_history)

    def _build(self) -> None:
        frame = tk.Frame(self.root, padx=20, pady=15)
        frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame, text="Windows VR Notifier", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        self.lbl_status = tk.Label(frame, text=self._status_text(), font=("Segoe UI", 10), fg=self._status_color())
        self.lbl_status.pack(anchor="w", pady=(2, 10))
        self.notebook = ttk.Notebook(frame)
        self.notebook.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        self._build_controls_tab()
        self._build_position_tab()
        self._build_appearance_tab()
        self._build_history_tab()
        btn_frame = tk.Frame(frame)
        btn_frame.pack(fill=tk.X)
        tk.Button(btn_frame, text="Hide to Tray", width=14, command=self.hide_window).pack(side=tk.LEFT)
        tk.Button(btn_frame, text="Quit App", width=14, bg="#ffdddd", command=self.app.quit_app).pack(side=tk.RIGHT)

    def _build_controls_tab(self) -> None:
        tab = tk.Frame(self.notebook, padx=15, pady=15)
        self.notebook.add(tab, text="Controls")
        self.dnd_var = tk.BooleanVar(value=self.app.dnd_enabled)
        tk.Checkbutton(tab, text="Do Not Disturb (Silence VR Overlay)", font=("Segoe UI", 10, "bold"),
            variable=self.dnd_var, command=self._on_toggle_dnd).pack(anchor="w", pady=(0, 15))
        tk.Button(tab, text="Send Test Notification", font=("Segoe UI", 10, "bold"), bg="#e0e0e0",
            command=self.app.send_test_notification).pack(fill=tk.X, pady=(0, 8))
        self.btn_preview = tk.Button(tab, text="Preview Overlay", font=("Segoe UI", 10, "bold"),
            command=self._toggle_preview)
        self.btn_preview.pack(fill=tk.X, pady=(0, 15))
        info_text = ("Controls\n\n"
            "• DND — silences all VR toast notifications\n"
            "• Test — sends a sample notification to your headset\n"
            "• Preview — shows a static card for adjusting position\n\n"
            "Use the Position and Appearance tabs to fine-tune the overlay.")
        tk.Label(tab, text=info_text, font=("Segoe UI", 9), justify=tk.LEFT, fg="#666666").pack(anchor="w", fill=tk.X)

    def _build_position_tab(self) -> None:
        tab = tk.Frame(self.notebook, padx=15, pady=15)
        self.notebook.add(tab, text="Position")
        tk.Label(tab, text="Overlay Position", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 10))
        self.slider_x = self._make_slider(tab, "X (Left / Right):", POS_X_RANGE, self.app.config.pos_x)
        self.slider_y = self._make_slider(tab, "Y (Up / Down):", POS_Y_RANGE, self.app.config.pos_y)
        self.slider_z = self._make_slider(tab, "Z (Distance):", POS_Z_RANGE, self.app.config.pos_z)
        tk.Label(tab, text="\nTip: Use the Preview button on the Controls tab\nto see changes in real-time while adjusting.",
            font=("Segoe UI", 9), justify=tk.LEFT, fg="#666666").pack(anchor="w", pady=(10, 0))

    def _build_appearance_tab(self) -> None:
        tab = tk.Frame(self.notebook, padx=15, pady=15)
        self.notebook.add(tab, text="Appearance")
        tk.Label(tab, text="Overlay Appearance", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 10))
        self.slider_width = self._make_slider(tab, "Width (m):", (0.1, 1.5), self.app.config.overlay_width_m)
        self.slider_alpha = self._make_slider(tab, "Alpha:", (0.1, 1.0), self.app.config.overlay_alpha)
        self.slider_duration = self._make_slider(tab, "Display (s):", (1.0, 15.0), self.app.config.display_seconds)
        self.slider_fade = self._make_slider(tab, "Fade (s):", (0.0, 1.0), self.app.config.fade_duration)
        tk.Label(tab, text="\n• Alpha — overlay opacity (0.1 = faint, 1.0 = solid)\n"
            "• Display — how long each notification stays visible\n"
            "• Fade — fade in/out animation duration (0 = instant)",
            font=("Segoe UI", 9), justify=tk.LEFT, fg="#666666").pack(anchor="w", pady=(10, 0))

    def _build_history_tab(self) -> None:
        tab = tk.Frame(self.notebook, padx=15, pady=15)
        self.notebook.add(tab, text="History")
        tk.Label(tab, text="Recent Notifications", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 5))
        self.history_text = scrolledtext.ScrolledText(tab, height=15, wrap=tk.WORD,
            font=("Consolas", 9), state=tk.DISABLED)
        self.history_text.pack(fill=tk.BOTH, expand=True)

    def _make_slider(self, parent, label_text, rng, initial):
        tk.Label(parent, text=label_text, font=("Segoe UI", 8)).pack(anchor="w")
        slider = tk.Scale(parent, from_=rng[0], to=rng[1], resolution=0.05,
            orient=tk.HORIZONTAL, command=self._on_slider_change)
        slider.set(initial)
        slider.pack(fill=tk.X, pady=(0, 5))
        slider.bind("<ButtonRelease-1>", lambda e: self._on_slider_release())
        return slider

    def _status_text(self) -> str:
        if self.app.renderer is None:
            return "Connecting to SteamVR..."
        return "DND active" if self.app.dnd_enabled else "Running & listening"

    def _status_color(self) -> str:
        if self.app.renderer is None:
            return "gray"
        return "orange" if self.app.dnd_enabled else "green"

    def refresh_status(self) -> None:
        self.lbl_status.config(text=self._status_text(), fg=self._status_color())
        self.dnd_var.set(self.app.dnd_enabled)

    def _on_toggle_dnd(self) -> None:
        self.app.set_dnd(self.dnd_var.get())
        self.refresh_status()

    def _on_slider_change(self, _val) -> None:
        if self._initializing or self.app.renderer is None:
            return
        cfg = self.app.config
        cfg.pos_x = self.slider_x.get()
        cfg.pos_y = self.slider_y.get()
        cfg.pos_z = self.slider_z.get()
        cfg.overlay_width_m = self.slider_width.get()
        cfg.overlay_alpha = self.slider_alpha.get()
        cfg.display_seconds = self.slider_duration.get()
        cfg.fade_duration = self.slider_fade.get()
        try:
            self.app.renderer.apply_transform()
            self.app.renderer.overlay.setOverlayWidthInMeters(
                self.app.renderer.overlay_handle, cfg.overlay_width_m)
            self.app.renderer.update_live_alpha()
        except Exception:
            pass

    def _on_slider_release(self) -> None:
        if self._initializing:
            return
        self.app.config.save()
        if self.app.dashboard is not None:
            try:
                self.app.dashboard.render_panel()
            except Exception:
                pass

    def _toggle_preview(self) -> None:
        self.app.toggle_preview()

    def refresh_preview_button(self) -> None:
        if not hasattr(self, "btn_preview"):
            return
        if self.app.preview_active:
            self.btn_preview.config(text="Stop Preview", relief=tk.SUNKEN)
        else:
            self.btn_preview.config(text="Preview Overlay", relief=tk.RAISED)

    def _refresh_history(self) -> None:
        try:
            if self.root.winfo_exists():
                items = self.app.history.snapshot()
                self.history_text.config(state=tk.NORMAL)
                self.history_text.delete("1.0", tk.END)
                for n in items[: self.app.config.history_max]:
                    ts = time.strftime("%H:%M:%S", time.localtime(n.timestamp))
                    line = f"[{ts}] {n.app_name} — {n.title}\n  {n.body}\n\n"
                    self.history_text.insert(tk.END, line)
                self.history_text.config(state=tk.DISABLED)
        except Exception:
            pass
        try:
            self.root.after(1000, self._refresh_history)
        except Exception:
            pass

    def hide_window(self) -> None:
        self.root.withdraw()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def mainloop(self) -> None:
        self.root.mainloop()

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass

class VRNotificationApp:
    def __init__(self) -> None:
        self.config = ConfigStore()
        self.running = threading.Event()
        self.running.set()
        self._dnd_lock = threading.Lock()
        self._dnd_enabled = self.config.dnd_enabled
        self._preview_lock = threading.Lock()
        self._preview_active = False
        self.history = NotificationHistory(max_items=self.config.history_max)
        self.overlay = None
        self.overlay_handle = None
        self.renderer: Optional[OverlayRenderer] = None
        self.dashboard: Optional[DashboardPanel] = None
        self.listener = None
        self.tray = None
        self._listener_thread: Optional[threading.Thread] = None
        self.gui = ControlPanel(self)
        try:
            self.gui.root.update_idletasks()
            self.gui.root.update()
        except Exception:
            pass
        threading.Thread(target=self._init_steamvr, daemon=True).start()

    def _init_steamvr(self) -> None:
        try:
            openvr.init(openvr.VRApplication_Overlay)
            overlay = openvr.IVROverlay()
            self._ensure_manifest_registered()
            overlay_handle = overlay.createOverlay(OVERLAY_KEY, OVERLAY_NAME)
            renderer = OverlayRenderer(overlay, overlay_handle, self.config)
            try:
                self.dashboard = DashboardPanel(overlay, self.config, self)
            except Exception:
                self.dashboard = None
            self.overlay = overlay
            self.overlay_handle = overlay_handle
            self.renderer = renderer
            self.listener = NotificationListener(self)
            self.gui.root.after(0, self._on_steamvr_ready)
        except openvr.OpenVRError as e:
            err_msg = str(e) 
            self.gui.root.after(0, lambda: self._on_steamvr_failed(err_msg))
        except Exception as e:
            err_msg = str(e)
            self.gui.root.after(0, lambda: self._on_steamvr_failed(err_msg))

    def _on_steamvr_ready(self) -> None:
        self.gui.refresh_status()
        self.tray = TrayController(self)
        self.gui.root.after(300, self.tray.start)
        self.gui.root.after(600, self.start_listener)

    def _on_steamvr_failed(self, error: str) -> None:
        self.gui.lbl_status.config(text=f"SteamVR error: {error[:60]}", fg="red")

    def _ensure_manifest_registered(self) -> None:
        self._write_manifest()
        try:
            apps = openvr.VRApplications()
            try:
                if apps.isApplicationInstalled(OVERLAY_KEY):
                    apps.removeApplicationManifest(OVERLAY_KEY)
            except Exception:
                pass
            apps.addApplicationManifest(MANIFEST_PATH)
            apps.setApplicationAutoLaunch(OVERLAY_KEY, True)
        except Exception:
            pass

    def _write_manifest(self) -> None:
        target_url = pathlib.Path(sys.executable).as_uri()
        working_dir = BASE_DIR.replace("\\", "/") + "/"
        manifest = {
            "source": "builtin",
            "applications": [{
                "app_key": OVERLAY_KEY,
                "launch_type": "url",
                "url": target_url,
                "working_directory": working_dir,
                "image_path": "",
                "is_dashboard_overlay": True,
                "strings": {"en_us": {
                    "name": OVERLAY_NAME,
                    "description": "Pipes Windows Toast Notifications directly into SteamVR",
                }},
            }],
        }
        try:
            with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
        except Exception:
            pass

    @property
    def dnd_enabled(self) -> bool:
        with self._dnd_lock:
            return self._dnd_enabled

    def set_dnd(self, value: bool) -> None:
        with self._dnd_lock:
            self._dnd_enabled = bool(value)
        self.config.dnd_enabled = bool(value)
        self.config.save()
        if not value and self.renderer is not None:
            try:
                self.renderer.hide()
                self.renderer._set_alpha(self.config.overlay_alpha)
            except Exception:
                pass
        try:
            self.gui.root.after(0, self.gui.refresh_status)
        except Exception:
            pass
        if self.dashboard is not None:
            threading.Thread(target=self._safe_render_dashboard, daemon=True).start()

    def _safe_render_dashboard(self) -> None:
        if self.dashboard is None:
            return
        try:
            self.dashboard.render_panel()
        except Exception:
            pass

    @property
    def preview_active(self) -> bool:
        with self._preview_lock:
            return self._preview_active

    def toggle_preview(self) -> None:
        if self.renderer is None:
            return
        with self._preview_lock:
            self._preview_active = not self._preview_active
            active = self._preview_active
        if active:
            self.renderer.show_preview()
        else:
            self.renderer.hide_preview()
        try:
            self.gui.root.after(0, self.gui.refresh_preview_button)
        except Exception:
            pass
        if self.dashboard is not None:
            threading.Thread(target=self._safe_render_dashboard, daemon=True).start()

    def send_test_notification(self) -> None:
        if self.listener is None:
            return
        loop = self.listener.loop
        queue = self.listener.queue
        if loop is None or loop.is_closed() or queue is None:
            self.start_listener()
            self.gui.root.after(2000, self._send_test_after_restart)
            return
        try:
            asyncio.run_coroutine_threadsafe(
                queue.put(("Test App", "Hello from VR Notifier!",
                    "If you see this in your headset, positioning works!", None)),
                loop)
        except RuntimeError as e:
            self.start_listener()
            self.gui.root.after(2000, self._send_test_after_restart)

    def _send_test_after_restart(self) -> None:
        if (self.listener is None or self.listener.loop is None or self.listener.queue is None):
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.listener.queue.put(("Test App", "Hello from VR Notifier!",
                    "If you see this in your headset, positioning works!", None)),
                self.listener.loop)
        except Exception:
            pass

    def start_listener(self) -> None:
        if self._listener_thread is not None and self._listener_thread.is_alive():
            return
        self._listener_thread = threading.Thread(target=self._run_asyncio, daemon=True)
        self._listener_thread.start()

    def _run_asyncio(self) -> None:
        backoff = 1.0
        while self.running.is_set():
            try:
                asyncio.run(self.listener.run())
                break
            except Exception:
                if not self.running.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                self.listener.loop = None
                self.listener._worker_task = None

    def quit_app(self, _icon=None, _item=None) -> None:
        self.running.clear()
        if self.listener and self.listener.loop and self.listener._worker_task:
            def _cancel():
                self.listener._worker_task.cancel()
            try:
                asyncio.run_coroutine_threadsafe(_cancel(), self.listener.loop).result(timeout=2.0)
            except Exception:
                pass
        if self.tray is not None:
            self.tray.stop()
        try:
            self.gui.root.after(0, self._do_shutdown)
        except Exception:
            self._do_shutdown()

    def _steamvr_shutdown(self) -> None:
        if not self.running.is_set():
            return
        try:
            self.gui.root.after(0, self.quit_app)
        except Exception:
            self.quit_app()

    def _do_shutdown(self) -> None:
        self.gui.destroy()
        self._cleanup()
        sys.exit(0)

    def _cleanup(self) -> None:
        try:
            openvr.shutdown()
        except Exception:
            pass

if __name__ == "__main__":
    app = VRNotificationApp()
    try:
        app.gui.mainloop()
    except KeyboardInterrupt:
        app.quit_app()
