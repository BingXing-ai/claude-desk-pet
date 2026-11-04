# Claude 娘桌宠悬浮窗
# mod 把每个会话的状态写进 <运行目录>/sessions/<会话id>.json，这里读最新的那个来换表情和气泡。
# 用法：pythonw pet.pyw <运行目录>
import ctypes
import ctypes.wintypes as wt
import glob
import json
import math
import os
import socket
import subprocess
import sys
import time
import tkinter as tk
import winsound

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SOUNDS = os.path.join(os.path.dirname(HERE), 'sounds')
RUN_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.expanduser('~'), '.claude', 'desk-pet')
SESSIONS = os.path.join(RUN_DIR, 'sessions')
SETTINGS = os.path.join(RUN_DIR, 'settings.json')

# 只允许开一个桌宠：占住一个本地端口，占不到说明已经有一个在跑
try:
    _lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _lock.bind(('127.0.0.1', 47321))
except OSError:
    sys.exit(0)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# 状态 → 立绘
SPRITES = {
    'thinking': 'smile-closed',
    'tool': 'typing',
    'reading': 'notes',
    'waiting': 'knock',
    'celebrate': 'confetti',
    'idle': 'smile',
    'error': 'panic',
    'aborted': 'guilty',
    'stone': 'stone',
}
SIZES = {'小': 140, '中': 190, '大': 250}  # 角色高度（100% 缩放下的像素）
IDLE_AFTER = 10 * 60  # 等你回复超过 10 分钟就开始发呆
ORANGE = (232, 116, 59, 255)   # 气泡描边
CREAM = (255, 250, 243, 255)   # 气泡底色
INK = (58, 42, 26, 255)        # 气泡文字
ANIMATED = {'thinking', 'tool', 'reading'}  # 这些状态下，台词末尾的“…”会变成跳动的点点点
DOTS = '...'
SS = 4  # 气泡先按 4 倍大小画再缩小，圆角和尾巴的边缘更平滑
HOP = 14  # 换状态时跳起的高度
BOB = 3   # 平时上下晃动的最大幅度
GAP = 4   # 跳到最高时，头顶和气泡尖之间至少留这么多

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


# ---------- 分层窗口（逐像素透明） ----------
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 2
GA_ROOT = 2

# 64 位 Python 下句柄是指针大小，必须声明类型，否则会被截断成 32 位
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetWindowLongW.restype = wt.LONG
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.restype = wt.LONG
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, wt.LONG]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION),
                                       wt.DWORD]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE,
                                   wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]


class Layered:
    """用 UpdateLayeredWindow 把一张 RGBA 图贴到窗口上：每个像素有自己的透明度，边缘不会有锯齿和毛边，
    完全透明的地方鼠标还能直接点穿。"""

    def __init__(self, widget):
        self.widget = widget
        self.screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(self.screen)
        self.bits = ctypes.c_void_p()
        self.bmp = self.old = None
        self.size = (0, 0)

    def hwnd(self):
        # Tk 的顶层窗口是 winfo_id() 那个子窗口的外层；每次现取，防止 Tk 重建外层窗口后失效
        return user32.GetAncestor(self.widget.winfo_id(), GA_ROOT)

    def show(self, im):
        hwnd = self.hwnd()
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not ex & WS_EX_LAYERED:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        w, h = im.size
        if (w, h) != self.size:
            if self.bmp:
                gdi32.SelectObject(self.dc, self.old)
                gdi32.DeleteObject(self.bmp)
            bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # 高度取负 = 从上往下存
            self.bmp = gdi32.CreateDIBSection(self.screen, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
            self.old = gdi32.SelectObject(self.dc, self.bmp)
            self.size = (w, h)
        data = im.tobytes('raw', 'BGRa')  # Windows 要的是 BGRA 顺序、颜色预乘透明度
        ctypes.memmove(self.bits.value, data, len(data))
        user32.UpdateLayeredWindow(hwnd, self.screen, None, ctypes.byref(wt.SIZE(w, h)), self.dc,
                                   ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(BLENDFUNCTION(0, 0, 255, 1)),
                                   ULW_ALPHA)


# ---------- 显示器 ----------
class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT), ('dwFlags', wt.DWORD)]


user32.MonitorFromPoint.restype = wt.HMONITOR
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.restype = wt.BOOL
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]


def monitor_at(x, y):
    """返回 (x, y) 所在显示器（不在任何显示器上就取最近的那个）的 (整个屏幕, 去掉任务栏的工作区)，
    都是 (左, 上, 右, 下)。"""
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    mon = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        m, w = info.rcMonitor, info.rcWork
        return (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)
    full = (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
    return full, full


# ---------- Claude 窗口 ----------
def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ''
    finally:
        kernel32.CloseHandle(h)


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def claude_is_foreground():
    hwnd = user32.GetForegroundWindow()
    return bool(hwnd) and process_name(window_pid(hwnd)) == 'claude.exe'


def find_claude_window():
    found = []
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 \
                and process_name(window_pid(hwnd)) == 'claude.exe':
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(PROC(cb), 0)
    return found[0] if found else None


def claude_running():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq claude.exe', '/NH'], capture_output=True,
                             text=True, timeout=10, creationflags=0x08000000).stdout
        return 'claude.exe' in out.lower()
    except Exception:
        return True


# ---------- 设置和状态文件 ----------
def load_settings():
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(SETTINGS, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception:
        pass


def read_latest():
    """读所有会话里最新的状态；顺手清掉两天前的旧文件。"""
    best, now = None, time.time()
    for p in glob.glob(os.path.join(SESSIONS, '*.json')):
        try:
            mtime = os.path.getmtime(p)
            if now - mtime > 2 * 86400:
                os.remove(p)
                continue
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            if best is None or mtime > best[0]:
                best = (mtime, data)
        except Exception:
            continue
    return best


# ---------- 画图 ----------
def prepare(path, height):
    """缩放立绘。先换成预乘透明度再缩放，半透明的发丝边缘不会混进透明像素里的杂色。"""
    im = Image.open(path).convert('RGBA')
    w = round(im.width * height / im.height)
    return im.convert('RGBa').resize((w, height), Image.LANCZOS).convert('RGBA')


def load_font(px):
    fonts = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    for name, index in (('msyh.ttc', 1), ('msyh.ttc', 0), ('msyh.ttf', 0), ('simhei.ttf', 0)):
        try:
            return ImageFont.truetype(os.path.join(fonts, name), px, index=index)
        except OSError:
            continue
    try:
        return ImageFont.load_default(px)
    except TypeError:  # Pillow 10.1 以前不支持指定大小
        return ImageFont.load_default()


def fit_text(text, max_w, measure):
    """文字太长就从中间省略，保留开头（“在改”）和结尾（扩展名）。"""
    if measure(text) <= max_w:
        return text
    head, tail = len(text) // 2, len(text) - len(text) // 2
    while head + tail > 1:
        if head >= tail:
            head -= 1
        else:
            tail -= 1
        cut = text[:head] + '…' + text[len(text) - tail:]
        if measure(cut) <= max_w:
            return cut
    return '…'


class Pet:
    def __init__(self):
        self.settings = load_settings()
        self.root = tk.Tk()
        self.root.title('Claude 桌宠')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.scale = self.root.winfo_fpixels('1i') / 96

        # 画面全部由 Layered 贴上去，这个画布只用来接收鼠标事件
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.font = ('Microsoft YaHei UI', 10)  # 右键菜单用

        self.state, self.text, self.project = 'waiting', '我来啦～', ''
        self.state_since = time.time()
        self.last_mtime = 0
        self.last_mtime_seen = 0
        self.hop_at = time.time()
        self.hidden = False
        self.drag = None
        self.last_alive = time.time()
        self.last_frame = None

        self.build()
        self.place_window()
        self.make_menu()
        self.root.update_idletasks()
        self.layer = Layered(self.root)

        self.canvas.bind('<ButtonPress-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.canvas.bind('<Double-Button-1>', self.on_double)
        self.canvas.bind('<Button-3>', lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.poll()
        self.animate()
        self.watch_focus()
        self.watch_claude()

    # ---------- 布局 ----------
    def build(self):
        s = self.scale
        h = round(SIZES.get(self.settings.get('size', '中'), 190) * s)
        self.sprites = {state: prepare(os.path.join(HERE, 'sprites', name + '.png'), h)
                        for state, name in SPRITES.items()}
        self.char_h = h
        self.char_w = max(im.width for im in self.sprites.values())
        self.W = max(self.char_w, round(190 * s))
        # 气泡在最上面，下面给跳跃和晃动留够空间，角色怎么动都碰不到气泡，也不会出窗口底边
        self.bubble_bottom = round(38 * s)
        self.tail_tip = self.bubble_bottom + round(10 * s)
        self.rest_y = self.tail_tip + round((GAP + HOP + BOB) * s)  # 静止时立绘顶边的位置
        self.H = self.rest_y + h + round(BOB * s)
        self.pfont = load_font(round(10 * 96 / 72 * s))  # 10 磅
        self.canvas.config(width=self.W, height=self.H)
        self.draw_bubble()

    def draw_bubble(self):
        """按当前台词排好气泡：算出文字和气泡的位置，画好不带文字的气泡底图。"""
        s, measure = self.scale, self.pfont.getlength
        pad = round(10 * s)
        max_w = self.W - 2 * pad - 4
        state = self.effective_state()
        text, self.dots_base = self.display_text(), None
        if state in ANIMATED and (text.endswith('…') or text == '思考中'):
            # 末尾的省略号换成一跳一跳的点点点；文字左对齐，按三个点的宽度留好位置，点数变化时文字不会左右晃
            self.dots_base = fit_text(text.rstrip('…'), max_w - measure(DOTS), measure)
            text = self.dots_base + DOTS
        else:
            text = fit_text(text, max_w, measure)
        text_w = measure(text)
        self.bubble_state = state
        self.bubble_text = text
        self.text_x = self.W / 2 - text_w / 2
        x0 = max(1, math.floor(self.text_x) - pad)
        x1 = min(self.W - 2, math.ceil(self.text_x + text_w) + pad)
        self.bubble_bg = self.bubble_image(x0, x1)
        self.bubble_cache = {}

    def bubble_image(self, x0, x1):
        s = self.scale
        y0, y1, tip = 2, self.bubble_bottom, self.tail_tip
        lw = max(2, round(2 * s))
        r = round(12 * s)
        cx, half = self.W / 2, 6 * s
        big = Image.new('RGBA', (self.W * SS, (tip + 2) * SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)

        def k(*pts):
            return [(x * SS, y * SS) for x, y in pts]

        # 尾巴是个倒三角：先画橙色的外轮廓，再在里面画一个往里缩了一圈描边宽度的奶油色三角
        slope = half / (tip - y1)                              # 尾巴斜边每往下 1 像素，往里收多少
        inner_tip = tip - lw * math.hypot(1, slope) / slope    # 三角往里缩 lw 后，尖端上移的距离
        top = y1 - lw                                          # 内三角的上边藏进气泡里，和底色连成一片
        d.rounded_rectangle(k((x0, y0), (x1, y1)), radius=r * SS, fill=ORANGE)
        d.polygon(k((cx - half - lw * slope, y1 - lw), (cx + half + lw * slope, y1 - lw), (cx, tip)), fill=ORANGE)
        d.rounded_rectangle(k((x0 + lw, y0 + lw), (x1 - lw, y1 - lw)), radius=(r - lw) * SS, fill=CREAM)
        d.polygon(k((cx - (inner_tip - top) * slope, top), (cx + (inner_tip - top) * slope, top), (cx, inner_tip)),
                  fill=CREAM)
        return big.convert('RGBa').resize((self.W, tip + 2), Image.BOX).convert('RGBA')

    def bubble_with(self, text):
        im = self.bubble_cache.get(text)
        if im is None:
            im = self.bubble_bg.copy()
            ImageDraw.Draw(im).text((self.text_x, round(20 * self.scale)), text, font=self.pfont, fill=INK,
                                    anchor='lm')
            self.bubble_cache[text] = im
        return im

    def render(self, state, y, text):
        key = (state, y, text, id(self.bubble_bg))
        if key == self.last_frame:
            return
        self.last_frame = key
        frame = Image.new('RGBA', (self.W, self.H), (0, 0, 0, 0))
        sprite = self.sprites[state]
        frame.alpha_composite(sprite, (self.W // 2 - sprite.width // 2, max(0, y)))
        frame.alpha_composite(self.bubble_with(text), (0, 0))
        self.layer.show(frame)

    def display_text(self):
        if self.effective_state() == 'idle':
            return '在发呆～ zZ'
        return self.text

    def effective_state(self):
        if self.state == 'waiting' and time.time() - self.state_since > IDLE_AFTER:
            return 'idle'
        return self.state

    def place_window(self, x=None, y=None):
        if x is None:
            x, y = self.settings.get('x'), self.settings.get('y')
        if not isinstance(x, int) or not isinstance(y, int):
            # 第一次打开：主显示器（坐标原点所在的那块）工作区的右下角
            _, (left, top, right, bottom) = monitor_at(0, 0)
            x, y = right - self.W - round(40 * self.scale), bottom - self.H
        # 拉回到窗口中心所在（或最近）的那块显示器里，副屏拔掉之后也不会跑到看不见的地方
        (left, top, right, bottom), _ = monitor_at(x + self.W // 2, y + self.H // 2)
        x = min(max(left, x), right - self.W)
        y = min(max(top, y), bottom - self.H)
        self.root.geometry(f'{self.W}x{self.H}+{x}+{y}')

    def make_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0, font=self.font)
        size_menu = tk.Menu(self.menu, tearoff=0, font=self.font)
        self.size_var = tk.StringVar(value=self.settings.get('size', '中'))
        for name in SIZES:
            size_menu.add_radiobutton(label=name, variable=self.size_var, value=name, command=self.resize)
        self.menu.add_cascade(label='大小', menu=size_menu)
        self.hide_var = tk.BooleanVar(value=self.settings.get('hideOnFocus', False))
        self.menu.add_checkbutton(label='切到 Claude 时自动隐藏', variable=self.hide_var, command=self.toggle_hide)
        self.sound_var = tk.BooleanVar(value=self.settings.get('sound', True))
        self.menu.add_checkbutton(label='提示音', variable=self.sound_var, command=self.toggle_sound)
        self.menu.add_separator()
        self.menu.add_command(label='退出桌宠', command=self.root.destroy)

    def resize(self):
        self.settings['size'] = self.size_var.get()
        save_settings(self.settings)
        bottom_x = self.root.winfo_x() + self.W // 2
        bottom_y = self.root.winfo_y() + self.H
        self.build()
        self.place_window(bottom_x - self.W // 2, bottom_y - self.H)  # 脚底位置不变，变大后超出屏幕就挪回来

    def toggle_hide(self):
        self.settings['hideOnFocus'] = self.hide_var.get()
        save_settings(self.settings)

    def toggle_sound(self):
        self.settings['sound'] = self.sound_var.get()
        save_settings(self.settings)

    # ---------- 鼠标 ----------
    def on_press(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def on_drag(self, e):
        if self.drag:
            self.root.geometry(f'+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}')

    def on_release(self, e):
        self.drag = None
        self.settings['x'], self.settings['y'] = self.root.winfo_x(), self.root.winfo_y()
        save_settings(self.settings)

    def on_double(self, e):
        # 双击：把 Claude 窗口叫到前台
        hwnd = find_claude_window()
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)

    # ---------- 循环 ----------
    def poll(self):
        latest = read_latest()
        if latest and latest[0] != self.last_mtime:
            self.last_mtime_seen, self.last_mtime = self.last_mtime, latest[0]
            data = latest[1]
            state = data.get('state', 'waiting')
            if state not in SPRITES:
                state = 'waiting'
            changed = state != self.state
            self.state, self.text = state, data.get('text', '')
            self.project = data.get('project', '')
            self.state_since = time.time()
            sound = data.get('sound')
            if sound in ('done', 'error') and self.sound_var.get() and self.last_mtime_seen:  # 刚启动时不重播旧提示音
                try:
                    winsound.PlaySound(os.path.join(SOUNDS, sound + '.wav'),
                                       winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                except Exception:
                    pass
            if changed:
                self.hop_at = time.time()
            self.draw_bubble()
        elif self.effective_state() != self.bubble_state:  # 等太久，进入发呆
            self.draw_bubble()
        self.root.after(250, self.poll)

    def animate(self):
        t = time.time()
        st = self.effective_state()
        s = self.scale
        if st == 'stone':
            bob = 0
        elif st == 'tool':
            bob = 2.5 * math.sin(t * 9)   # 敲键盘：快速小幅抖动
        elif st == 'error':
            bob = 2 * math.sin(t * 25)    # 慌张：发抖
        else:
            bob = 3 * math.sin(t * 2.2)   # 平时：慢慢呼吸
        since = t - self.hop_at
        hop = -HOP * math.sin(since / 0.28 * math.pi) if since < 0.28 else 0  # 换状态时小跳一下
        y = self.rest_y + round((bob + hop) * s)
        text = self.bubble_text
        if self.dots_base is not None:
            # “思考中…” 这类进行中的台词，后面的点点点一跳一跳
            text = self.dots_base + '.' * (int(t * 2.5) % 3 + 1)
        if not self.hidden:
            self.render(st, y, text)
        self.root.after(33, self.animate)

    def watch_focus(self):
        should_hide = self.hide_var.get() and claude_is_foreground() and self.drag is None
        if should_hide and not self.hidden:
            self.root.withdraw()
            self.hidden = True
        elif not should_hide and self.hidden:
            self.root.deiconify()
            self.root.attributes('-topmost', True)
            self.hidden = False
            self.last_frame = None  # 重新显示后强制重画一帧
        self.root.after(300, self.watch_focus)

    def watch_claude(self):
        # Claude 关掉两分钟后，桌宠也自己退出
        if claude_running():
            self.last_alive = time.time()
        elif time.time() - self.last_alive > 120:
            self.root.destroy()
            return
        self.root.after(30000, self.watch_claude)


if __name__ == '__main__':
    try:
        Pet().root.mainloop()
    except Exception:
        import traceback
        with open(os.path.join(RUN_DIR, 'pet-error.log'), 'w', encoding='utf-8') as f:
            traceback.print_exc(file=f)
