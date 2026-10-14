from flask import Flask, render_template, request, jsonify, send_from_directory
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import keyboard
import psutil
import atexit

import tkinter as tk
from tkwry import WebView

APP_VERSION = "3.0"

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

app = Flask(__name__)

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
    Каждый маркер — отдельное top-level окно со СВОЕЙ альфой,
    что даёт независимое плавное появление/исчезновение каждого элемента.
    """
    MARKER_GEOM = {
        "pause": ("#6ad2ff", "ПАУЗА"),
        "like":  ("#fe2c55", "ЛАЙК"),
        "pin":   ("#face15", "ИЗБРАННОЕ"),
    }
    FADE_STEP = 60          # единиц альфы за тик (~30 FPS → ~0.4 с полный переход)
    FLASH_DURATION = 1     # секунд
    LOCAL_OVAL = (12, 32)   # позиция центра кружка внутри окна маркера
    TAIL_LEN = 22

    def __init__(self, root):
        self.root = root
        self.phone_w = 1260
        self.phone_h = 2240
        self.markers = {}                                  # name -> {win, canvas, widgets, w, h, visible}
        self.alphas = {n: 0 for n in self.MARKER_GEOM}     # независимая альфа каждого маркера
        self._flashes = {}                                 # {action: end_timestamp}

    # ---------- служебное ----------

    def _active_is_scrcpy_or_app(self):
        name = get_active_process_name()
        if name and str(name).lower() == 'scrcpy.exe':
            return True
        try:
            hwnd = GetForegroundWindow()
            if not hwnd:
                return False
            pid = wintypes.DWORD()
            GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                return True
        except Exception:
            pass
        return False

    def flash_now(self, action=None):
        """Вспышка на 1 секунду. Каждое нажатие живёт независимо."""
        now = time.time()
        if action is None:
            for n in self.MARKER_GEOM:
                self._flashes[n] = now + self.FLASH_DURATION
        else:
            self._flashes[action] = now + self.FLASH_DURATION

    def _top_hwnd(self, name):
        try:
            h = self.markers[name]['win'].winfo_id()
            for _ in range(4):
                p = user32.GetParent(h)
                if not p:
                    break
                h = p
            return h
        except Exception:
            return 0

    def _ensure_marker(self, name):
        """Создаёт отдельное окно под конкретный маркер (один раз)."""
        if name in self.markers:
            return
        color, label = self.MARKER_GEOM[name]

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=COLORKEY_HEX)
        win.withdraw()

        # Локальная геометрия маркера (относительно левого-верхнего угла окна)
        ox, oy = self.LOCAL_OVAL
        dx = self.TAIL_LEN * 0.866
        dy = -self.TAIL_LEN * 0.5
        text_w = 68 if label == "ИЗБРАННОЕ" else 48
        box_top = oy + dy - 9
        box_bot = oy + dy + 5

        win_w = int(ox + dx + text_w + 8)
        win_h = int(max(box_bot, oy + 4) + 6)

        canvas = tk.Canvas(win, width=win_w, height=win_h,
                           bg=COLORKEY_HEX, highlightthickness=0)
        canvas.pack(fill='both', expand=True)

        r = 4
        oval = canvas.create_oval(ox - r, oy - r, ox + r, oy + r,
                                  fill=color, outline=color)
        tail = canvas.create_line(ox, oy, ox + dx, oy + dy,
                                  fill='#000000', width=2)
        fx = ox + dx
        box = canvas.create_rectangle(fx, box_top, fx + text_w, box_bot,
                                      fill='#000000', outline='#000000')
        text = canvas.create_text(fx + text_w // 2, (box_top + box_bot) // 2,
                                  text=label, fill=color,
                                  font=('Consolas', 8, 'bold'))
        canvas.tag_raise(oval)

        self.markers[name] = {
            'win': win,
            'canvas': canvas,
            'widgets': {'oval': oval, 'tail': tail, 'box': box, 'text': text},
            'w': win_w,
            'h': win_h,
            'visible': False,
        }

    def _apply_click_through(self, name):
        """WS_EX_TRANSPARENT/TOOLWINDOW/NOACTIVATE + LAYERED — один раз при показе окна."""
        h = self._top_hwnd(name)
        if not h:
            return
        try:
            style = user32.GetWindowLongW(h, GWL_EXSTYLE)
            style |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
            user32.SetWindowLongW(h, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _apply_alpha(self, name, alpha):
        """Применяет альфу конкретного окна-маркера (colorkey + alpha)."""
        h = self._top_hwnd(name)
        if not h:
            return
        try:
            user32.SetLayeredWindowAttributes(
                h, COLORKEY,
                max(0, min(255, int(alpha))),
                LWA_COLORKEY | LWA_ALPHA
            )
        except Exception:
            pass

    # ---------- основной цикл ----------

    def update(self):
        # Гарантируем, что у каждого маркера есть своё окно
        for name in self.MARKER_GEOM:
            self._ensure_marker(name)

        # Окно именно нашего scrcpy (по сохранённому PID)
        scr_pid = controller.scrcpy_pid
        scr_hwnd = find_window_by_pid(scr_pid) if scr_pid else 0

        # Чистим истёкшие вспышки
        now = time.time()
        for n in list(self._flashes.keys()):
            if self._flashes[n] <= now:
                del self._flashes[n]

        flashing_markers = [n for n in self.MARKER_GEOM if n in self._flashes]
        settings_active = bool(
            scr_hwnd and controller.current_tab == "settings"
            and self._active_is_scrcpy_or_app()
        )

        # Независимый плавный переход альфы каждого маркера
        for name in self.MARKER_GEOM:
            target = 255 if (name in flashing_markers or settings_active) else 0
            cur = self.alphas[name]
            if cur < target:
                cur = min(target, cur + self.FADE_STEP)
            elif cur > target:
                cur = max(target, cur - self.FADE_STEP)
            self.alphas[name] = cur

        any_visible = any(a > 0 for a in self.alphas.values())

        # --- Геометрия «области телефона» ---
        if scr_hwnd:
            cref = wintypes.RECT()
            user32.GetClientRect(scr_hwnd, ctypes.byref(cref))
            w = cref.right - cref.left
            h = cref.bottom - cref.top
            if w <= 0 or h <= 0:
                return
            pt = wintypes.POINT(0, 0)
            user32.ClientToScreen(scr_hwnd, ctypes.byref(pt))
            scale = min(w / self.phone_w, h / self.phone_h)
            sw = int(self.phone_w * scale)
            sh = int(self.phone_h * scale)
            ox = pt.x + (w - sw) // 2
            oy = pt.y + (h - sh) // 2
        elif any_visible:
            # Нет scrcpy, но есть вспышка — показываем в правом нижнем углу экрана
            sw = int(self.phone_w * 0.35)
            sh = int(self.phone_h * 0.35)
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            ox = screen_w - sw - 40
            oy = screen_h - sh - 80
        else:
            # Всё скрыто — просто прячем окна-маркеры
            for name in self.MARKER_GEOM:
                m = self.markers[name]
                if m['visible']:
                    try:
                        m['win'].withdraw()
                    except Exception:
                        pass
                    m['visible'] = False
            return

        local_ox, local_oy = self.LOCAL_OVAL

        # --- Позиционируем и показываем/прячем каждый маркер независимо ---
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
                continue

            x, y = (int(v) for v in controller.coords_for_overlay(name).split())
            px = ox + x / self.phone_w * sw
            py = oy + y / self.phone_h * sh

            ww, wh = m['w'], m['h']
            wx = int(px - local_ox)
            wy = int(py - local_oy)
            m['win'].geometry(f"{ww}x{wh}+{wx}+{wy}")

            if not m['visible']:
                m['win'].deiconify()
                self._apply_click_through(name)
                m['visible'] = True

            self._apply_alpha(name, alpha)

    def _tick(self):
        try:
            self.update()
        except Exception:
            pass
        self.root.after(33, self._tick)

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
        self.hotkeys = {
            'pause': 'num 0',
            'like': 'num 5',
            'pin': 'num 2',
            'scroll_up': 'num 3',
            'scroll_down': 'num 1',
        }

        self.load_hotkeys()
        self.load_connection_settings()
        self.load_coords()
        self.start_scrcpy()
        
        self.keyboard_listener_active = True
        self.start_keyboard_listener()
        self.start_scrcpy_output_thread()

    def load_hotkeys(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    if 'hotkeys' in config:
                        self.hotkeys.update(config['hotkeys'])
        except Exception as e:
            print(f"Ошибка загрузки горячих клавиш: {e}")

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
            "pin": "630 1180"
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    coords = json.load(f)
                    self.pause_coords = coords.get("pause", default_coords["pause"])
                    self.like_coords = coords.get("like", default_coords["like"])
                    self.pin_coords = coords.get("pin", default_coords["pin"])
            else:
                self.pause_coords = default_coords["pause"]
                self.like_coords = default_coords["like"]
                self.pin_coords = default_coords["pin"]
                
        except Exception as e:
            print(f"Ошибка загрузки конфигурации: {e}")
            self.pause_coords = default_coords["pause"]
            self.like_coords = default_coords["like"]
            self.pin_coords = default_coords["pin"]
    
    def save_coords_to_file(self):
        try:
            config = {}
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
            
            config.update({
                "pause": self.pause_coords,
                "like": self.like_coords,
                "pin": self.pin_coords
            })
            
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
            return True
        except Exception as e:
            print(f"Ошибка сохранения конфигурации: {e}")
            return False

    def get_coords_by_name(self, name):
        return getattr(self, name + "_coords", "0 0")

    def coords_for_overlay(self, name):
        return getattr(self, name + "_coords", "0 0")

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
        if len(self.log_messages) > 50:
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

    def perform_action(self, action):
        if get_active_process_name() == "scrcpy.exe":
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
        self.load_coords()
        return self.adb_command(f"input tap {self.pause_coords}")
    
    def like_action(self):
        self.load_coords()
        self.log_message("❤ Лайк", 'like')
        return self.adb_command(f"input tap {self.like_coords}")
    
    def pin_action(self):
        self.load_coords()
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

    def keyboard_listener(self):
        time.sleep(3)
        self.log_message("✅ Служба запущена!", 'success')
        
        while self.keyboard_listener_active:
            try:
                for action, key in self.hotkeys.items():
                    if keyboard.is_pressed(key):
                        self.perform_action(action)
                        time.sleep(0.2)
                time.sleep(0.01)
            except Exception as e:
                self.log_message(f"Ошибка в keyboard_listener: {e}", 'error')

    def read_scrcpy_output(self):
        while hasattr(self, 'scrcpy_process') and self.scrcpy_process:
            output = self.scrcpy_process.stdout.readline() # type: ignore
            if output:
                output_line = output.strip()
                self.log_message(output_line, 'info')
                print(output_line)
                
                # Сохраняем информацию о текстуре
                if "INFO: Texture:" in output_line:
                    texture_info = output_line.split("INFO: Texture:")[1].strip()
                    self.save_texture_info(texture_info)
                    
            time.sleep(0.1)

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
        
        if self.connection_type == "usb":
            subprocess.run(["adb", "disconnect"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.run(["adb", "connect", f"{self.adb_ip}:{self.adb_port}"], check=True,
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.start_scrcpy()
        return True

    def cleanup(self):
        self.keyboard_listener_active = False
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

@app.route('/')
def index():
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
    
    return render_template('index.html', 
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

@app.route('/tab_changed', methods=['POST'])
def tab_changed():
    data = request.json
    tab = data.get('tab') # type: ignore
    controller.current_tab = tab
    return jsonify({'success': True})

@app.route('/action', methods=['POST'])
def handle_action():
    data = request.json
    action = data.get('action') # type: ignore

    if action == "update_hotkey":
        success = controller.update_hotkey(
            data.get('action_name'), # type: ignore
            data.get('key') # type: ignore
        )
    elif action == "save_connection_settings":
        success = controller.save_connection_settings(
            data.get('connection_type'), # type: ignore
            data.get('adb_ip'), # type: ignore
            data.get('adb_port'), # type: ignore
            data.get('scrcpy_fps'), # type: ignore
            data.get('scrcpy_hwdec'), # type: ignore
            data.get('scrcpy_codec'), # type: ignore
            data.get('scrcpy_bitrate'), # type: ignore
            data.get('scrcpy_video_buffer'), # type: ignore
            data.get('scrcpy_audio_buffer') # type: ignore
        )
    elif action == "save_coords":
        success = controller.save_coords(
            data.get('pause_x'), # type: ignore
            data.get('pause_y'), # type: ignore
            data.get('like_x'), # type: ignore
            data.get('like_y'), # type: ignore
            data.get('pin_x'), # type: ignore
            data.get('pin_y') # type: ignore
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
    
    return jsonify({
        'success': success,
        'logs': controller.log_messages[-22:],
        'scrcpy_running': controller.scrcpy_process and controller.scrcpy_process.poll() is None
    })

@app.route('/get_logs')
def get_logs():
    return jsonify({
        'logs': controller.log_messages[-22:],
        'scrcpy_running': controller.scrcpy_process and controller.scrcpy_process.poll() is None
    })

# Маршрут для обслуживания файлов из текущей директории
@app.route('/files/<path:filename>')
def serve_files(filename):
    # Безопасная проверка пути
    base_directory = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_directory, filename)
    
    # Проверяем, что файл находится в текущей директории (безопасность)
    if not os.path.commonpath([base_directory, file_path]) == base_directory:
        return "Access denied", 403
    
    if not os.path.exists(file_path):
        return "File not found", 404
        
    return send_from_directory(base_directory, filename)

@app.route('/shutdown', methods=['POST'])
def shutdown():
    controller.cleanup()
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()
    return 'Server shutting down...'

def run_flask():
    app.run(debug=False, port=56987)

def wait_for_server(host, port, timeout=30):
    """Ждёт, пока Flask реально начнёт принимать соединения.
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
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Ждём поднятия Flask-сервера, иначе WebView открывает страницу раньше
    # времени и wry показывает ERR_CONNECTION_REFUSED (без повторной попытки).
    if not wait_for_server("127.0.0.1", 56987):
        print("Ошибка: Flask-сервер не запустился за отведённое время.")
    else:
        print(f"Flask-сервер запущен на http://127.0.0.1:56987/")
    
    # window = webview.create_window(
    #     'Android Controller', 
    #     'http://127.0.0.1:56987/', 
    #     width=1000, height=580, resizable=False
    # )

    # Создаем главное окно
    root = tk.Tk()
    root.title(f"ADB TikTok Controller - v{APP_VERSION}")
    root.geometry("1000x580")
    root.resizable(False, False)

    try:
        ico = _internal_path("app.ico")
        if os.path.exists(ico):
            root.iconbitmap(ico)
    except Exception:
        pass
    # Создаем фрейм для WebView
    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True)
    # Встраиваем WebView с сайтом
    web = WebView(frame, url="http://127.0.0.1:56987/")

    # оверлей маркеров поверх окна scrcpy (того, что запустила эта программа)
    overlay = MarkerOverlay(root)
    _overlay = overlay
    overlay.root.after(33, overlay._tick)

    # актуальные размеры экрана телефона из свежего скриншота
    def refresh_phone_dims():
        p = _internal_path("screen.png")
        dims = png_dimensions(p)
        if dims:
            overlay.phone_w, overlay.phone_h = dims
        root.after(500, refresh_phone_dims)
    refresh_phone_dims()

    # Запускаем приложение
    
    def on_closed():
        controller.keyboard_listener_active = False
        # завершить ТОЛЬКО свой scrcpy по PID и его дерево
        kill_scrcpy_by_pid()
        controller.scrcpy_pid = None
        # закрыть консоль
        close_console()
        # выйти (sys.exit выполнит atexit-хук kill_scrcpy_by_pid)
        sys.exit(0)
    
    # window.events.closed += on_closed # type: ignore
    # webview.start()
    root.mainloop()