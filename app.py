from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse
from typing import Optional
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import queue
from ctypes import wintypes
from keyboard import _canonical_names as _kb_canon

import keyboard
import psutil
import atexit

import tkinter as tk
from tkinter import font as tkfont
from tkwry import WebView

APP_VERSION = "3.1"

# --- Acrylic-прозрачность + резкие углы окна (как в window-code.py) ---
# Атрибуты DWM (см. window-code.py, порт мода "Mica" moe.caramel.mica):
#   20  DWMWA_USE_IMMERSIVE_DARK_MODE   тёмный/светлый заголовок
#   33  DWMWA_WINDOW_CORNER_PREFERENCE  скругление углов (1 = DONOTROUND)
#   34  DWMWA_BORDER_COLOR              цвет рамки
#   35  DWMWA_CAPTION_COLOR             цвет заголовка
#   36  DWMWA_TEXT_COLOR                цвет текста заголовка
#   38  DWMWA_SYSTEMBACKDROP_TYPE       Mica / Acrylic фон
# Только ctypes, без сторонних зависимостей.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38

DWMSBT_AUTO = 0
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2
DWMSBT_TRANSIENTWINDOW = 3
DWMSBT_TABBEDWINDOW = 4

DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3

DWMWA_COLOR_NONE = 0xFFFFFFFE
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF


def _build_number() -> int | None:
    """Версия сборки Windows (напр. 22621, 26300) или None при ошибке."""
    if sys.platform != "win32":
        return None
    try:
        ctypes.windll.kernel32.GetVersionExW.restype = wintypes.BOOL
    except Exception:
        pass

    class OSVERSIONINFOEXW(ctypes.Structure):
        _fields_ = [
            ("dwOSVersionInfoSize", wintypes.DWORD),
            ("dwMajorVersion", wintypes.DWORD),
            ("dwMinorVersion", wintypes.DWORD),
            ("dwBuildNumber", wintypes.DWORD),
            ("dwPlatformId", wintypes.DWORD),
            ("szCSDVersion", wintypes.WCHAR * 128),
            ("wServicePackMajor", wintypes.WORD),
            ("wServicePackMinor", wintypes.WORD),
            ("wSuiteMask", wintypes.WORD),
            ("wProductType", wintypes.BYTE),
            ("wReserved", wintypes.BYTE),
        ]

    info = OSVERSIONINFOEXW()
    info.dwOSVersionInfoSize = ctypes.sizeof(OSVERSIONINFOEXW)
    try:
        if not ctypes.windll.kernel32.GetVersionExW(ctypes.byref(info)):
            return None
    except Exception:
        return None
    return int(info.dwBuildNumber)


def _window_hwnd(root) -> int | None:
    """HWND верхнеуровневого Tk-окна (с заголовком и рамкой).

    ``winfo_id()`` отдаёт внутренний TkChild-HWND (DWM его отклоняет),
    поэтому добираемся до корня через GetAncestor(GA_ROOT) — как в
    window-code.py."""
    if sys.platform != "win32":
        return None
    try:
        inner = int(root.winfo_id())
    except Exception:
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wintypes.HWND
        root_hwnd = int(user32.GetAncestor(wintypes.HWND(inner), 2))  # GA_ROOT
    except Exception:
        return inner
    return root_hwnd if root_hwnd else inner


def _apply_dwm(hwnd: int, attribute: int, value: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
    except Exception:
        pass
    buf = ctypes.byref(ctypes.c_int(value))
    try:
        result = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), attribute, buf, ctypes.sizeof(ctypes.c_int)
        )
    except Exception:
        return False
    return result == 0


def apply_mica(
    root,
    *,
    backdrop: int | None = DWMSBT_TRANSIENTWINDOW,  # Acrylic — полупрозрачный
    dark: bool = True,
    corner: int | None = DWMWCP_DONOTROUND,          # резкие углы
    border_color: int | None = DWMWA_COLOR_NONE,
    caption_color: int | None = None,
    text_color: int | None = 0xFFFFFF,               # белый текст заголовка
) -> bool:
    """Применить DWM-фон/цвета/углы к Tk-окну, как мод Mica в window-code.py."""
    hwnd = _window_hwnd(root)
    if hwnd is None:
        return False

    applied = False
    build = _build_number()

    applied = _apply_dwm(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark)) or applied

    if backdrop is not None and build is not None and build >= 22621:
        _apply_dwm(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, backdrop)

    if corner is not None:
        _apply_dwm(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, corner)

    if border_color is not None:
        _apply_dwm(hwnd, DWMWA_BORDER_COLOR, border_color)
    if caption_color is not None:
        _apply_dwm(hwnd, DWMWA_CAPTION_COLOR, caption_color)
    if text_color is not None:
        _apply_dwm(hwnd, DWMWA_TEXT_COLOR, text_color)

    return applied

# Каталог приложения: в собранной версии (PyInstaller one-dir) это папка, где
# лежит .exe. Конфиги и картинки читаются/создаются рядом с exe, а не в _internal
# и не в текущем рабочем каталоге.
def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = _app_dir()

def _app_path(name):
    return os.path.join(APP_DIR, name)

# Ресурсы приложения, которые бандлятся сборкой PyInstaller в подкаталог
# _internal: screen.png, app.ico и т.п. В исходнике они лежат рядом с app.py.
def _internal_path(name):
    if getattr(sys, "frozen", False):
        base = os.path.join(os.path.dirname(sys.executable), "_internal")
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return _app_path(name)

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))

def _jinja(request: Request, name: str, **context):
    """Рендер Jinja-шаблона с подстановкой request; аналог render_template."""
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
    )

# В собранном (PyInstaller onedir) приложении scrcpy.exe, adb.exe и DLL лежат в
# каталоге _internal, которого нет в PATH. Добавляем его, чтобы команды
# "scrcpy"/"adb" находились и в исходнике, и в собранном exe.
def _bin_dirs():
    dirs = []
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.join(here, "_internal"),
              os.path.join(os.getcwd(), "_internal"),
              os.getcwd()):
        if d not in dirs and os.path.isdir(d):
            dirs.append(d)
    return dirs

for d in _bin_dirs():
    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

# Получение ID активного окна
user32 = ctypes.windll.user32
GetForegroundWindow = user32.GetForegroundWindow
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
GetWindowThreadProcessId.restype = wintypes.DWORD

# SetWindowPos — используется для быстрого перемещения окон маркеров без
# перерисовки и без SetWindowPos через Tk (который каждый раз пересоздаёт
# window style и вызывает мерцание/рывки).
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOREDRAW = 0x0008
SWP_ASYNCWINDOWPOS = 0x4000

user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.SetWindowPos.restype = wintypes.BOOL

def active_is_scrcpy_or_app():
    """Активно ли окно scrcpy либо главное Tk-окно приложения."""
    hwnd = GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return True
    try:
        process = psutil.Process(pid.value)
        return str(process.name()).lower() == 'scrcpy.exe'
    except psutil.NoSuchProcess:
        return False

def find_window_by_pid(pid):
    """Возвращает HWND видимого top-level окна процесса с заданным PID, или 0."""
    if not pid:
        return 0
    hwnd = user32.FindWindowExW(None, None, None, None)
    while hwnd:
        wpid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            return hwnd
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    return 0


def get_window_rect(hwnd):
    """Прямоугольник окна в экранных координатах: (left, top, right, bottom)."""
    if not hwnd:
        return None
    try:
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def move_resize_window(hwnd, left, top, width, height):
    """Переместить/изменить размер окна hwnd с мгновенным результатом."""
    if not hwnd:
        return False
    try:
        return bool(user32.SetWindowPos(
            hwnd, 0, int(left), int(top), int(width), int(height),
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER))
    except Exception:
        return False


class ScrcpyDocker:
    """Размещает окно scrcpy при его появлении: сразу справа от главного окна,
    с той же высотой, ширина — по реальному соотношению сторон устройства.
    Окно позиционируется один раз при появлении и больше не двигается."""

    def __init__(self, main_root):
        self.root = main_root
        self.main_hwnd = None
        self._positioned_hwnd = 0   # окно, которому уже задали позицию
        self.phone_aspect = 2240.0 / 1260.0  # h/w реального устройства

    def _main_hwnd(self):
        if self.main_hwnd and user32.IsWindow(self.main_hwnd):
            return self.main_hwnd
        self.main_hwnd = _window_hwnd(self.root) or 0
        return self.main_hwnd

    def _scr_hwnd(self):
        pid = controller.scrcpy_pid
        if not pid:
            return 0
        return find_window_by_pid(pid)

    def update(self):
        """Вызывается из Tk-цикла (root.after) каждый тик."""
        scr = self._scr_hwnd()
        main = self._main_hwnd()
        if not scr or not main:
            return

        # Заголовок окна scrcpy — тип подключения и адрес устройства.
        try:
            user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
            user32.SetWindowTextW.restype = wintypes.BOOL
            if controller.connection_type == "wifi":
                title = f"Wi-Fi: {controller.adb_ip}:{controller.adb_port}"
            else:
                title = "USB: " + (controller.device_name or
                                   controller.device_serial or "—")
            user32.SetWindowTextW(scr, title)
        except Exception:
            pass

        # Позиционируем только один раз при появлении окна scrcpy.
        if self._positioned_hwnd == scr:
            return
        main_geom = get_window_rect(main)
        if not main_geom:
            return
        self._position(scrcpy_hwnd=scr, main_geom=main_geom)

    def _position(self, scrcpy_hwnd, main_geom):
        ml, mt, mr, mb = main_geom
        mw = mr - ml
        mh = mb - mt
        # Высота равна высоте главного окна, ширина из соотношения сторон.
        new_h = mh
        new_w = int(new_h / self.phone_aspect) if self.phone_aspect else mw
        new_left = mr + 1
        new_top = mt
        move_resize_window(scrcpy_hwnd, new_left, new_top, new_w, new_h)
        self._positioned_hwnd = scrcpy_hwnd

WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
LWA_COLORKEY = 0x00000001
LWA_ALPHA = 0x00000002
COLORKEY = 0x00010101  # RGB #010101 — прозрачный цвет фона оверлея
COLORKEY_HEX = '#010101'

def png_dimensions(path):
    """Размеры PNG из заголовка IHDR без библиотек."""
    try:
        with open(path, 'rb') as f:
            header = f.read(24)
        if header[:8] != b'\x89PNG\r\n\x1a\n':
            return None
        w = int.from_bytes(header[16:20], 'big')
        h = int.from_bytes(header[20:24], 'big')
        return w, h
    except Exception:
        return None

class MarkerOverlay:
    """
    Некликабельный оверлей маркеров поверх окна scrcpy.
    Каждый маркер — отдельное top-level окно со СВОЕЙ альфой.
    Плюс отдельное layered-окно под полупрозрачный «хвост» (tail),
    чтобы получить ровные 50% alpha без stipple-артефактов.
    """
    MARKER_GEOM = {
        "pause": ("#6ad2ff", "ПАУЗА"),
        "like":  ("#fe2c55", "ЛАЙК"),
        "pin":   ("#face15", "ИЗБРАННОЕ"),
        "scroll_up":   ("#3399ff", "СЛЕДУЮЩЕЕ ВИДЕО"),
        "scroll_down": ("#3399ff", "ПРЕДЫДУЩЕЕ ВИДЕО"),
    }
    SCROLL_MARKERS = ("scroll_up", "scroll_down")
    FADE_TIME_S = 0.2
    FADE_STEP_PER_S = 255.0 / FADE_TIME_S
    FLASH_DURATION = 1.0
    DEFAULT_COORDS = {
        "pause": "630 450", "like": "630 1020", "pin": "630 1180",
        "scroll_up": "630 1120", "scroll_down": "630 1120",
    }

    LOCAL_CROSS = (12, 32)
    CROSS_GAP = 2
    CROSS_LEN = 4
    CROSS_WIDTH = 1

    TAIL_LEN = 22
    TAIL_ALPHA = 255         # 50% от 255

    TICK_MS = 50            # медленный тик в простое (маркеры скрыты)
    TICK_FAST_MS = 5        # быстрый тик при активной fade-анимации

    def __init__(self, root):
        self.root = root
        self.phone_w = 1260
        self.phone_h = 2240
        self.markers = {}
        self.alphas = {n: 0 for n in self.MARKER_GEOM}
        self._flashes = {}
        self._scr_hwnd = 0
        self._last_geom = None
        self._last_tick = time.perf_counter()
        for name in self.MARKER_GEOM:
            self._ensure_marker(name)

    # ---------- служебное ----------

    def _active_is_scrcpy_or_app(self):
        return active_is_scrcpy_or_app()

    def flash_now(self, action=None):
        now = time.time()
        if action is None:
            for n in self.MARKER_GEOM:
                self._flashes[n] = now + self.FLASH_DURATION
        else:
            self._flashes[action] = now + self.FLASH_DURATION
            if action in self.SCROLL_MARKERS:
                sibling = "scroll_up" if action == "scroll_down" else "scroll_down"
                self._flashes.pop(sibling, None)

    def _top_hwnd(self, name):
        m = self.markers.get(name)
        if not m:
            return 0
        h = m.get('hwnd') or 0
        if h and user32.IsWindow(h):
            return h
        try:
            h = m['win'].winfo_id()
            for _ in range(4):
                p = user32.GetParent(h)
                if not p:
                    break
                h = p
            m['hwnd'] = h
            return h
        except Exception:
            return 0

    def _tail_hwnd(self, name):
        m = self.markers.get(name)
        if not m or not m.get('tail_win'):
            return 0
        h = m.get('tail_hwnd') or 0
        if h and user32.IsWindow(h):
            return h
        try:
            h = m['tail_win'].winfo_id()
            for _ in range(4):
                p = user32.GetParent(h)
                if not p:
                    break
                h = p
            m['tail_hwnd'] = h
            return h
        except Exception:
            return 0

    def _ensure_marker(self, name):
        if name in self.markers:
            return
        color, label = self.MARKER_GEOM[name]

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=COLORKEY_HEX)
        win.withdraw()

        # Плашки scroll_up/scroll_down — без изменений
        if name in self.SCROLL_MARKERS:
            font = ('Consolas', 10, 'bold')
            text_w = int(tkfont.Font(font=font).measure(label))
            win_w = text_w + 10
            win_h = 18
            self.markers[name] = {
                'win': win, 'canvas': None,
                'widgets': {'label': label},
                'w': win_w, 'h': win_h,
                'visible': False, 'hwnd': 0,
                'applied_alpha': -1, 'applied_pos': None,
                'tail_win': None, 'tail_hwnd': 0,
                'tail_applied_pos': None,
                'tail_applied_alpha': -1,
                'coord_xy': None, 'coord_key': None,
            }
            win.geometry(f"{win_w}x{win_h}")
            box = tk.Frame(win, bg='#000000')
            box.place(relx=0.5, rely=0.5, anchor='center',
                      relwidth=1.0, relheight=1.0)
            tk.Label(box, text=label, fg=color, bg='#000000',
                     font=font).place(relx=0.5, rely=0.5, anchor='center')
            return

        # --- Обычный маркер: прицел + плашка на основном окне ---
        ox, oy = self.LOCAL_CROSS
        gap = self.CROSS_GAP
        clen = self.CROSS_LEN
        cw = self.CROSS_WIDTH
        reach = gap + clen
        dx = self.TAIL_LEN * 0.866
        dy = -self.TAIL_LEN * 0.5
        text_w = 68 if label == "ИЗБРАННОЕ" else 48
        box_top = oy + dy - 9
        box_bot = oy + dy + 5

        # Основное окно: прицел + плашка. Tail здесь НЕ рисуем.
        win_w = int(ox + dx + text_w + 8)
        win_h = int(max(box_bot, oy + reach) + 6)

        canvas = tk.Canvas(win, width=win_w, height=win_h,
                           bg=COLORKEY_HEX, highlightthickness=0)
        canvas.pack(fill='both', expand=True)

        seg_up = canvas.create_line(ox, oy - gap, ox, oy - gap - clen, fill=color, width=cw)
        seg_dn = canvas.create_line(ox, oy + gap, ox, oy + gap + clen, fill=color, width=cw)
        seg_lf = canvas.create_line(ox - gap, oy, ox - gap - clen, oy, fill=color, width=cw)
        seg_rt = canvas.create_line(ox + gap, oy, ox + gap + clen, oy, fill=color, width=cw)

        fx = ox + dx
        box = canvas.create_rectangle(fx, box_top-2, fx + text_w, box_bot-2,
                                      fill='#000000', outline='#000000')
        text = canvas.create_text(fx + text_w // 2, ((box_top + box_bot) // 2)-2,
                                  text=label, fill=color,
                                  font=('Consolas', 8, 'bold'))
        for seg in (seg_up, seg_dn, seg_lf, seg_rt):
            canvas.tag_raise(seg)

        # --- Отдельное окно под tail с собственной alpha ---
        # Tail рисуем как диагональную линию толщиной 1. Tk не даёт alpha
        # на фигуру, но layered-окно целиком даёт ровные 50%.
        tail_x0 = ox + 2
        tail_y0 = oy - 2
        tail_x1 = ox + dx
        tail_y1 = oy + dy
        # Локальные координаты внутри tail-окна: сдвигаем всё в положительную
        # область, оставляя запас 2px.
        pad = 2
        tw = int(abs(tail_x1 - tail_x0) + pad * 2 + 2)
        th = int(abs(tail_y1 - tail_y0) + pad * 2 + 2)
        lx0 = pad
        ly0 = pad if tail_y0 <= tail_y1 else th - pad
        lx1 = pad + int(abs(tail_x1 - tail_x0))
        ly1 = (th - pad) if tail_y0 <= tail_y1 else pad

        tail_win = tk.Toplevel(self.root)
        tail_win.overrideredirect(True)
        tail_win.attributes('-topmost', True)
        tail_win.configure(bg=COLORKEY_HEX)
        tail_win.withdraw()
        tail_canvas = tk.Canvas(tail_win, width=tw, height=th,
                                bg=COLORKEY_HEX, highlightthickness=0)
        tail_canvas.pack(fill='both', expand=True)
        tail_canvas.create_line(lx0, ly0-2, lx1, ly1-2, fill='#000000', width=2)

        # Смещение tail-окна относительно окна маркера:
        # центр tail-окна ставим так, чтобы координаты tail совпали.
        tail_off_x = int(min(tail_x0, tail_x1) - pad)
        tail_off_y = int(min(tail_y0, tail_y1) - pad)

        self.markers[name] = {
            'win': win, 'canvas': canvas,
            'widgets': {'seg_up': seg_up, 'seg_dn': seg_dn,
                        'seg_lf': seg_lf, 'seg_rt': seg_rt,
                        'box': box, 'text': text},
            'w': win_w, 'h': win_h,
            'visible': False, 'hwnd': 0,
            'applied_alpha': -1, 'applied_pos': None,
            'tail_win': tail_win,
            'tail_canvas': tail_canvas,
            'tail_hwnd': 0,
            'tail_w': tw, 'tail_h': th,
            'tail_off_x': tail_off_x,
            'tail_off_y': tail_off_y,
            'tail_applied_pos': None,
            'tail_applied_alpha': -1,
            'tail_visible': False,
            'coord_xy': None, 'coord_key': None,
        }

    def _apply_click_through(self, name):
        h = self._top_hwnd(name)
        if not h:
            return
        try:
            style = user32.GetWindowLongW(h, GWL_EXSTYLE)
            style |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
            user32.SetWindowLongW(h, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _apply_click_through_tail(self, name):
        h = self._tail_hwnd(name)
        if not h:
            return
        try:
            style = user32.GetWindowLongW(h, GWL_EXSTYLE)
            style |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
            user32.SetWindowLongW(h, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _apply_alpha(self, name, alpha):
        h = self._top_hwnd(name)
        if not h:
            return
        try:
            user32.SetLayeredWindowAttributes(
                h, COLORKEY, max(0, min(255, int(alpha))),
                LWA_COLORKEY | LWA_ALPHA)
        except Exception:
            pass

    def _apply_alpha_tail(self, name, alpha):
        """Tail получает 50% от общей альфы маркера: при fade-in/out
        прозрачность tail меняется пропорционально."""
        h = self._tail_hwnd(name)
        if not h:
            return
        try:
            a = int(int(alpha) * self.TAIL_ALPHA / 255)
            user32.SetLayeredWindowAttributes(
                h, COLORKEY, max(0, min(255, a)),
                LWA_COLORKEY | LWA_ALPHA)
        except Exception:
            pass

    def _move(self, name, x, y):
        h = self._top_hwnd(name)
        if not h:
            return False
        try:
            return bool(user32.SetWindowPos(
                h, 0, int(x), int(y), 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW))
        except Exception:
            return False

    def _move_tail(self, name, x, y):
        h = self._tail_hwnd(name)
        if not h:
            return False
        try:
            return bool(user32.SetWindowPos(
                h, 0, int(x), int(y), 0, 0,
                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOREDRAW))
        except Exception:
            return False

    def _coord_xy(self, name):
        """Координаты маркера (x, y). Кэшируем, пока значение поля контроллера
        не изменилось — в простое (без редактирования координат) это разгружает
        цикл от getattr + split каждый тик."""
        m = self.markers.get(name)
        if m is None:
            return 0, 0
        if 'coord_key' not in m or 'coord_xy' not in m:
            m['coord_key'] = None
            m['coord_xy'] = None
        val = getattr(controller, name + "_coords", None)
        if not val:
            val = self.DEFAULT_COORDS.get(name, "0 0")
        if m['coord_key'] != val:
            try:
                m['coord_xy'] = tuple(int(v) for v in str(val).split())
                m['coord_key'] = val
            except Exception:
                m['coord_xy'] = (0, 0)
                m['coord_key'] = val
        return m['coord_xy']

    # ---------- основной цикл ----------

    def update(self, dt):
        scr_pid = controller.scrcpy_pid
        if scr_pid:
            if self._scr_hwnd and not user32.IsWindow(self._scr_hwnd):
                self._scr_hwnd = 0
            if not self._scr_hwnd:
                self._scr_hwnd = find_window_by_pid(scr_pid)
                # У окна scrcpy — тот же материал фона (Acrylic), тёмная рамка и
                # резкие углы, что и у главного окна (см. apply_mica).
                if self._scr_hwnd:
                    _apply_dwm(
                        self._scr_hwnd,
                        DWMWA_SYSTEMBACKDROP_TYPE,
                        DWMSBT_TRANSIENTWINDOW,
                    )
                    _apply_dwm(
                        self._scr_hwnd,
                        DWMWA_USE_IMMERSIVE_DARK_MODE,
                        1,
                    )
                    _apply_dwm(
                        self._scr_hwnd,
                        DWMWA_WINDOW_CORNER_PREFERENCE,
                        DWMWCP_DONOTROUND,
                    )
            scr_hwnd = self._scr_hwnd
        else:
            self._scr_hwnd = 0
            scr_hwnd = 0

        now = time.time()
        if self._flashes:
            for n in list(self._flashes.keys()):
                if self._flashes[n] <= now:
                    del self._flashes[n]

        flashing_markers = [n for n in self.MARKER_GEOM if n in self._flashes]
        settings_active = bool(
            scr_hwnd and controller.current_tab == "settings"
            and self._active_is_scrcpy_or_app())

        step = self.FADE_STEP_PER_S * max(0.0, min(dt, 0.1))
        flashing = bool(self._flashes) or settings_active
        any_visible = False
        for name in self.MARKER_GEOM:
            target = 255 if (name in flashing_markers or settings_active) else 0
            cur = self.alphas[name]
            if cur < target:
                cur = min(target, cur + step)
            elif cur > target:
                cur = max(target, cur - step)
            self.alphas[name] = cur
            if cur > 0:
                any_visible = True

        # В простое: всё скрыто и ничего не анимируется — WinAPI-чтение
        # геометрии scrcpy не нужно. Только прячем окна, если ещё не спрятаны.
        if not any_visible and not flashing:
            for name in self.MARKER_GEOM:
                m = self.markers[name]
                if m['visible']:
                    try:
                        m['win'].withdraw()
                    except Exception:
                        pass
                    m['visible'] = False
                    m['applied_alpha'] = -1
                if m.get('tail_win') and m.get('tail_visible'):
                    try:
                        m['tail_win'].withdraw()
                    except Exception:
                        pass
                    m['tail_visible'] = False
                    m['tail_applied_alpha'] = -1
            return

        if scr_hwnd:
            cur_geom = self._read_scrcpy_geom(scr_hwnd)
            if cur_geom is None:
                self._scr_hwnd = 0
                return
            self._last_geom = cur_geom
            ox, oy, sw, sh = cur_geom
        elif any_visible:
            sw = int(self.phone_w * 0.35)
            sh = int(self.phone_h * 0.35)
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            ox = screen_w - sw - 40
            oy = screen_h - sh - 80
        else:
            for name in self.MARKER_GEOM:
                m = self.markers[name]
                if m['visible']:
                    try:
                        m['win'].withdraw()
                    except Exception:
                        pass
                    m['visible'] = False
                    m['applied_alpha'] = -1
                if m.get('tail_win') and m.get('tail_visible'):
                    try:
                        m['tail_win'].withdraw()
                    except Exception:
                        pass
                    m['tail_visible'] = False
                    m['tail_applied_alpha'] = -1
            return

        local_ox, local_oy = self.LOCAL_CROSS

        for name in self.MARKER_GEOM:
            alpha = self.alphas[name]
            m = self.markers[name]

            if alpha <= 0:
                if m['visible']:
                    try:
                        m['win'].withdraw()
                    except Exception:
                        pass
                    m['visible'] = False
                    m['applied_alpha'] = -1
                if m.get('tail_win') and m.get('tail_visible'):
                    try:
                        m['tail_win'].withdraw()
                    except Exception:
                        pass
                    m['tail_visible'] = False
                    m['tail_applied_alpha'] = -1
                continue

            x, y = self._coord_xy(name)
            px = ox + x / self.phone_w * sw
            py = oy + y / self.phone_h * sh

            ww, wh = m['w'], m['h']
            if name in self.SCROLL_MARKERS:
                pcx = ox + sw // 2
                pcy = oy + int(sh * 0.98)
                wx = int(pcx - ww // 2)
                wy = int(pcy - wh // 2)
            else:
                wx = int(px - local_ox)
                wy = int(py - local_oy)

            pos = (wx, wy, ww, wh)

            need_show = not m['visible']
            if need_show:
                try:
                    m['win'].geometry(f"{ww}x{wh}+{wx}+{wy}")
                except Exception:
                    pass
                m['applied_pos'] = pos
                m['win'].deiconify()
                m['visible'] = True
                self._apply_click_through(name)
                self._apply_alpha(name, int(alpha))
                m['applied_alpha'] = int(alpha)
                m['hwnd'] = 0
                self._top_hwnd(name)

                # Показываем tail-окно для обычных маркеров
                if m.get('tail_win'):
                    tw_, th_ = m['tail_w'], m['tail_h']
                    tx = wx + m['tail_off_x']
                    ty = wy + m['tail_off_y']
                    try:
                        m['tail_win'].geometry(f"{tw_}x{th_}+{tx}+{ty}")
                    except Exception:
                        pass
                    m['tail_applied_pos'] = (tx, ty, tw_, th_)
                    m['tail_win'].deiconify()
                    m['tail_visible'] = True
                    self._apply_click_through_tail(name)
                    ai = int(alpha)
                    self._apply_alpha_tail(name, ai)
                    m['tail_applied_alpha'] = ai * self.TAIL_ALPHA // 255
                    m['tail_hwnd'] = 0
                    self._tail_hwnd(name)
                continue

            if m['applied_pos'] != pos:
                if not self._move(name, wx, wy):
                    try:
                        m['win'].geometry(f"{ww}x{wh}+{wx}+{wy}")
                    except Exception:
                        pass
                m['applied_pos'] = pos

            ai = int(alpha)
            if m['applied_alpha'] != ai:
                self._apply_alpha(name, ai)
                m['applied_alpha'] = ai

            # Синхронизация tail-окна: позиция + альфа
            if m.get('tail_win'):
                tw_, th_ = m['tail_w'], m['tail_h']
                tx = wx + m['tail_off_x']
                ty = wy + m['tail_off_y']
                tpos = (tx, ty, tw_, th_)
                if m.get('tail_applied_pos') != tpos:
                    if not self._move_tail(name, tx, ty):
                        try:
                            m['tail_win'].geometry(f"{tw_}x{th_}+{tx}+{ty}")
                        except Exception:
                            pass
                    m['tail_applied_pos'] = tpos
                # Алюма tail применяем ТОЛЬКО при изменении — в плато вспышки
                # значение стабильно и лишние SetLayeredWindowAttributes не нужны.
                tai = ai * self.TAIL_ALPHA // 255
                if m.get('tail_applied_alpha') != tai:
                    self._apply_alpha_tail(name, ai)
                    m['tail_applied_alpha'] = tai

    def _read_scrcpy_geom(self, scr_hwnd):
        try:
            cref = wintypes.RECT()
            user32.GetClientRect(scr_hwnd, ctypes.byref(cref))
            w = cref.right - cref.left
            h = cref.bottom - cref.top
            if w <= 0 or h <= 0:
                return None
            pt = wintypes.POINT(0, 0)
            user32.ClientToScreen(scr_hwnd, ctypes.byref(pt))
            scale = min(w / self.phone_w, h / self.phone_h)
            sw = int(self.phone_w * scale)
            sh = int(self.phone_h * scale)
            ox = pt.x + (w - sw) // 2
            oy = pt.y + (h - sh) // 2
            return (ox, oy, sw, sh)
        except Exception:
            return None

    def _tick(self):
        now = time.perf_counter()
        dt = now - self._last_tick
        self._last_tick = now
        try:
            self.update(dt)
        except Exception:
            pass
        # Адаптивная частота тика. Когда маркеры полностью скрыты и нет
        # вспышек/открытых настроек, update() рано выходит не трогая WinAPI —
        # тикаем редко. При анимации (вспышка) или открытой вкладке настроек —
        # быстрый тик для плавного fade. Проверка активности не зависит от
        # GetForegroundWindow на каждый тик: решение принимается по состоянию.
        if self._flashes or controller.current_tab == "settings":
            next_ms = self.TICK_FAST_MS
        else:
            next_ms = self.TICK_MS
        self.root.after(next_ms, self._tick)

def close_console():
    """Закрывает окно консоли (cmd из app.bat). Windows убьёт все процессы этой консоли: и app.py, и scrcpy."""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    except Exception:
        pass

def kill_scrcpy_by_pid():
    """Убивает ТОЛЬКО свой scrcpy по сохранённому PID (и его дерево)."""
    pid = getattr(controller, 'scrcpy_pid', None) if 'controller' in globals() else None
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            proc.terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    time.sleep(0.2)

try:
    atexit.register(kill_scrcpy_by_pid)
except Exception:
    pass

def force_exit():
    """Запасной вариант — безусловное завершение процесса и его детей."""
    try:
        subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
    try:
        os._exit(0)
    except Exception:
        pass

class AndroidController:
    def __init__(self):
        self.log_messages = []
        self.config_file = _app_path("config.json")
        self.connection_type = "usb"
        self.adb_ip = "192.168.1.1"
        self.adb_port = "5555"
        self.scrcpy_fps = "60"
        self.scrcpy_hwdec = "auto"
        self.scrcpy_codec = "h264"
        self.scrcpy_bitrate = ""
        self.scrcpy_video_buffer = ""
        self.scrcpy_audio_buffer = ""
        self.scrcpy_process = None
        self.scrcpy_pid = None
        self.current_tab = "control"
        self.device_serial = None
        self.device_name = None
        self._device_monitor_active = False
        self.hotkeys = {
            'pause': 'num 0',
            'like': 'num 5',
            'pin': 'num 2',
            'scroll_up': 'num 3',
            'scroll_down': 'num 1',
        }
 
        self.load_hotkeys()
        self.load_connection_settings()
        self.detect_device_serial()
        self.load_coords()
        self.start_scrcpy()
        
        self.keyboard_listener_active = True
        self.hotkey_queue = queue.Queue()
        self.hotkey_scan_map = {}
        self._pressed_keys = set()
        self._keypad_scans = self._load_keypad_scans()
        self._build_hotkey_scan_map()
        self.kb_hook = keyboard.hook(self._on_key_event)
        self.start_keyboard_listener()
        self.start_scrcpy_output_thread()
        self.start_device_monitor()

    def load_hotkeys(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    if 'hotkeys' in config:
                        self.hotkeys.update(config['hotkeys'])
        except Exception as e:
            print(f"Ошибка загрузки горячих клавиш: {e}")
        if hasattr(self, 'hotkey_scan_map'):
            self._build_hotkey_scan_map()

    def detect_device_serial(self):
        """Получает серийный номер текущего подключённого USB-устройства.
        Если устройство не подключено — пишет «Устройство не подключено»
        и не оставляет пугающих ошибок. Иначе пробует несколько источников
        (get-serialno, getprop ro.serialno, getprop ro.boot.serialno)."""
        self.device_serial = None
        try:
            if not self._has_usb_device():
                self.log_message("⚠️ Устройство не подключено", 'info')
                return
            candidates = [
                ["adb", "shell", "get-serialno"],
                ["adb", "shell", "getprop", "ro.serialno"],
                ["adb", "shell", "getprop", "ro.boot.serialno"],
            ]
            for cmd in candidates:
                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        timeout=10
                    )
                except subprocess.TimeoutExpired:
                    continue
                serial = result.stdout.strip()
                if serial and serial.lower() not in ("unknown", "error", "offline") \
                        and "not found" not in serial.lower() \
                        and "inaccessible" not in serial.lower():
                    self.device_serial = serial
                    self.device_name = self.detect_device_name()
                    self.set_scrcpy_window_title()
                    return
            print("Не удалось определить serial устройства")
        except Exception as e:
            print(f"Не удалось определить устройство: {e}")

    def detect_device_name(self):
        """Получает реальное название подключённого устройства (бренд + модель)
        через adb getprop. Возвращает строку названия или None, если устройство
        недоступно/название не определено."""
        try:
            if not self.device_serial:
                return None
            results = {}
            for prop in ["ro.product.manufacturer", "ro.product.model",
                         "ro.product.marketname", "ro.product.device"]:
                try:
                    r = subprocess.run(
                        ["adb", "shell", "getprop", prop],
                        capture_output=True, text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=8,
                    )
                except subprocess.TimeoutExpired:
                    continue
                val = (r.stdout or "").strip()
                if val and val.lower() not in ("unknown", "error", "offline"):
                    results[prop] = val
            if not results:
                return None
            market = results.get("ro.product.marketname")
            if market:
                return market
            manuf = results.get("ro.product.manufacturer")
            model = results.get("ro.product.model")
            if manuf and model:
                if model.lower().startswith(manuf.lower()):
                    return model
                return f"{manuf} {model}"
            return model or manuf
        except Exception as e:
            print(f"Не удалось определить название устройства: {e}")
            return None

    def set_scrcpy_window_title(self):
        """Устанавливает заголовок окна scrcpy в реальное название устройства.
        Окно scrcpy ищется по PID. Безопасно вызывается из любого потока."""
        name = self.device_name
        if not name:
            return
        pid = self.scrcpy_pid
        if not pid:
            return
        try:
            hwnd = find_window_by_pid(pid)
            if hwnd:
                user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
                user32.SetWindowTextW.restype = wintypes.BOOL
                user32.SetWindowTextW(hwnd, f"Устройство: {name}")
        except Exception as e:
            print(f"Не удалось установить название окна scrcpy: {e}")

    def _has_usb_device(self):
        """True, если к adb подключено хотя бы одно устройство (USB/локальное).
        Для Wi-Fi-режима полагаемся на успешность предыдущих команд."""
        try:
            if self.connection_type == "wifi":
                return True
            r = subprocess.run(["adb", "devices"], capture_output=True, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or "List" in line:
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "device":
                    return True
            return False
        except Exception:
            return False

    def save_hotkeys(self):
        try:
            config = {}
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
            
            config['hotkeys'] = self.hotkeys
            
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
            return True
        except Exception as e:
            print(f"Ошибка сохранения горячих клавиш: {e}")
            return False

    def update_hotkey(self, action, key):
        self.hotkeys[action] = key.lower()
        self._build_hotkey_scan_map()
        if self.save_hotkeys():
            self.log_message(f"✅ Горячая клавиша для {action} изменена на {key}", 'success')
            return True
        return False

    def load_connection_settings(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    self.connection_type = config.get("connection_type", "usb")
                    self.adb_ip = config.get("adb_ip", "192.168.1.1")
                    self.adb_port = config.get("adb_port", "5555")
                    self.scrcpy_fps = config.get("scrcpy_fps", "60")
                    self.scrcpy_hwdec = config.get("scrcpy_hwdec", "auto")
                    self.scrcpy_codec = config.get("scrcpy_codec", "h264")
                    self.scrcpy_bitrate = config.get("scrcpy_bitrate", "")
                    self.scrcpy_video_buffer = config.get("scrcpy_video_buffer", "")
                    self.scrcpy_audio_buffer = config.get("scrcpy_audio_buffer", "")
        except Exception as e:
            print(f"Ошибка загрузки настроек подключения: {e}")
            self.connection_type = "wifi"
            self.adb_ip = "192.168.1.1"
            self.adb_port = "5555"
            self.scrcpy_fps = "60"

    def start_keyboard_listener(self):
        self.keyboard_thread = threading.Thread(target=self.keyboard_listener)
        self.keyboard_thread.daemon = True
        self.keyboard_thread.start()

    def start_scrcpy_output_thread(self):
        self.scrcpy_output_thread = threading.Thread(target=self.read_scrcpy_output)
        self.scrcpy_output_thread.daemon = True
        self.scrcpy_output_thread.start()

    def save_connection_settings(self, connection_type, adb_ip, adb_port, scrcpy_fps,
                                 scrcpy_hwdec=None, scrcpy_codec=None,
                                 scrcpy_bitrate=None, scrcpy_video_buffer=None,
                                 scrcpy_audio_buffer=None):
        try:
            if connection_type == "wifi":
                if not adb_ip or not adb_port:
                    raise ValueError("Для Wi-Fi подключения необходимо указать IP и порт")
                
                test_connect = subprocess.run(
                    ["adb", "connect", f"{adb_ip}:{adb_port}"], 
                    capture_output=True, 
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if "unable to connect" in test_connect.stdout.lower():
                    raise ConnectionError(f"Не удалось подключиться к {adb_ip}:{adb_port}")
                
            self.connection_type = connection_type
            self.adb_ip = adb_ip
            self.adb_port = adb_port
            self.scrcpy_fps = scrcpy_fps
            if scrcpy_hwdec:
                self.scrcpy_hwdec = scrcpy_hwdec
            if scrcpy_codec:
                self.scrcpy_codec = scrcpy_codec
            if scrcpy_bitrate is not None:
                self.scrcpy_bitrate = str(scrcpy_bitrate)
            if scrcpy_video_buffer is not None:
                self.scrcpy_video_buffer = str(scrcpy_video_buffer)
            if scrcpy_audio_buffer is not None:
                self.scrcpy_audio_buffer = str(scrcpy_audio_buffer)
            
            config = {}
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
            
            config.update({
                "connection_type": connection_type,
                "adb_ip": adb_ip,
                "adb_port": adb_port,
                "scrcpy_fps": scrcpy_fps,
                "scrcpy_hwdec": self.scrcpy_hwdec,
                "scrcpy_codec": self.scrcpy_codec,
                "scrcpy_bitrate": self.scrcpy_bitrate,
                "scrcpy_video_buffer": self.scrcpy_video_buffer,
                "scrcpy_audio_buffer": self.scrcpy_audio_buffer
            })
            
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
                
            self.log_message("✅ Настройки подключения сохранены!", 'success')
            self.restart_scrcpy()
            return True
            
        except Exception as e:
            self.log_message(f"❌ Ошибка сохранения настроек: {str(e)}", 'error')
            return False

    def load_coords(self):
        default_coords = {
            "pause": "630 450",
            "like": "630 1020",
            "pin": "630 1180",
            "scroll_up": "630 1120",
            "scroll_down": "630 1120"
        }

        def apply(coords):
            self.pause_coords = coords.get("pause", default_coords["pause"])
            self.like_coords = coords.get("like", default_coords["like"])
            self.pin_coords = coords.get("pin", default_coords["pin"])
            self.scroll_up_coords = coords.get("scroll_up", default_coords["scroll_up"])
            self.scroll_down_coords = coords.get("scroll_down", default_coords["scroll_down"])

        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                # Приоритет: настройки конкретного устройства -> глобальные -> дефолт
                serial = self.device_serial
                if serial:
                    dev_coords = config.get("coords_by_device", {}).get(serial)
                    if dev_coords:
                        apply(dev_coords)
                        return
                # Обратная совместимость со старым форматом (единый блок координат)
                if any(k in ("pause", "like", "pin") for k in config):
                    apply(config)
                    return
                apply(default_coords)
            else:
                apply(default_coords)

        except Exception as e:
            print(f"Ошибка загрузки конфигурации: {e}")
            apply(default_coords)

    def save_coords_to_file(self):
        try:
            config = {}
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)

            config.setdefault("coords_by_device", {})
            dev_coords = {
                "pause": self.pause_coords,
                "like": self.like_coords,
                "pin": self.pin_coords
            }

            if self.device_serial:
                config["coords_by_device"][self.device_serial] = dev_coords
                self.log_message(f"💾 Координаты сохранены для устройства {self.device_serial}", 'success')
            else:
                config["coords_by_device"]["<default>"] = dev_coords
                self.log_message("💾 Координаты сохранены (устройство не определено → <default>)", 'info')

            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
            return True
        except Exception as e:
            print(f"Ошибка сохранения конфигурации: {e}")
            return False

    def get_coords_by_name(self, name):
        return getattr(self, name + "_coords", "0 0")

    def coords_for_overlay(self, name):
        # Координаты маркеров оверлея берём НАПРЯМУЮ из текущих полей
        # контроллера — это живой preview до сохранения в файл.
        val = getattr(self, name + "_coords", None)
        if val:
            return str(val)
        default_coords = {
            "pause": "630 450",
            "like": "630 1020",
            "pin": "630 1180",
            "scroll_up": "630 1120",
            "scroll_down": "630 1120"
        }
        return default_coords.get(name, "0 0")

    def set_pointer_location(self, visible):
        value = "1" if visible else "0"
        self.adb_command(f"settings put system pointer_location {value}")

    def adb_command(self, command):
        try:
            if self.connection_type == "wifi":
                subprocess.run(["adb", "connect", f"{self.adb_ip}:{self.adb_port}"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            
            subprocess.run(["adb", "shell"] + command.split(), check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        except subprocess.CalledProcessError as e:
            print(f"Не удалось выполнить команду: {e}")
            return False
        except FileNotFoundError:
            print("ADB не найден. Убедитесь, что Android Debug Bridge установлен и добавлен в PATH.")
            return False

    def adb_screen(self):
        if not self._has_usb_device():
            self.log_message("⚠️ Устройство не подключено — скриншот недоступен", 'info')
            return False
        try:
            if self.connection_type == "wifi":
                subprocess.run(["adb", "connect", f"{self.adb_ip}:{self.adb_port}"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            result = subprocess.run(
                ["adb", "exec-out", "screencap", "-p"],
                capture_output=True,
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            with open(_internal_path("screen.png"), "wb") as f:
                f.write(result.stdout)
            return True
        except subprocess.CalledProcessError as e:
            self.log_message(f"Не удалось выполнить команду: {e}", 'error')
            print(f"Не удалось выполнить команду: {e}")
            return False
        except FileNotFoundError:
            print("ADB не найден. Убедитесь, что Android Debug Bridge установлен и добавлен в PATH.")
            return False
    
    def log_message(self, message, tag='info'):
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        log_entry = {
            'timestamp': timestamp,
            'message': message,
            'tag': tag
        }
        self.log_messages.append(log_entry)
        if len(self.log_messages) > 1000:
            self.log_messages.pop(0)
    
    def save_coords(self, pause_x, pause_y, like_x, like_y, pin_x, pin_y):
        self.pause_coords = f'{pause_x} {pause_y}'
        self.like_coords = f'{like_x} {like_y}'
        self.pin_coords = f'{pin_x} {pin_y}'

        if self.save_coords_to_file():
            self.adb_screen()
            self.log_message("✅ Координаты сохранены!", 'success')
            return True
        else:
            self.log_message("❌ Не удалось сохранить координаты в файл", 'error')
            return False

    def preview_coords(self, pause_x, pause_y, like_x, like_y, pin_x, pin_y):
        """Живой предпросмотр: обновляет только in-memory координаты,
        чтобы оверлей маркеров сразу переместился ДО сохранения в файл.
        Сохранение в config.json выполняется лишь кнопкой «Сохранить координаты»."""
        try:
            self.pause_coords = f'{int(pause_x)} {int(pause_y)}'
            self.like_coords = f'{int(like_x)} {int(like_y)}'
            self.pin_coords = f'{int(pin_x)} {int(pin_y)}'
            return True
        except Exception:
            return False

    def reset_coords(self):
        """Отменяет несохранённые изменения: перечитывает сохранённые
        координаты из config.json в in-memory поля. Оверлей маркеров,
        считывающий поля каждый тик, сразу вернёт маркеры на место."""
        self.load_coords()
        return True

    def save_texture_info(self, texture_info):
        """Сохраняет информацию о текстуре в texture.json"""
        try:
            texture_data = {
                "texture": texture_info,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            }
            
            with open(_app_path("texture.json"), "w") as f:
                json.dump(texture_data, f, indent=4)
            
            self.log_message(f"✅ Информация о текстуре сохранена: {texture_info}", 'success')
            return True
        except Exception as e:
            self.log_message(f"❌ Ошибка сохранения информации о текстуре: {str(e)}", 'error')
            return False

    def _app_window_active(self):
        """Активно ли именно главное Tk-окно приложения (не scrcpy, не чужое)."""
        hwnd = GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def perform_action(self, action):
        # Бонды работают, когда активно окно scrcpy ИЛИ главное Tk-окно.
        # Игнорирование по вкладкам («Клавиатура»/«Настройки») действует
        # ТОЛЬКО пока активно окно приложения. Если активно окно scrcpy —
        # бонды работают всегда.
        if active_is_scrcpy_or_app():
            if self._app_window_active() and self.current_tab in ("hotkeys", "settings"):
                return False
            actions = {
                "pause": self.pause_action,
                "like": self.like_action,
                "pin": self.pin_action,
                "scroll_down": self.scroll_down_action,
                "scroll_up": self.scroll_up_action,
                "vol_plus": self.vol_plus_action,
                "vol_min": self.vol_min_action
            }
            action_func = actions.get(action)
            if action_func:
                result = action_func()
                # подсветить соответствующий маркер при вспышке
                if result and _overlay is not None and action in MarkerOverlay.MARKER_GEOM:
                    _overlay.flash_now(action)
                return result
        return False

    def pause_action(self):
        self.log_message("⏯️ Пауза/плей", 'pause')
        return self.adb_command(f"input tap {self.pause_coords}")
    
    def like_action(self):
        self.log_message("❤ Лайк", 'like')
        return self.adb_command(f"input tap {self.like_coords}")
    
    def pin_action(self):
        self.log_message("⭐ Избранное", 'pin')
        return self.adb_command(f"input tap {self.pin_coords}")
    
    def scroll_up_action(self):
        self.log_message("⏩ Следующее видео", 'scroll')
        return self.adb_command("input swipe 500 1000 500 500 100")
    
    def scroll_down_action(self):
        self.log_message("⏪ Предыдущее видео", 'scroll')
        return self.adb_command("input swipe 500 500 500 1000 100")

    def vol_plus_action(self):
        self.log_message("🔊 Громкость ➕", 'vol')
        return self.adb_command("input keyevent KEYCODE_VOLUME_UP")

    def vol_min_action(self):
        self.log_message("🔈 Громкость ➖", 'vol')
        return self.adb_command("input keyevent KEYCODE_VOLUME_DOWN")

    def home_action(self):
        return self.adb_command("input keyevent KEYCODE_HOME")

    def back_action(self):
        return self.adb_command("input keyevent KEYCODE_BACK")
        
    def recent_action(self):
        return self.adb_command("input keyevent KEYCODE_APP_SWITCH")

    def adb_shell_text(self, command):
        """Выполняет shell-команду и возвращает stdout (str) или None при ошибке."""
        try:
            if self.connection_type == "wifi":
                subprocess.run([self.adb, "connect", f"{self.adb_ip}:{self.adb_port}"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            r = subprocess.run(["adb", "shell"] + command.split(),
                               capture_output=True, text=True, check=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            return r.stdout
        except Exception:
            return None

    def open_app(self, package):
        """Открывает приложение по имени пакета, определяя реальную launch-activity."""
        installed = self.adb_shell_text("pm list packages")
        if installed is None:
            self.log_message("Не удалось получить список пакетов", 'error')
            return False
        if package == "com.zhiliaoapp.musically.go":
            candidates = ["com.zhiliaoapp.musically.go"]
        elif package == "com.zhiliaoapp.musically":
            candidates = ["com.zhiliaoapp.musically", "com.ss.android.ugc.trill"]
        else:
            candidates = [package]
        found = next((p for p in candidates if ("package:" + p) in installed), None)
        if not found:
            self.log_message(f"Пакет не установлен: {package}", 'error')
            return False
        # определяем launch-activity через resolve-activity
        res = self.adb_shell_text(f'cmd package resolve-activity --brief {found}')
        if res:
            line = res.strip().splitlines()
            if line:
                last = line[-1].strip()
                if '/' in last and 'No Activity' not in last:
                    if self.adb_command(f"am start -n {last}"):
                        return True
        # запасной вариант — monkey
        return self.adb_command(f"monkey -p {found} -c android.intent.category.LAUNCHER 1")

    def open_tiktok(self):
        self.log_message("📱 Открытие TikTok", 'scroll')
        return self.open_app("com.zhiliaoapp.musically")

    def open_tiktok_lite(self):
        self.log_message("📱 Открытие TikTok Lite", 'scroll')
        return self.open_app("com.zhiliaoapp.musically.go")

    def _load_keypad_scans(self):
        """Набор физических скан-кодов клавиш правой цифровой клавиатуры."""
        try:
            from keyboard import _winkeyboard as _wkb
            return sorted(set(t[0] for t in _wkb.keypad_keys))
        except Exception:
            return []

    def _scancodes_for(self, key):
        """Возвращает список кортежей (scan_code, is_keypad), которым физически
        соответствует hotkey-привязка. Передает numpad и верхний ряд как РАЗНЫЕ
        клавиши: «num 5» — это только правая цифровая клавиатура (is_keypad=True),
        «5» — только верхний ряд цифр (is_keypad=False)."""
        try:
            codes = keyboard.key_to_scan_codes(key)
        except Exception:
            codes = ()
        if not codes:
            return []
        name = key.strip().lower()
        is_num = name.startswith('num ') or name == 'num'
        if is_num:
            kp = self._keypad_scans
            num = [c for c in codes if c in kp]
            if num:
                return [(num[-1], True)]
            return [(codes[-1], True)]
        return [(codes[0], False)]

    def _build_hotkey_scan_map(self):
        """Перестраивает карту action -> [(scan_code, is_keypad)] из текущих hotkeys."""
        self.hotkey_scan_map = {}
        for action, key in self.hotkeys.items():
            self.hotkey_scan_map[action] = self._scancodes_for(key)

    def _on_key_event(self, event):
        """Событийный обработчик клавиш: определяет действие по физической
        клавише (scan_code + is_keypad) и ставит его в очередь. Все нажатия
        попадают в очередь сразу — выполнение по очереди делает отдельный поток.

        Срабатывание ТОЛЬКО один раз на нажатие: повторные KEY_DOWN от
        авто-повтора клавиатуры (при удержании клавиши) игнорируются до тех
        пор, пока клавиша не будет отпущена (KEY_UP)."""
        if not self.keyboard_listener_active:
            return
        try:
            hit = (event.scan_code, event.is_keypad)
            if event.event_type == keyboard.KEY_DOWN:
                if hit in self._pressed_keys:
                    return
                self._pressed_keys.add(hit)
                for action, pairs in self.hotkey_scan_map.items():
                    if hit in pairs:
                        self.hotkey_queue.put(action)
                        break
            elif event.event_type == keyboard.KEY_UP:
                self._pressed_keys.discard(hit)
        except Exception as e:
            self.log_message(f"Ошибка в обработчике клавиш: {e}", 'error')

    def keyboard_listener(self):
        time.sleep(3)
        self.log_message("✅ Служба запущена!", 'success')

        while self.keyboard_listener_active:
            try:
                action = self.hotkey_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.perform_action(action)
            except Exception as e:
                self.log_message(f"Ошибка в keyboard_listener: {e}", 'error')

    def read_scrcpy_output(self):
        while hasattr(self, 'scrcpy_process') and self.scrcpy_process:
            output = self.scrcpy_process.stdout.readline() # type: ignore
            if output:
                output_line = output.strip()
                self.log_message(output_line, 'info')
                
                # Сохраняем информацию о текстуре
                if "INFO: Texture:" in output_line:
                    texture_info = output_line.split("INFO: Texture:")[1].strip()
                    self.save_texture_info(texture_info)
                    
            time.sleep(0.1)

    def start_device_monitor(self):
        """Периодически проверяет список подключённых устройств adb и, если
        текущее (и/или единственное) устройство сменилось, повторно определяет
        его серийник и загружает соответствующие ему координаты. scrcpy при
        смене устройства НЕ перезапускается — он стартует только при запуске
        программы (если устройство уже подключено) или по команде пользователя."""
        self._device_monitor_active = True
        t = threading.Thread(target=self._device_monitor_loop, daemon=True)
        t.start()

    def _device_monitor_loop(self):
        if not self._device_monitor_active:
            return
        # ждём, чтобы adb успели подняться при первом старте
        time.sleep(2)
        while self._device_monitor_active:
            try:
                new_serial = self._current_usb_serial()
                if new_serial is not None and new_serial != self.device_serial:
                    old = self.device_serial
                    self.device_serial = new_serial
                    self.device_name = self.detect_device_name()
                    self.log_message(
                        f"🔄 Подключено устройство {self.device_name or self.device_serial}",
                        'restart')
                    # пересчитываем координаты под новый серийник
                    self.load_coords()
                    self.set_scrcpy_window_title()
                elif new_serial is None and self.device_serial is not None:
                    # устройство отключили
                    self.device_serial = None
                    self.device_name = None
                    self.log_message("⚠️ Устройство отключено", 'info')
            except Exception as e:
                print(f"Ошибка мониторинга устройства: {e}")
            time.sleep(2)

    def _current_usb_serial(self):
        """Возвращает серийник единственного подключённого USB-устройства
        или None, если их несколько/нет/команда недоступна. Для Wi-Fi-режима
        серийник не переопределяем, чтобы не менять координаты."""
        try:
            if self.connection_type == "wifi":
                # в wifi-режиме возвращаем текущий серийник, чтобы не
                # сработала ложная ветка «Устройство отключено»
                return self.device_serial
            r = subprocess.run(["adb", "devices", "-l"],
                               capture_output=True, text=True,
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               timeout=10)
            serials = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("List") or "device" not in line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                # строки вида: <serial> device product:... model:...
                if len(parts) >= 2 and parts[1] == "device":
                    serials.append(parts[0])
            if len(serials) == 1:
                return serials[0]
            return None
        except Exception:
            return None

    def _sync_device_coords(self):
        """Подтягивает сохранённые координаты устройства. Если для текущего
        серийника ещё нет своего блока — переносит в него текущие значения,
        чтобы у каждого устройства были собственные координаты."""
        try:
            if not self.device_serial:
                return
            config = {}
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
            config.setdefault("coords_by_device", {})
            bydev = config["coords_by_device"]
            if self.device_serial in bydev:
                return
            # нет блока для этого серийника — сохраняем текущие координаты
            bydev[self.device_serial] = {
                "pause": self.pause_coords,
                "like": self.like_coords,
                "pin": self.pin_coords,
            }
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
            self.log_message(
                f"💾 Текущие координаты привязаны к устройству "
                f"{self.device_serial}", 'success')
        except Exception as e:
            print(f"Ошибка сохранения координат устройства: {e}")

    def _scrcpy_base_args(self, extra=()):
        """Формирует базовый список аргументов scrcpy со всеми настройками."""
        args = [f"--max-fps={self.scrcpy_fps}", "--stay-awake", "--turn-screen-off",
                "--no-mouse-hover", "--keep-active", "--no-window-aspect-ratio-lock",
                "--disable-screensaver", "--background-color=#000",
                f"--hwdec={self.scrcpy_hwdec}",
                f"--video-codec={self.scrcpy_codec}"]
        return args + list(extra)

    def start_scrcpy(self):
        try:
            if self.scrcpy_process and self.scrcpy_process.poll() is None:
                self.log_message("🔄 Scrcpy уже запущен", 'restart')
                return

            if self.connection_type == "usb" and not self._has_usb_device():
                self.log_message("⚠️ Устройство не подключено — scrcpy не запущен", 'info')
                self.scrcpy_pid = None
                return

            if self.connection_type == "usb":
                subprocess.run(["adb", "disconnect"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                command = ["scrcpy"] + self._scrcpy_base_args()
                self.log_message("🔄 Запуск scrcpy (USB)...", 'restart')
            else:
                subprocess.run(["adb", "connect", f"{self.adb_ip}:{self.adb_port}"], check=True,
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                command = ["scrcpy"] + self._scrcpy_base_args([f"--tcpip={self.adb_ip}:{self.adb_port}"])
                self.log_message(f"🔄 Запуск scrcpy (Wi-Fi {self.adb_ip}:{self.adb_port})...", 'restart')
            self.adb_screen()
            self.scrcpy_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            try:
                self.scrcpy_pid = self.scrcpy_process.pid
            except Exception:
                self.scrcpy_pid = None
        except FileNotFoundError:
            self.log_message("scrcpy не найден. Убедитесь, что он установлен и добавлен в PATH.", 'error')
        except Exception as e:
            self.log_message(f"Ошибка запуска scrcpy: {str(e)}", 'error')

    def restart_scrcpy(self):
        self.log_message(f"🔄 Попытка перезапуска scrcpy ({self.connection_type})...", 'restart')
        if self.scrcpy_process:
            self.scrcpy_process.terminate()
            try:
                self.scrcpy_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.scrcpy_process.kill()
        self.scrcpy_pid = None
        
        if self.connection_type == "wifi":
            subprocess.run(["adb", "connect", f"{self.adb_ip}:{self.adb_port}"], check=True,
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.detect_device_serial()
        self.load_coords()
        self.start_scrcpy()
        return True

    def cleanup(self):
        self.keyboard_listener_active = False
        self._device_monitor_active = False
        if hasattr(self, 'kb_hook') and self.kb_hook:
            try:
                keyboard.unhook(self.kb_hook)
            except Exception as e:
                print(f"Ошибка снятия перехвата клавиш: {e}")
        kill_scrcpy_by_pid()
        self.scrcpy_pid = None
        if hasattr(self, 'scrcpy_process') and self.scrcpy_process:
            self.scrcpy_process.terminate()

def get_active_process_name():
    hwnd = GetForegroundWindow()
    pid = wintypes.DWORD()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process = psutil.Process(pid.value)
        return process.name()
    except psutil.NoSuchProcess:
        return None

controller = AndroidController()
_overlay = None

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    scrcpy_running = controller.scrcpy_process and controller.scrcpy_process.poll() is None
    # По умолчанию выключаем отображение указателя
    controller.set_pointer_location(False)
    
    # Получаем координаты для отображения в центре окна
    pause_x, pause_y = controller.pause_coords.split()
    like_x, like_y = controller.like_coords.split()
    pin_x, pin_y = controller.pin_coords.split()
    
    # Рассчитываем относительные координаты для отображения в центре
    # Предполагаем стандартное разрешение экрана 1260x2240
    screen_width = 1260
    screen_height = 2240
    
    # Центрируем координаты относительно центра окна
    center_x = 400  # центр окна приложения
    center_y = 270  # центр окна приложения
    
    # Вычисляем относительные координаты
    pause_rel_x = center_x + (int(pause_x) - screen_width // 2) // 3
    pause_rel_y = center_y + (int(pause_y) - screen_height // 2) // 3
    like_rel_x = center_x + (int(like_x) - screen_width // 2) // 3
    like_rel_y = center_y + (int(like_y) - screen_height // 2) // 3
    pin_rel_x = center_x + (int(pin_x) - screen_width // 2) // 3
    pin_rel_y = center_y + (int(pin_y) - screen_height // 2) // 3
    
    return _jinja(request, 'index.html', 
        app_version=APP_VERSION,
        pause_coords=controller.pause_coords.split(),
        like_coords=controller.like_coords.split(),
        pin_coords=controller.pin_coords.split(),
        pause_rel_coords=[pause_rel_x, pause_rel_y],
        like_rel_coords=[like_rel_x, like_rel_y],
        pin_rel_coords=[pin_rel_x, pin_rel_y],
        connection_type=controller.connection_type,
        logs=controller.log_messages,
        scrcpy_running=scrcpy_running,
        adb_ip=controller.adb_ip,
        adb_port=controller.adb_port,
        scrcpy_fps=controller.scrcpy_fps,
        scrcpy_hwdec=controller.scrcpy_hwdec,
        scrcpy_codec=controller.scrcpy_codec,
        scrcpy_bitrate=controller.scrcpy_bitrate,
        scrcpy_video_buffer=controller.scrcpy_video_buffer,
        scrcpy_audio_buffer=controller.scrcpy_audio_buffer,
        hotkeys=controller.hotkeys)

@app.post("/tab_changed")
async def tab_changed(request: Request):
    data = await request.json()
    tab = data.get('tab')
    controller.current_tab = tab
    return {"success": True}

@app.post("/action")
async def handle_action(request: Request):
    data = await request.json()
    action = data.get('action')

    if action == "update_hotkey":
        success = controller.update_hotkey(
            data.get('action_name'),
            data.get('key')
        )
    elif action == "save_connection_settings":
        success = controller.save_connection_settings(
            data.get('connection_type'),
            data.get('adb_ip'),
            data.get('adb_port'),
            data.get('scrcpy_fps'),
            data.get('scrcpy_hwdec'),
            data.get('scrcpy_codec'),
            data.get('scrcpy_bitrate'),
            data.get('scrcpy_video_buffer'),
            data.get('scrcpy_audio_buffer')
        )
    elif action == "save_coords":
        success = controller.save_coords(
            data.get('pause_x'),
            data.get('pause_y'),
            data.get('like_x'),
            data.get('like_y'),
            data.get('pin_x'),
            data.get('pin_y')
        )
    elif action in [
            "pause", 
            "like", 
            "pin", 
            "scroll_up", "scroll_down", 
            "vol_min", "vol_plus", "home", "back", "recent",
            "open_tiktok", "open_tiktok_lite",
            "restart_scrcpy",
        ]:
        if action == "vol_plus":
            success = controller.vol_plus_action()
        elif action == "vol_min":
            success = controller.vol_min_action()
        elif action == "restart_scrcpy":
            success = controller.restart_scrcpy()
        elif action == "pause":
            success = controller.pause_action()
        elif action == "like":
            success = controller.like_action()
        elif action == "pin":
            success = controller.pin_action()
        elif action == "scroll_down":
            success = controller.scroll_down_action()
        elif action == "scroll_up":
            success = controller.scroll_up_action()
        elif action == "home":
            success = controller.home_action()
        elif action == "back":
            success = controller.back_action()
        elif action == "recent":
            success = controller.recent_action()
        elif action == "open_tiktok":
            success = controller.open_tiktok()
        elif action == "open_tiktok_lite":
            success = controller.open_tiktok_lite()
        else:
            success = controller.perform_action(action)
    else:
        success = False

    # вспышка оверлея с подсветкой только нажатой кнопки (если она есть среди маркеров)
    if success and _overlay is not None and action in MarkerOverlay.MARKER_GEOM:
        _overlay.flash_now(action)
    
    return {
        'success': success,
        'logs': controller.log_messages,
        'scrcpy_running': controller.scrcpy_process and controller.scrcpy_process.poll() is None
    }

@app.get("/get_logs")
async def get_logs():
    return {
        'logs': controller.log_messages,
        'scrcpy_running': controller.scrcpy_process and controller.scrcpy_process.poll() is None
    }

@app.get("/get_coords")
async def get_coords():
    """Возвращает текущие координаты контроллера. Интерфейс опрашивает их,
    чтобы автоматически подхватывать координаты при смене подключённого
    устройства (без перезагрузки страницы)."""
    return {
        'pause': controller.pause_coords.split(),
        'like': controller.like_coords.split(),
        'pin': controller.pin_coords.split(),
        'device_serial': controller.device_serial
    }

@app.post("/preview_coords")
async def preview_coords(request: Request):
    """Живой предпросмотр координат оверлея: обновляет in-memory значения
    контроллера, не записывая их в config.json. Маркеры оверлея считывают
    координаты напрямую из этих полей каждый тик, поэтому сразу перемещаются.
    Сохранение в файл выполняет отдельный эндпоинт save_coords."""
    data = await request.json()
    success = controller.preview_coords(
        data.get('pause_x'), data.get('pause_y'),
        data.get('like_x'), data.get('like_y'),
        data.get('pin_x'), data.get('pin_y')
    )
    return {'success': success}

@app.post("/reset_coords")
async def reset_coords():
    """Отмена несохранённых правок: возвращает in-memory координаты
    контроллера к значениям из config.json. Вернёт актуальные координаты,
    чтобы форма и оверлей обновились."""
    success = controller.reset_coords()
    return {
        'success': success,
        'pause': controller.pause_coords.split(),
        'like': controller.like_coords.split(),
        'pin': controller.pin_coords.split(),
    }

# Маршрут для обслуживания файлов из текущей директории
@app.get("/files/{filename:path}")
async def serve_files(filename: str):
    # Безопасная проверка пути
    base_directory = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.normpath(os.path.join(base_directory, filename))
    
    # Проверяем, что файл находится в текущей директории (безопасность)
    try:
        if os.path.commonpath([base_directory, file_path]) != base_directory:
            return "Access denied", 403
    except ValueError:
        return "Access denied", 403
    
    if not os.path.exists(file_path):
        return "File not found", 404
        
    return FileResponse(file_path)

@app.post("/shutdown")
async def shutdown():
    controller.cleanup()
    # Принудительно завершаем процесс: весь поток завершится вместе с Tk-циклом.
    import os
    os._exit(0)

def find_free_port(start=1000, end=65535):
    """Возвращает свободный TCP-порт в диапазоне [start, end], или start,
    если подходящий не найден (чтобы запуск не падал)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        for p in tuple(range(start, end + 1)):
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
        return start
    finally:
        s.close()


def run_server(port):
    """Запускает FastAPI-приложение через uvicorn в фоновом потоке.
    Не печатать каждый HTTP-запрос в консоль — это главный источник спама."""
    import logging
    logging.getLogger('uvicorn.access').setLevel(logging.ERROR)
    logging.getLogger('uvicorn.error').setLevel(logging.ERROR)
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="error")

def wait_for_server(host, port, timeout=30):
    """Ждёт, пока сервер реально начнёт принимать соединения.
    Без этого WebView открывает страницу до поднятия сервера и получает
    ERR_CONNECTION_REFUSED (который wry не повторяет) — особенно в собранном exe."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False

if __name__ == "__main__":
    # Защита от повторного запуска: один именованный мьютекс на всю систему.
    # Если мьютекс уже создан — приложение уже работает, выходим.
    MUTEX_NAME = "Local\\ADB_TikTok_Controller_SingleInstance"
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not _mutex or kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        # Приложение уже запущено — тихо завершаемся без диалога.
        sys.exit(0)

    # Выбираем порт заранее: по умолчанию 1000, если занят — следующий свободный.
    SERVER_PORT = find_free_port(start=1000)

    server_thread = threading.Thread(target=run_server, args=(SERVER_PORT,), daemon=True)
    server_thread.start()

    # Создаем главное окно сразу — не ждём поднятия сервера.
    root = tk.Tk()

    # Ожидание сервера — в фоновом потоке, чтобы не задерживать окно.
    def _wait_server():
        if wait_for_server("127.0.0.1", SERVER_PORT):
            print(f"Сервер запущен на http://127.0.0.1:{SERVER_PORT}/")
        else:
            print("Ошибка: сервер не запустился за отведённое время.")
    threading.Thread(target=_wait_server, daemon=True).start()
    root.title(f"ADB TikTok Controller - v{APP_VERSION}")
    root.geometry("710x650")
    root.resizable(False, False)

    try:
        ico = _internal_path("app.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)
    except Exception:
        pass
    # Acrylic-прозрачность + резкие углы окна (как window-code.py):
    # backdrop=DWMSBT_TRANSIENTWINDOW (Acrylic — полупрозрачный),
    # corner=DWMWCP_DONOTROUND (без скругления), белый текст заголовка.
    try:
        apply_mica(root)
    except Exception as e:
        print(f"[mica] не удалось применить: {e}")
    # Создаем фрейм для WebView
    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True)
    # Встраиваем WebView с сайтом
    web = WebView(frame, url=f"http://127.0.0.1:{SERVER_PORT}/")

    # оверлей маркеров поверх окна scrcpy (того, что запустила эта программа)
    overlay = MarkerOverlay(root)
    _overlay = overlay
    overlay._last_tick = time.perf_counter()
    overlay.root.after(MarkerOverlay.TICK_MS, overlay._tick)

    # примагничивание окна scrcpy справа к главному окну, синхронно
    # по высоте; отключается, как только пользователь сам двигает/растягивает
    # окно scrcpy.
    docker = ScrcpyDocker(root)
    def _docker_tick():
        try:
            docker.update()
        except Exception:
            pass
        root.after(50, _docker_tick)
    root.after(300, _docker_tick)

    # актуальные размеры экрана телефона из свежего скриншота
    def refresh_phone_dims():
        p = _internal_path("screen.png")
        dims = png_dimensions(p)
        if dims:
            w, h = dims
            overlay.phone_w, overlay.phone_h = w, h
            if h > 0 and w > 0:
                docker.phone_aspect = h / w
        root.after(2000, refresh_phone_dims)
    refresh_phone_dims()

    root.mainloop()