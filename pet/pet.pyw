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

from PIL import Image, ImageTk

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
KEY = '#ff00fe'       # 透明色：画布上这个颜色的像素会被挖空
EDGE = (58, 36, 26)   # 半透明边缘和这个深棕色混合，避免边缘发出洋红色的毛边
ORANGE = '#e8743b'

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


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


def prepare(path, height):
    """缩放立绘并把透明部分处理成透明色。"""
    im = Image.open(path).convert('RGBA')
    w = round(im.width * height / im.height)
    im = im.resize((w, height), Image.LANCZOS)
    bg = Image.new('RGBA', im.size, EDGE + (255,))
    solid = Image.alpha_composite(bg, im)
    key = Image.new('RGBA', im.size, KEY)
    mask = im.getchannel('A').point(lambda a: 255 if a >= 110 else 0)
    key.paste(solid, (0, 0), mask)
    return key.convert('RGB')


class Pet:
    def __init__(self):
        self.settings = load_settings()
        self.root = tk.Tk()
        self.root.title('Claude 桌宠')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-transparentcolor', KEY)
        self.root.configure(bg=KEY)
        self.scale = self.root.winfo_fpixels('1i') / 96

        self.canvas = tk.Canvas(self.root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.font = ('Microsoft YaHei UI', 10)
        self.small = ('Microsoft YaHei UI', 8)

        self.state, self.text, self.project = 'waiting', '我来啦～', ''
        self.state_since = time.time()
        self.last_mtime = 0
        self.last_mtime_seen = 0
        self.hop_at = time.time()
        self.hidden = False
        self.drag = None
        self.last_alive = time.time()

        self.build()
        self.place_window()
        self.make_menu()

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
        h = round(SIZES.get(self.settings.get('size', '中'), 190) * self.scale)
        self.images = {}
        for state, name in SPRITES.items():
            self.images[state] = ImageTk.PhotoImage(prepare(os.path.join(HERE, 'sprites', name + '.png'), h))
        self.char_h = h
        self.char_w = max(img.width() for img in self.images.values())
        self.bubble_h = round(52 * self.scale)
        self.W = max(self.char_w, round(190 * self.scale))
        self.H = self.bubble_h + round(8 * self.scale) + h + round(6 * self.scale)
        self.canvas.config(width=self.W, height=self.H)
        self.canvas.delete('all')
        self.sprite = self.canvas.create_image(self.W // 2, self.H, anchor='s', image=self.images[self.state])
        self.draw_bubble()

    def draw_bubble(self):
        c, s = self.canvas, self.scale
        c.delete('bubble')
        text = self.display_text()
        tid = c.create_text(self.W // 2, round(20 * s), text=text, font=self.font, fill='#3a2a1a', tags='bubble')
        x0, y0, x1, y1 = c.bbox(tid)
        pad = round(10 * s)
        x0, x1 = max(1, x0 - pad), min(self.W - 2, x1 + pad)
        y0, y1 = 2, round(38 * s)
        r = round(12 * s)
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               self.W // 2 + round(8 * s), y1, self.W // 2, y1 + round(10 * s), self.W // 2 - round(4 * s), y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        c.create_polygon(pts, smooth=True, fill='#fffaf3', outline=ORANGE, width=max(2, round(2 * s)), tags='bubble')
        c.tag_raise(tid)
        self.bubble_text = tid

    def display_text(self):
        if self.effective_state() == 'idle':
            return '在发呆～ zZ'
        return self.text

    def effective_state(self):
        if self.state == 'waiting' and time.time() - self.state_since > IDLE_AFTER:
            return 'idle'
        return self.state

    def place_window(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = self.settings.get('x', sw - self.W - round(40 * self.scale))
        y = self.settings.get('y', sh - self.H - round(70 * self.scale))
        x, y = min(max(0, x), sw - 40), min(max(0, y), sh - 40)
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
        self.root.geometry(f'{self.W}x{self.H}+{bottom_x - self.W // 2}+{bottom_y - self.H}')

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
            self.canvas.itemconfig(self.sprite, image=self.images[self.effective_state()])
            self.draw_bubble()
        elif self.effective_state() == 'idle' and self.canvas.itemcget(self.sprite, 'image') != str(self.images['idle']):
            self.canvas.itemconfig(self.sprite, image=self.images['idle'])
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
        hop = -14 * math.sin(since / 0.28 * math.pi) if since < 0.28 else 0  # 换状态时小跳一下
        self.canvas.coords(self.sprite, self.W // 2, self.H + round((bob + hop) * s) - round(4 * s))
        if st == 'thinking' and self.text.endswith('中'):
            # “思考中” 后面的点点点一跳一跳
            self.canvas.itemconfig(self.bubble_text, text=self.text + '.' * (int(t * 2.5) % 3 + 1))
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
