"""键盘注入小助手（必须在 DSH 沙箱之外跑）。

为什么单独一个进程：DSH 里发出的 SendInput 会被系统丢掉（实测：返回成功但
目标窗口收不到按键）。而从「计划任务」启动的普通进程注入是有效的——
所以网关通过 127.0.0.1:9889 让它代按键。

协议：一行文本，例如 `ctrl+alt+k`；回 `ok` 或 `err <原因>`。
"""

import ctypes
import os
import json
import ctypes.wintypes as wt
import socketserver
import threading
import time

HOST, PORT = "127.0.0.1", 9893
ULONG_PTR = ctypes.c_void_p
KEYUP, SCANCODE = 0x0002, 0x0008

MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
NAMED = {
    "enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "tab": 0x09,
    "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25,
    "right": 0x27, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
}


class KB(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KB), ("pad", ctypes.c_byte * 32)]


class IN(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


user32 = ctypes.windll.user32
user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(IN), ctypes.c_int)
user32.SendInput.restype = wt.UINT
lock = threading.Lock()


def tap(vk, up=False):
    e = IN(type=1)
    e.u.ki.wVk = vk
    e.u.ki.dwFlags = KEYUP if up else 0
    return user32.SendInput(1, ctypes.byref(e), ctypes.sizeof(IN))


def vk_of(token: str) -> int:
    t = token.strip().lower()
    if t in NAMED:
        return NAMED[t]
    if len(t) == 1:
        code = user32.VkKeyScanW(ord(t))
        return code & 0xFF
    raise ValueError("不认识的键：%s" % token)


def press(combo: str) -> str:
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    if not parts:
        return "err 空组合"
    try:
        mods = [MODS[p] for p in parts[:-1] if p in MODS]
        key = vk_of(parts[-1])
    except (KeyError, ValueError) as exc:
        return "err %s" % exc
    with lock:
        for m in mods:
            tap(m)
        time.sleep(0.03)
        tap(key)
        time.sleep(0.04)
        tap(key, True)
        for m in reversed(mods):
            tap(m, True)
    return "ok"




# ── 让桌宠看一眼屏幕：先把它的窗口顶到前台，再按「看看屏幕」 ──
LOOK_COMBO = "ctrl+alt+k"
OPEN_CHAT_COMBO = "ctrl+alt+q"


def focus_pet() -> bool:
    """把桌宠顶到前台。

    普通 SetForegroundWindow 会被 Windows 拒绝（返回 0），所以用
    AttachThreadInput 把本线程挂到前台线程上，再配合一次 ALT 轻敲骗过前台锁。
    这一段照搬自 dsh-computer-use 的窗口激活做法。
    """
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    best = []

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if "dsh-pet" in buf.value.lower():
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            best.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not best:
        return False
    best.sort(reverse=True)
    hwnd = best[0][1]

    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    self_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = attached_target = False
    if fg_thread and fg_thread != self_thread:
        attached_fg = bool(user32.AttachThreadInput(fg_thread, self_thread, True))
    if target_thread and target_thread != self_thread:
        attached_target = bool(user32.AttachThreadInput(target_thread, self_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        # ALT 轻敲：让系统认为“有键盘输入”，解前台锁
        tap(0x12)
        tap(0x12, True)
        time.sleep(0.1)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached_target:
            user32.AttachThreadInput(target_thread, self_thread, False)
        if attached_fg:
            user32.AttachThreadInput(fg_thread, self_thread, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd or True


PET_DATA = os.path.join(os.environ.get("APPDATA", ""), "dsh-pet-standalone-webm-chat")


def pet_center():
    """现读桌宠位置（她会走动，缓存的一定会点偏）。"""
    try:
        files = [f for f in os.listdir(PET_DATA)
                 if f.startswith("pet-runtime-v2-") and f.endswith(".json")]
        if not files:
            return None
        files.sort(key=lambda f: os.path.getmtime(os.path.join(PET_DATA, f)), reverse=True)
        data = json.load(open(os.path.join(PET_DATA, files[0]), encoding="utf-8"))
        return int(data["x"] + data["w"] / 2), int(data["y"] + data["h"] / 2)
    except Exception:
        return None


def click_pos(x: int, y: int, button: str = "left") -> bool:
    user32 = ctypes.windll.user32
    down = 0x0008 if button == "right" else 0x0002
    up = 0x0010 if button == "right" else 0x0004
    try:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.12)
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.05)
        user32.mouse_event(up, 0, 0, 0, 0)
        return True
    except Exception:
        return False


def do_look_menu() -> str:
    """（备用）右键桌面宠物 → 菜单里选「看看屏幕」。

    为什么不按快捷键：RegisterHotKey 不响应合成输入（实测过）。
    但鼠标右键能让她的菜单真的弹出来，菜单弹出后是带焦点的普通窗口，
    方向键 + 回车能选中「看看屏幕」——这条链路是稳的。
    """
    center = pet_center()
    if not center:
        return "err 读不到桌宠位置"
    cx, cy = center
    if not click_pos(cx, cy, "right"):
        return "err 右键失败"
    time.sleep(0.8)                      # 等菜单弹出并拿到焦点
    for _ in range(3):                   # 菜单里第 3 项是「看看屏幕」
        tap(0x28)                        # VK_DOWN
        time.sleep(0.12)
    tap(0x0D)                            # Enter
    tap(0x0D, True)
    return "ok look@(%d,%d)" % (cx, cy)

# ── 往当前焦点窗口里打中文字（给桌宠的代发用）──

def do_look() -> str:
    """让她看一眼屏幕：直接按她的「看看屏幕」全局热键。

    实测：注入的按键能触发她的热键（vision 计数会涨），前提是她这个实例的热键还活着。
    不起菜单、不打断看片——这是首选项。菜单版留在 do_look_menu 当备用。
    """
    r = press(LOOK_COMBO)
    return "ok hotkey " + r


def type_text(text: str) -> str:
    """用 SendInput 的 UNICODE 方式逐字输入，绕过 IME，中文也能打进去。"""
    KEYEVENTF_UNICODE = 0x0004

    def send_char(ch: str, up: bool) -> int:
        e = IN(type=1)
        e.u.ki.wVk = 0
        e.u.ki.wScan = ord(ch)
        e.u.ki.dwFlags = KEYEVENTF_UNICODE | (KEYUP if up else 0)
        return user32.SendInput(1, ctypes.byref(e), ctypes.sizeof(IN))

    with lock:
        for ch in text:
            send_char(ch, False)
            send_char(ch, True)
            time.sleep(0.01)
        time.sleep(0.06)
        tap(0x0D)
        tap(0x0D, True)
    return "ok"


def find_window(title_sub: str) -> int:
    """按标题子串找可见窗口，返回 hwnd。"""
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    found = []

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if title_sub in buf.value:
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if (r.right - r.left) > 300 and (r.bottom - r.top) > 200:
                found.append((hwnd, r))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return 0
    found.sort(key=lambda x: (x[1].right - x[1].left) * (x[1].bottom - x[1].top), reverse=True)
    return found[0][0]


def click_at(x: int, y: int) -> None:
    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def do_chat(text: str) -> str:
    """代发：打开桌宠的对话窗 → 点一下输入框 → 打字 → 回车。"""
    import ctypes.wintypes as wt

    if not text:
        return "err 空文本"
    focus_pet()
    press(OPEN_CHAT_COMBO)
    time.sleep(1.8)

    hwnd = 0
    for sub in ("泡泡", "AI 对话", "对话"):
        hwnd = find_window(sub)
        if hwnd:
            break
    if not hwnd:
        return "err 没找到对话窗"
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    # 输入框在窗口下部中间（约 88% 高度处）
    # 输入框：窗口左侧面板约 34% 宽，输入区在底部
    click_at(r.left + int(w * 0.78), r.top + int(h * 0.74))
    time.sleep(0.5)
    return paste_text(text)




# ── 剪贴板 + Ctrl+V：WebEngine 的输入框不认注入的 Unicode，但认粘贴 ──

def set_clipboard(text: str) -> bool:
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    GMEM_MOVEABLE = 0x0002
    CF_UNICODETEXT = 13

    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        return True
    finally:
        user32.CloseClipboard()


def paste_text(text: str) -> str:
    if not set_clipboard(text):
        return "err 剪贴板打不开"
    time.sleep(0.15)
    press("ctrl+v")
    time.sleep(0.25)
    tap(0x0D)
    tap(0x0D, True)
    return "ok"




def type_only(text: str) -> str:
    KEYEVENTF_UNICODE = 0x0004

    def send_char(ch, up):
        e = IN(type=1)
        e.u.ki.wVk = 0
        e.u.ki.wScan = ord(ch)
        e.u.ki.dwFlags = KEYEVENTF_UNICODE | (KEYUP if up else 0)
        return user32.SendInput(1, ctypes.byref(e), ctypes.sizeof(IN))

    with lock:
        for ch in text:
            send_char(ch, False); send_char(ch, True); time.sleep(0.01)
    return "ok"


def click_xy(x, y):
    class R_(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return "ok"



def open_chat() -> str:
    """把桌宠顶到前台，再按开会话窗的热键（不按热键的话有时不触发）。"""
    if not focus_pet():
        return "err 没找到桌宠窗口"
    time.sleep(0.2)
    r = press(OPEN_CHAT_COMBO)
    time.sleep(0.4)
    return r




def force_foreground() -> str:
    """用 SwitchToThisWindow（老的、不查前台锁）把桌宠顶上来，并回报结果。"""
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    best = []

    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if "dsh-pet" in buf.value.lower():
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            best.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not best:
        return "err 没窗口"
    best.sort(reverse=True)
    hwnd = best[0][1]
    try:
        user32.SwitchToThisWindow(hwnd, True)
    except Exception as exc:  # noqa: BLE001
        return "err %s" % exc
    time.sleep(0.4)
    fg = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(fg, buf, 256)
    return "ok fg=%s (%s)" % (fg, buf.value[:20])




def click_xy_button(x: int, y: int, button: str = "left") -> str:
    """在屏幕坐标点一下（left/right/double）。鼠标注入在这台机器上是有效的。"""
    user32 = ctypes.windll.user32
    down = 0x0008 if button == "right" else 0x0002
    up = 0x0010 if button == "right" else 0x0004
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    user32.mouse_event(down, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(up, 0, 0, 0, 0)
    return "ok"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(512).decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            cmd, _, arg = line.strip().partition(" ")
            low = cmd.lower()
            if low == "look":
                reply = do_look()
            elif low == "menulook":
                reply = do_look_menu()
            elif low == "type":
                reply = type_text(arg)
            elif low == "focuspet":
                reply = force_foreground()
            elif low == "openchat":
                reply = open_chat()
            elif low == "typeonly":
                reply = type_only(arg)
            elif low in ("clickxy", "rclick"):
                parts = arg.split()
                if len(parts) >= 2:
                    reply = click_xy_button(int(parts[0]), int(parts[1]),
                                            "right" if low == "rclick" else "left")
                else:
                    reply = "err 需要 x y"
            elif low == "paste":
                reply = paste_text(arg)
            elif low == "chat":
                reply = do_chat(arg)
            else:
                reply = press(line)
        except Exception as exc:  # noqa: BLE001
            reply = "err %s" % exc
        self.wfile.write((reply + "\n").encode())


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((HOST, PORT), Handler) as srv:
        srv.serve_forever()
