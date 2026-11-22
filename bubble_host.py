# -*- coding: utf-8 -*-
"""气泡小工：把桌宠那套原生气泡搬到一个独立进程里跑。

为什么单独一个进程：气泡要 Qt 事件循环，而语音网关是纯 http 线程池——
把 Qt 塞进网关，Qt 一崩语音就哑了。小工崩了最多没气泡。

协议（stdin 一行一个 JSON，stdout 回一行 ack）：
    {"cmd": "show", "text": "要说的话", "duration_ms": 3200}
    {"cmd": "show", "text": "...", "anchor": [x, y, w, h]}
    {"cmd": "hide"}
    {"cmd": "quit"}

**每次弹之前读一次桌宠的 config.json**：样式（self_talk_bubble_style）、
字号（bubble_text_scale）、停留时长（self_talk_duration_seconds）。用户在桌宠
设置里改完，下一句就是新样子；读坏了沿用上一次，不退回默认。

位置：默认读 `%APPDATA%\\dsh-pet-standalone-webm-chat\\pet-runtime-v2-*-slot-*.json`
里最新的窗口矩形，每 30ms 跟一次（mtime 缓存，文件没变不重新解析），四档大小自动跟。

托盘菜单也住在这里（模式切换）：自动 / B站视频 / B站看番 / 通用视频 +
语音关 / 按键说话 / 语音电话。点一下改网关模式，然后冒一句默认 3.2 秒的反馈。

单独试：python bubble_host.py --demo "测试一下气泡"
"""
from __future__ import annotations

import json
import os
import queue
import socketserver
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bubble.speech_bubble import PetSpeechBubble  # noqa: E402
from phrases import TRAY_FEEDBACK  # noqa: E402

GATEWAY_BASE = os.environ.get("PAOPAO_GATEWAY", "http://127.0.0.1:9888")

PET_HOME = Path(os.environ.get("APPDATA", "")) / "dsh-pet-standalone-webm-chat"
PET_CONFIG = PET_HOME / "config.json"
# 桌宠开 stream_capture_mode 时，主窗上方给气泡留了空间：锚点往下挪多少（像素）。
# 跟分辨率/缩放有关，本机 2560×1440@125% 是 70；换机器对不齐就改 config.json 的
# bubble.capture_offset_px（网关通过环境变量传进来），别改代码。
CAPTURE_OFFSET = int(os.environ.get("PAOPAO_CAPTURE_OFFSET", "70") or 70)
ACK = {"ok": True}
DEFAULT_STYLE = "classic_top"
DEFAULT_DURATION_S = 3.2
# 位置缓存：30ms 一跟，靠 mtime 判断要不要重新解析，别每次都读 JSON
_CAPTURE_CACHE: dict = {"pid": 0, "value": False, "ready_at": 0.0, "read": False}
_RECT_CACHE: dict = {"path": "", "mtime": 0, "capture": None, "rect": None}
_MOVE_CACHE: dict = {"pid": 0, "runtime": None, "real": None, "dx": 0, "dy": 0}
_SCALE_CACHE: dict = {"mtime": 0, "scale": 0.0}
# 最近一次 read_pet_rect() 的锚点来源（只给日志看）
_LAST_RECT: dict = {"runtime": None, "real": None, "real_ok": False, "delta": (0, 0)}

# ---------------------------------------------------------------- 托盘菜单
# 观看：单选；语音：单选。键是模式名，值是菜单上那行字。
WATCH_ITEMS = (
    ("off", "关闭"),
    ("auto", "自动模式"),
    ("video", "B站视频"),
    ("anime", "B站看番"),
    ("game", "玩游戏"),
    ("generic", "通用视频"),
)
VOICE_ITEMS = (
    ("off", "语音关"),
    ("ptt", "按键说话"),
    ("call", "语音电话"),
)
# 礼物门槛（电池）：低于这个价值的不谢。100 金瓜子 = 1 电池，10 电池 = 1 元。
GIFT_MIN_ITEMS = (
    (0, "不限（所有礼物都谢）"),
    (1, "1 电池"),
    (5, "5 电池"),
    (10, "10 电池（默认，约 1 元）"),
    (30, "30 电池"),
    (50, "50 电池"),
    (100, "100 电池"),
)
LOCK_ITEMS = (
    ("5m", "5 分钟（默认）"),
    ("forever", "一直保持"),
)
# 反馈文案在 phrases.py 里统一管理（网关预合成时用的是同一份）
FEEDBACK = TRAY_FEEDBACK


def watch_key(mode: str, source: str) -> str:
    """把网关的 (mode, source) 映射成托盘里该勾哪一项。

    2026-11-17 修：以前 game/live/course 全落到「自动模式」，通用视频锁到期后
    还会被当成 B站视频；现在按 source 区分，并且 live/course 也有自己的菜单项。
    """
    mode = str(mode or "auto").lower()
    source = str(source or "").lower()
    if mode == "off":
        return "off"
    # 直播/网课不单独占菜单项：老大说它们统合在「B站看番」里
    if mode in ("anime", "live", "course"):
        return "anime"
    if mode == "game":
        return "game"
    if mode == "video":
        if "generic" in source:
            return "generic"          # 托盘选的通用视频 / 自动判定的非B站播放器
        if "bili" in source:
            return "video"            # 自动判定的B站
        if source == "auto-video":
            return "generic"
        return "video"                # 托盘选的 B站 / API 指定
    return "auto"                     # auto / normal / 未知


def make_tray_icon() -> QIcon:
    """用我们那只头像当托盘图标（跟插件 logo 同一张）。"""
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "paopao.png")
    if os.path.exists(icon_path):
        pix = QPixmap(icon_path)
        if not pix.isNull():
            return QIcon(pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    # 兜底：画一个小蓝圆，不依赖外部图片
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#4aa3ff"))
        painter.drawEllipse(4, 4, 56, 56)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(24, 22, 9, 9)
        painter.drawEllipse(40, 22, 9, 9)
        painter.setBrush(QColor("#b8e2ff"))
        painter.drawEllipse(22, 40, 20, 8)
    finally:
        painter.end()
    return QIcon(pix)


class TrayMenu(QObject):
    """托盘里的模式切换。改动一律走网关 HTTP，勾选状态每秒从 /status 拉一次。"""

    def __init__(self, bubble, on_feedback):
        super().__init__()
        self.bubble = bubble
        self.on_feedback = on_feedback          # 回调：传一句话，弹默认 3.2 秒的气泡
        self.nam = QNetworkAccessManager(self)
        self.syncing = False
        self._last_watch = ""
        # 刚点过的开关：在服务器确认之前，别拿 /status 里的旧值把勾选盖回去。
        # 2026-11-18 修：以前 1 秒一次的 sync 会在 POST 落地前把「直播弹幕」重新勾上，
        # 老大再点一下就成了「开」——看着就像关掉之后自己又打开了。
        self._pending_toggle: dict[str, tuple[bool, float]] = {}
        self._pending_choice: dict[str, tuple[str, float]] = {}

        self.menu = QMenu()
        self.status_act = self.menu.addAction("当前：自动 · 按键说话")
        self.status_act.setEnabled(False)
        self.menu.addSeparator()

        self.watch_group = QActionGroup(self)
        self.watch_group.setExclusive(True)
        self.watch_actions: dict[str, QAction] = {}
        for key, label in WATCH_ITEMS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(("watch", key))
            self.watch_group.addAction(act)
            self.menu.addAction(act)
            act.triggered.connect(self._on_pick)
            self.watch_actions[key] = act

        self.menu.addSeparator()
        self.lock_menu = self.menu.addMenu("手动锁")
        self.lock_group = QActionGroup(self)
        self.lock_group.setExclusive(True)
        self.lock_actions: dict[str, QAction] = {}
        for key, label in LOCK_ITEMS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(("lock", key))
            self.lock_group.addAction(act)
            self.lock_menu.addAction(act)
            act.triggered.connect(self._on_pick)
            self.lock_actions[key] = act

        self.menu.addSeparator()
        self.voice_group = QActionGroup(self)
        self.voice_group.setExclusive(True)
        self.voice_actions: dict[str, QAction] = {}
        for key, label in VOICE_ITEMS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(("voice", key))
            self.voice_group.addAction(act)
            self.menu.addAction(act)
            act.triggered.connect(self._on_pick)
            self.voice_actions[key] = act

        # 直播助手：跟语音同一栏，但**不是单选**，可以和任何模式/语音一起开
        self.menu.addSeparator()
        self.host_act = QAction("直播助手", self)
        self.host_act.setCheckable(True)
        self.host_act.setData(("host", "enabled"))
        self.host_act.triggered.connect(self._on_pick)
        self.menu.addAction(self.host_act)
        # 直播设置（2026-11-22 老大要的）：回弹幕出声 + 欢迎进场都收进这个子菜单
        self.host_menu = self.menu.addMenu("直播设置")
        self.host_speak_act = QAction("回弹幕出声（Ctrl+Alt+B）", self)
        self.host_speak_act.setCheckable(True)
        self.host_speak_act.setData(("host", "speak"))
        self.host_speak_act.triggered.connect(self._on_pick)
        self.host_menu.addAction(self.host_speak_act)
        self.host_welcome_act = QAction("欢迎进场（新观众）", self)
        self.host_welcome_act.setCheckable(True)
        self.host_welcome_act.setData(("host", "welcome"))
        self.host_welcome_act.triggered.connect(self._on_pick)
        self.host_menu.addAction(self.host_welcome_act)
        # 2026-11-22 再加两个：感谢关注 / 感谢加粉丝团（和欢迎同一套）
        self.host_thanks_follow_act = QAction("感谢关注", self)
        self.host_thanks_follow_act.setCheckable(True)
        self.host_thanks_follow_act.setData(("host", "thanks_follow"))
        self.host_thanks_follow_act.triggered.connect(self._on_pick)
        self.host_menu.addAction(self.host_thanks_follow_act)
        self.host_thanks_fans_act = QAction("感谢加粉丝团", self)
        self.host_thanks_fans_act.setCheckable(True)
        self.host_thanks_fans_act.setData(("host", "thanks_fans"))
        self.host_thanks_fans_act.triggered.connect(self._on_pick)
        self.host_menu.addAction(self.host_thanks_fans_act)
        # 上舰和礼物各一项（2026-11-22 老大要的：上舰单列，不混粉丝团）
        self.host_thanks_guard_act = QAction("感谢上舰（舰长/提督/总督）", self)
        self.host_thanks_guard_act.setCheckable(True)
        self.host_thanks_guard_act.setData(("host", "thanks_guard"))
        self.host_thanks_guard_act.triggered.connect(self._on_pick)
        self.host_menu.addAction(self.host_thanks_guard_act)
        self.host_thanks_gift_act = QAction("感谢礼物", self)
        self.host_thanks_gift_act.setCheckable(True)
        self.host_thanks_gift_act.setData(("host", "thanks_gift"))
        self.host_thanks_gift_act.triggered.connect(self._on_pick)
        self.host_menu.addAction(self.host_thanks_gift_act)
        # 礼物门槛（2026-11-22 老大要的）：低于多少电池的礼物不谢
        self.gift_min_menu = self.host_menu.addMenu("礼物门槛（电池）")
        self.gift_min_group = QActionGroup(self)
        self.gift_min_group.setExclusive(True)
        self.gift_min_actions: dict[float, QAction] = {}
        for value, label in GIFT_MIN_ITEMS:
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(("host", "gift_min", value))
            self.gift_min_group.addAction(act)
            self.gift_min_menu.addAction(act)
            act.triggered.connect(self._on_pick)
            self.gift_min_actions[float(value)] = act

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("泡泡 · 模式")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        self.watch_actions["auto"].setChecked(True)
        self.lock_actions["5m"].setChecked(True)
        self.voice_actions["ptt"].setChecked(True)
        self.host_act.setChecked(False)
        self.host_speak_act.setChecked(False)
        self.host_welcome_act.setChecked(True)
        self.host_thanks_follow_act.setChecked(True)
        self.host_thanks_fans_act.setChecked(True)
        self.host_thanks_guard_act.setChecked(True)
        self.host_thanks_gift_act.setChecked(True)
        self.gift_min_actions[10.0].setChecked(True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync)
        self.timer.start(1000)
        QTimer.singleShot(300, self.sync)
        log("托盘就绪（系统托盘 %s）" % ("可用" if QSystemTrayIcon.isSystemTrayAvailable() else "不可用"))

    # ------------------------------------------------------------ 点击
    def _on_pick(self, _checked=False) -> None:
        act = self.sender()
        if not isinstance(act, QAction):
            return
        data = act.data()
        kind, key = data[0], data[1]
        extra = data[2] if len(data) > 2 else None
        if kind == "watch":
            payload = {"mode": "auto" if key == "auto" else key}
            if key == "generic":
                payload = {"mode": "video", "source": "tray-generic"}
            elif key == "off":
                payload = {"mode": "off", "source": "tray"}
            else:
                payload["source"] = "tray"
            self._mark_choice("watch", key)
            self._post("/mode", payload)
            # 气泡交给网关：它要随机挑那句提示，跟语音保持一致（这里再冒一个就会打架）
            return
        if kind == "lock":
            forever = key == "forever"
            self._mark_choice("lock", "forever" if forever else "5m")
            self._post("/mode-lock", {"forever": forever})
            self._feedback("lock_forever" if forever else "lock_5m")
            return
        if kind == "host":
            on = bool(act.isChecked())
            if key == "enabled":
                self._mark_toggle("host", on)
                self._post("/host", {"enabled": on, "persist": True})
            elif key == "welcome":
                self._mark_toggle("host_welcome", on)
                self._post("/host", {"welcome": on, "persist": True})
            elif key == "thanks_follow":
                self._mark_toggle("host_thanks_follow", on)
                self._post("/host", {"thanks_follow": on, "persist": True})
            elif key == "thanks_fans":
                self._mark_toggle("host_thanks_fans", on)
                self._post("/host", {"thanks_fans": on, "persist": True})
            elif key == "thanks_guard":
                self._mark_toggle("host_thanks_guard", on)
                self._post("/host", {"thanks_guard": on, "persist": True})
            elif key == "thanks_gift":
                self._mark_toggle("host_thanks_gift", on)
                self._post("/host", {"thanks_gift": on, "persist": True})
            elif key == "gift_min":
                self._mark_gift_min(extra)
                self._post("/host", {"gift_min_value": extra, "persist": True})
            else:
                self._mark_toggle("host_speak", on)
                self._post("/host", {"speak": on, "persist": True})
            # 气泡和声音由网关统一出，这里不再重复冒一个
            return
        self._mark_choice("voice", key)
        self._post("/voice", {"action": "set_mode", "mode": key})
        # 2026-11-19：语音模式的提示气泡和声音统一由网关出，这里不再重复冒

    # ------------------------------------------------------- 勾选防抖
    def _mark_toggle(self, key: str, want: bool) -> None:
        self._pending_toggle[key] = (bool(want), time.time() + 4.0)

    def _mark_choice(self, key: str, want: str) -> None:
        self._pending_choice[key] = (str(want), time.time() + 4.0)

    def _mark_gift_min(self, value) -> None:
        try:
            self._pending_choice["gift_min"] = ("%g" % float(value),
                                               time.time() + 4.0)
        except (TypeError, ValueError):
            pass

    def _toggle_blocks(self, key: str, server_on: bool) -> bool:
        """服务器还没跟上自己刚点的值 → 先别用 /status 覆盖勾选。"""
        pend = self._pending_toggle.get(key)
        if not pend:
            return False
        want, deadline = pend
        if bool(server_on) == want:
            self._pending_toggle.pop(key, None)
            return False
        if time.time() > deadline:
            self._pending_toggle.pop(key, None)
            return False
        return True

    def _choice_blocks(self, key: str, server_choice: str) -> bool:
        pend = self._pending_choice.get(key)
        if not pend:
            return False
        want, deadline = pend
        if server_choice == want:
            self._pending_choice.pop(key, None)
            return False
        if time.time() > deadline:
            self._pending_choice.pop(key, None)
            return False
        return True

    def _on_activated(self, reason) -> None:
        """左键单击：直接开关语音电话。"""
        if reason != QSystemTrayIcon.Trigger:
            return
        current = self.voice_actions
        turn_on = not (current["call"].isChecked())
        self._mark_choice("voice", "call" if turn_on else "ptt")
        self._post("/voice", {"action": "set_mode", "mode": "call" if turn_on else "ptt"})
        # 同上：提示交给网关

    def _feedback(self, key: str) -> None:
        text = FEEDBACK.get(key)
        if text:
            try:
                self.on_feedback(text)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 网络
    def _post(self, path: str, payload: dict) -> None:
        req = QNetworkRequest(QUrl(GATEWAY_BASE + path))
        req.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reply = self.nam.post(req, body)

        def _finished() -> None:
            try:
                code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if code is not None and int(code) >= 400:
                    log("POST %s 被网关拒了（HTTP %s）：%s"
                        % (path, code, json.dumps(payload, ensure_ascii=False)))
            except Exception:  # noqa: BLE001
                pass
            reply.deleteLater()
            QTimer.singleShot(150, self.sync)

        reply.finished.connect(_finished)

    def sync(self) -> None:
        reply = self.nam.get(QNetworkRequest(QUrl(GATEWAY_BASE + "/status")))
        reply.finished.connect(lambda: self._apply_status(reply))

    def _apply_status(self, reply) -> None:
        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            data = {}
        finally:
            reply.deleteLater()
        mode = str(data.get("mode") or "auto")
        source = str(data.get("mode_source") or "")
        watch = watch_key(mode, source)
        if watch != getattr(self, "_last_watch", ""):
            self._last_watch = watch
            log("托盘同步：%s / %s → %s" % (mode, source, watch))
        voice = str(((data.get("voice") or {}).get("mode")) or "ptt")
        lock_forever = bool(data.get("mode_lock_forever"))
        labels = dict(WATCH_ITEMS)
        self.status_act.setText("当前：%s · %s · %s" % (
            labels.get(watch, watch),
            dict(VOICE_ITEMS).get(voice, voice),
            "锁一直" if lock_forever else "锁5分钟"))
        # 每一项都先问一句「我刚点的那个生效了吗」：没生效就别拿旧状态盖回去
        if not self._choice_blocks("watch", watch):
            for key, act in self.watch_actions.items():
                act.blockSignals(True)
                act.setChecked(key == watch)
                act.blockSignals(False)
        lock_choice = "forever" if lock_forever else "5m"
        if not self._choice_blocks("lock", lock_choice):
            for key, act in self.lock_actions.items():
                act.blockSignals(True)
                act.setChecked(key == lock_choice)
                act.blockSignals(False)
        if not self._choice_blocks("voice", voice):
            for key, act in self.voice_actions.items():
                act.blockSignals(True)
                act.setChecked(key == voice)
                act.blockSignals(False)
        host = data.get("host") or {}
        for act, key, on in ((self.host_act, "host", bool(host.get("enabled"))),
                             (self.host_speak_act, "host_speak", bool(host.get("speak"))),
                             (self.host_welcome_act, "host_welcome",
                              bool(host.get("welcome", True))),
                             (self.host_thanks_follow_act, "host_thanks_follow",
                              bool(host.get("thanks_follow", True))),
                             (self.host_thanks_fans_act, "host_thanks_fans",
                              bool(host.get("thanks_fans", True))),
                             (self.host_thanks_guard_act, "host_thanks_guard",
                              bool(host.get("thanks_guard", True))),
                             (self.host_thanks_gift_act, "host_thanks_gift",
                              bool(host.get("thanks_gift", True)))):
            if self._toggle_blocks(key, on):
                continue
            act.blockSignals(True)
            act.setChecked(on)
            act.blockSignals(False)
        if host.get("error"):
            self.host_act.setToolTip(str(host.get("error"))[:120])
        try:
            gmin = float(host.get("gift_min_value") or 0)
        except (TypeError, ValueError):
            gmin = 0.0
        if not self._choice_blocks("gift_min", "%g" % gmin):
            for value, act in self.gift_min_actions.items():
                act.blockSignals(True)
                act.setChecked(abs(float(value) - gmin) < 0.5)
                act.blockSignals(False)


def log(msg: str) -> None:
    """日志走 stderr：stdout 专门留给 ack，别混在一起。"""
    print("[bubble] %s" % msg, file=sys.stderr, flush=True)


_ACK_SINK = None


def _ack(**fields) -> None:
    if _ACK_SINK is not None:
        _ACK_SINK({"ok": True, **fields})
        return
    print(json.dumps({"ok": True, **fields}, ensure_ascii=False), flush=True)


def read_bubble_cfg(prev: dict) -> dict:
    """每次弹之前读一次。

    读不到/读坏了 → 沿用上一次（别因为半截 JSON 把样式打回默认）；
    读到了但**键不在** → 用默认值（她在设置里清掉就是回到默认，不是保留旧值）。
    """
    try:
        data = json.loads(PET_CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return prev
    style = str(data.get("self_talk_bubble_style") or DEFAULT_STYLE)
    try:
        scale = float(data.get("bubble_text_scale") or 100) / 100.0
    except (TypeError, ValueError):
        scale = 1.0
    try:
        dur = float(data.get("self_talk_duration_seconds") or DEFAULT_DURATION_S) * 1000
    except (TypeError, ValueError):
        dur = DEFAULT_DURATION_S * 1000
    return {
        "style": style,
        "scale": max(0.5, min(3.0, scale)) if scale else 1.0,
        "duration_ms": int(dur) if dur and dur >= 500 else int(DEFAULT_DURATION_S * 1000),
    }


# 桌宠的位置标记只有 **1Hz**（它是"定期存档"，不是实时位置），所以她走着的时候
# 最坏会差一步（~60px）。试过用两次样本估速度外推，拐弯/停下时会过冲，反而更糟，
# 所以现在只用"新样本一到就瞬间对齐"（不做预测）。


def current_pet_pid() -> int:
    try:
        files = sorted(PET_HOME.glob("pet-runtime-v2-*-slot-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        return int(data.get("pid") or 0)
    except Exception:  # noqa: BLE001
        return 0


def pet_stream_capture() -> bool:
    """桌宠启动几秒后读一次捕获兼容开关；之后按这个 pid 缓存，不再连续读文件。"""
    pid = current_pet_pid()
    now = time.time()
    if pid != _CAPTURE_CACHE["pid"]:
        _CAPTURE_CACHE.update(pid=pid, value=False, ready_at=now + 3.0, read=False)
        return False
    if now < _CAPTURE_CACHE["ready_at"]:
        return False
    if not _CAPTURE_CACHE["read"]:
        try:
            data = json.loads(PET_CONFIG.read_text(encoding="utf-8-sig"))
            _CAPTURE_CACHE["value"] = bool(data.get("stream_capture_mode"))
        except Exception:  # noqa: BLE001
            _CAPTURE_CACHE["value"] = False
        _CAPTURE_CACHE["read"] = True
    return bool(_CAPTURE_CACHE["value"])


def read_pet_window_rect(pid: int) -> QRect | None:
    """桌宠真实可见窗口矩形（用来算直播捕获兼容在上方预留了多少）。"""
    if not pid:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        best = {"area": 0, "rect": None}
        user32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            wpid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if int(wpid.value) != pid:
                return True
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = int(rect.right - rect.left)
            h = int(rect.bottom - rect.top)
            if w >= 80 and h >= 80 and w * h > best["area"]:
                best["area"] = w * h
                best["rect"] = QRect(int(rect.left), int(rect.top), w, h)
            return True

        user32.EnumWindows(proc(cb), 0)
        return best["rect"]
    except Exception:  # noqa: BLE001
        return None


def _same_order(real: QRect, runtime: QRect) -> bool:
    """真实窗口跟精灵是不是一个量级（才可能是精灵窗，位移增量才有意义）。

    桌宠「单合成窗」拓扑下，pid 最大的可见窗口是一整块全屏透明画布（2048×1152），
    跟精灵（156×194）差了一个数量级。拿它的位移当「拖动增量」会出事：
    画布随显示器/任务栏/窗口重建变一下，增量就被记进锚点、推到屏幕另一头，
    而且 runtime 不动就一直错下去 —— 老大实测「气泡偏了，拖一下桌宠才对」。
    """
    def close(a: int, b: int) -> bool:
        return abs(a - b) <= max(80, int(0.5 * max(a, b)))
    return close(real.width(), runtime.width()) and close(real.height(), runtime.height())


def read_pet_rect() -> QRect | None:
    """返回桌宠精灵的全局矩形（桌宠自己写进 runtime json 的那个）。

    桌宠**每帧移动都会重写**这个文件（`_position_sync_pending` 是 0ms 去抖），
    所以我们跟得有多快只取决于自己的轮询频率。这里用 (路径, mtime) 做缓存：
    50ms 一跟，只要文件没变就复用上次解析结果，不去重读 JSON。
    """
    try:
        files = sorted(
            PET_HOME.glob("pet-runtime-v2-*-slot-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    # 真实窗是物理像素、runtime 是逻辑像素，换算用；拿不到就当 1:1
    try:
        scr = QApplication.primaryScreen()
        dpr = float(scr.devicePixelRatio()) if scr is not None else 1.0
    except Exception:  # noqa: BLE001
        dpr = 1.0
    if not dpr or dpr <= 0:
        dpr = 1.0
    for path in files[:3]:
        try:
            mtime = path.stat().st_mtime_ns
            capture = pet_stream_capture()
            data = json.loads(path.read_text(encoding="utf-8"))
            w, h = int(data.get("w") or 0), int(data.get("h") or 0)
            if w > 0 and h > 0:
                pid = int(data.get("pid") or 0)
                runtime = QRect(int(data.get("x") or 0), int(data.get("y") or 0), w, h)
                real = read_pet_window_rect(pid)
                if real is not None and _same_order(real, runtime):
                    # runtime 是精灵的真实位置；真实窗口只用来算“拖动增量”。
                    # runtime 没刷新时，把窗口移动量补到 runtime 上；runtime
                    # 一刷新就回到 runtime 基准，避免透明边距造成固定偏移。
                    if _MOVE_CACHE["pid"] != pid or _MOVE_CACHE["runtime"] is None:
                        _MOVE_CACHE.update(pid=pid, runtime=runtime, real=real, dx=0, dy=0)
                    else:
                        old_rt = _MOVE_CACHE["runtime"]
                        old_real = _MOVE_CACHE["real"]
                        rt_moved = (runtime.x() != old_rt.x() or runtime.y() != old_rt.y())
                        if rt_moved or old_real is None:
                            _MOVE_CACHE.update(runtime=runtime, real=real, dx=0, dy=0)
                        else:
                            # GetWindowRect 是物理像素，runtime 是 Qt 逻辑像素（125% 缩放就
                            # 差 1.25 倍）；不换算的话拖动增量会被放大，气泡拖完对不准。
                            sx = (real.x() - old_real.x()) / dpr
                            sy = (real.y() - old_real.y()) / dpr
                            if abs(sx) > 400 or abs(sy) > 400:
                                # 一步跳几百像素 = 换了个窗口/换了屏，不是拖动，别累加
                                _MOVE_CACHE.update(runtime=runtime, real=real, dx=0, dy=0)
                            else:
                                # 增量夹在 ±400 以内：拖来拖去也不会越按越偏
                                _MOVE_CACHE["dx"] = max(-400, min(400, _MOVE_CACHE["dx"] + int(sx)))
                                _MOVE_CACHE["dy"] = max(-400, min(400, _MOVE_CACHE["dy"] + int(sy)))
                                _MOVE_CACHE["real"] = real
                    rect = QRect(runtime.x() + _MOVE_CACHE["dx"],
                                 runtime.y() + _MOVE_CACHE["dy"],
                                 runtime.width(), runtime.height())
                else:
                    # 真实窗口不是精灵窗（单合成窗/全屏画布）：不用它的增量，
                    # 锚点就取 runtime，别把一个全屏画布的位移算进去。
                    _MOVE_CACHE.update(pid=pid, runtime=runtime, real=None, dx=0, dy=0)
                    rect = runtime
                rect.pet_pid = pid  # type: ignore[attr-defined]
                if pet_stream_capture():
                    rect = QRect(rect.x(), rect.y() + CAPTURE_OFFSET,
                                 rect.width(), rect.height())
                    rect.pet_pid = pid  # type: ignore[attr-defined]
                _RECT_CACHE.update(path=str(path), mtime=mtime, capture=capture, rect=rect)
                # 留一份「这一拍锚点是怎么算出来的」给日志用：气泡要是偏了，
                # logs\bubble.log 能直接看出是 runtime 过期、还是真实窗/增量掺了进来。
                _LAST_RECT.update(
                    runtime=(runtime.x(), runtime.y(), runtime.width(), runtime.height()),
                    real=(None if real is None else
                          (real.x(), real.y(), real.width(), real.height())),
                    real_ok=bool(real is not None and _same_order(real, runtime)),
                    delta=(_MOVE_CACHE["dx"], _MOVE_CACHE["dy"]),
                )
                return rect
        except Exception:
            continue
    return None


def pet_window_visible(pid: int) -> bool:
    """这个 pid 有没有一块「像样的」可见窗口（她没被收进托盘/隐藏）。

    气泡是挂在她头上的；她不在画面上时还弹一个气泡飘在桌面上会很怪——
    桌宠自己的 show_bubble 第一行就是 ``if not self.isVisible(): return False``。
    """
    if not pid:
        return True
    try:
        import ctypes
        import ctypes.wintypes as wt

        found = {"ok": False}
        user32 = ctypes.windll.user32
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            wpid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if int(wpid.value) != pid:
                return True
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            if (rect.right - rect.left) >= 60 and (rect.bottom - rect.top) >= 60:
                found["ok"] = True
                return False
            return True

        user32.EnumWindows(proc(cb), 0)
        return bool(found["ok"])
    except Exception:  # noqa: BLE001
        return True


def read_pet_scale(prev: float = 1.0) -> float:
    """桌宠当前的缩放（四档那个）。存在 config-slot-*.json 的 scale 里。"""
    try:
        files = sorted(PET_HOME.glob("config-slot-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = files[0]
        mtime = path.stat().st_mtime_ns
        if _SCALE_CACHE["mtime"] == mtime:
            return _SCALE_CACHE["scale"] or prev
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        value = float(data.get("scale") or 0)
        if 0.3 <= value <= 3.0:
            _SCALE_CACHE.update(mtime=mtime, scale=value)
            return value
    except Exception:  # noqa: BLE001
        pass
    return prev


_LAST_TOP: dict = {}
_LAST_BUBBLE_RECT: dict = {}


def _clear_old_bubble(overlay, bubble) -> None:
    """新气泡之前，把上一块区域擦掉。

    2026-11-19：透明全屏窗上的气泡缩小时，旧文字那圈像素没人擦，
    新气泡压上去就变成两段字叠在一起（老大看到的「乱码/看不见」就是这个）。
    """
    old = _LAST_BUBBLE_RECT.get("r")
    try:
        new = bubble.geometry()
    except Exception:  # noqa: BLE001
        new = None
    rect = old
    if new is not None:
        rect = old.united(new) if old is not None else new
    if rect is not None and overlay is not None:
        try:
            overlay.update(rect)
            overlay.repaint(rect)
        except Exception:  # noqa: BLE001
            pass
    if new is not None:
        _LAST_BUBBLE_RECT["r"] = new


def force_topmost(widget) -> None:
    """再压一次最上层。

    PetSpeechBubble 自己已经是 WS_EX_TOPMOST，但「置顶窗口」之间还按激活顺序排：
    游戏那种自己也置顶的窗口会盖住我们（WA_ShowWithoutActivating 不会把它顶上去）。
    这里用 HWND_TOPMOST + 不激活重排一次，气泡才压得住全屏游戏窗。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = int(widget.winId())
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        # 2026-11-19：单纯 SetWindowPos(TOPMOST) 有时候「已经是最上层」就不生效
        # （老大的桌宠也这样：关掉置顶再打开才回来）。所以先撤掉再压回去，
        # 等于手动做一遍「关掉置顶 → 再打开」，但不用人点。
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        try:
            widget.raise_()
        except Exception:  # noqa: BLE001
            pass
        # 顺便把窗口矩形记下来，看不见的时候能一眼看出是不是被挪走了
        _LAST_TOP = {"hwnd": hwnd, "ex": 0}
        GWL_EXSTYLE = -20
        WS_EX_TOPMOST = 0x00000008
        try:
            ex = user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            _LAST_TOP["ex"] = int(ex)
            _LAST_TOP["topmost"] = bool(int(ex) & WS_EX_TOPMOST)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    demo = ""
    demo_anchor = None
    if "--demo" in sys.argv:
        idx = sys.argv.index("--demo")
        demo = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "测试一下"
    if "--anchor" in sys.argv:
        idx = sys.argv.index("--anchor")
        try:
            parts = [int(v) for v in sys.argv[idx + 1].split(",")]
            if len(parts) == 4:
                demo_anchor = QRect(*parts)
        except Exception:  # noqa: BLE001
            demo_anchor = None

    app = QApplication(sys.argv[:1])
    # 记下 pid：网关重启时用它收掉上一任气泡小工，免得托盘出现两个图标
    try:
        pid_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(pid_dir, exist_ok=True)
        with open(os.path.join(pid_dir, "bubble_host.pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    # 全屏采集画布：OBS/直播姬用「窗口采集」抓这个窗口，气泡按桌面真实位置画。
    # 窗口固定满屏、不跟着桌宠移动，采集源就不会在桌宠/气泡之间跳。
    capture_overlay = os.environ.get("PAOPAO_BUBBLE_CAPTURE", "0") == "1"
    overlay = None
    screen = QApplication.primaryScreen()
    screen_geo = screen.geometry() if screen is not None else QRect(0, 0, 1920, 1080)
    CAPTURE_W, CAPTURE_H = screen_geo.width(), screen_geo.height()
    if capture_overlay:
        log("全屏采集画布 %dx%d（气泡按桌面位置画）" % (CAPTURE_W, CAPTURE_H))
    if capture_overlay:
        overlay = QWidget()
        overlay.setWindowTitle("泡泡气泡（全屏）")
        overlay.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        overlay.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        overlay.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        overlay.setGeometry(screen_geo)
        overlay.show()
        keep_top = QTimer(overlay)
        keep_top.timeout.connect(lambda: force_topmost(overlay))
        keep_top.start(800)
    bubble = PetSpeechBubble(None, style_id="classic_top")
    if overlay is not None:
        bubble.set_capture_compat(True, overlay)
    else:
        # 独立 Tool 窗口模式：OBS 窗口采集按这个标题找；气泡本身跟着桌宠位置走
        bubble.setWindowTitle("泡泡气泡")
    state = {"style": "classic_top", "scale": 1.0, "duration_ms": 3200}
    cmds: "queue.Queue[dict]" = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmds.put(json.loads(line))
            except Exception:
                continue
        # stdin EOF 不退出：网关重启后新网关会走本地 TCP 接回来，
        # 这样 OBS/直播姬里的窗口句柄不会变，不会掉回桌宠窗口。

    def center_bubble(anchor: QRect) -> None:
        """固定正上方居中；桌宠真实窗口的包围盒和气泡自动选位不一致时用这个。"""
        anim = getattr(bubble, "_pos_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:  # noqa: BLE001
                pass
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1080)
        w, h = bubble.width(), bubble.height()
        x = anchor.center().x() - w // 2
        y = anchor.top() - h - 12
        if y < avail.top():
            y = anchor.bottom() + 12
        x = max(avail.left(), min(x, avail.right() - w + 1))
        y = max(avail.top(), min(y, avail.bottom() - h + 1))
        bubble.move(x, y)
        state["manual_pos"] = True

    def do_show(cmd: dict) -> None:
        nonlocal state
        text = str(cmd.get("text") or "").strip()
        if not text:
            return
        state = read_bubble_cfg(state)
        try:
            bubble.set_style(state["style"])
        except Exception as exc:  # noqa: BLE001
            log("样式切换失败（保持原样）：%s" % exc)
        try:
            # 单条指定字号优先（直播弹幕要更大，观众才看得清）
            scale = float(cmd.get("scale") or state["scale"])
            state["scale"] = max(0.5, min(3.0, scale))
            bubble.set_text_scale(state["scale"])
        except Exception:  # noqa: BLE001
            pass
        anchor = None
        explicit = False
        raw = cmd.get("anchor")
        if isinstance(raw, list) and len(raw) == 4:
            anchor = QRect(*(int(v) for v in raw))
            explicit = True
        if anchor is None:
            anchor = read_pet_rect()
        if anchor is not None:
            state["offset_y"] = int(cmd.get("offset_y") or 0)
            if state["offset_y"]:
                anchor = QRect(anchor.x(), anchor.y() + state["offset_y"],
                               anchor.width(), anchor.height())
        if anchor is None:
            log("找不到桌宠窗口，气泡没法定位")
            _ack(id=cmd.get("id"), shown=False, reason="no-pet")
            return
        # 她不在画面上（收进托盘/隐藏）或者缩成一条边栏时，桌宠自己也不弹 —— 照抄这个规矩
        pid = int(getattr(anchor, "pet_pid", 0) or 0)
        if not explicit and not pet_window_visible(pid):
            log("桌宠当前不可见，跳过气泡")
            _ack(id=cmd.get("id"), shown=False, reason="pet-hidden")
            return
        if not explicit and (anchor.width() < 80 or anchor.height() < 80):
            log("桌宠缩成边栏了（%dx%d），跳过气泡" % (anchor.width(), anchor.height()))
            _ack(id=cmd.get("id"), shown=False, reason="pet-docked")
            return
        duration = int(cmd.get("duration_ms") or state["duration_ms"])
        state["pet_scale"] = read_pet_scale(state.get("pet_scale", 1.0))
        if _MOVE_CACHE.get("dx") or _MOVE_CACHE.get("dy"):
            log("锚点补了拖动增量：runtime 基准 +(%d,%d)" % (_MOVE_CACHE["dx"], _MOVE_CACHE["dy"]))
        # 完全照她 show_bubble 的写法：锚点用精灵包围盒，不额外挪
        _clear_old_bubble(overlay, bubble)      # 先擦掉上一气泡的残影
        bubble.show_text(text, anchor, duration_ms=duration, pet_scale=state["pet_scale"])
        # 首弹可能拿到桌宠还没刷新的 runtime、或者字排完控件尺寸又动了一点；
        # 隔几拍按最新锚点再摆一次（force 忽略位置去重），不用等用户拖桌宠。
        def _refresh_pos() -> None:
            if not bubble.isVisible():
                return
            # 2026-11-19：气泡显示期间继续抢置顶（别人抢了 z-order 也能压回来）
            force_topmost(overlay if overlay is not None else bubble)
            r = read_pet_rect()
            if r is None:
                return
            off = int(state.get("offset_y") or 0)
            if off:
                r = QRect(r.x(), r.y() + off, r.width(), r.height())
            bubble.reposition(r, force=True)

        for delay in (120, 320, 700, 1500, 3000, 5000):
            QTimer.singleShot(delay, _refresh_pos)
        force_topmost(overlay if overlay is not None else bubble)
        state["anchor"] = (anchor.x(), anchor.y(), anchor.width(), anchor.height())
        state["pid"] = pid
        shown = bool(bubble.isVisible())
        if not explicit:
            src = _LAST_RECT
            log("锚点来源：runtime=%s 真实窗=%s 增量=%s（%s）"
                % (src["runtime"], src["real"], src["delta"],
                   "真实窗可用" if src["real_ok"] else "真实窗不是精灵窗，不用增量"))
        top = _LAST_TOP.get("topmost")
        log("弹：%s（样式 %s，字号 %.2f，缩放 %.2f，%dms，锚点 (%d,%d)，气泡 (%d,%d)，%s%s）"
            % (text[:20], state["style"], state["scale"], state["pet_scale"], duration,
               anchor.x(), anchor.y(), bubble.x(), bubble.y(),
               "上屏" if shown else "没上屏",
               "" if top is None else ("，置顶=%s" % ("是" if top else "否"))))
        _ack(id=cmd.get("id"), shown=shown, anchor=[anchor.x(), anchor.y()],
             size=[bubble.width(), bubble.height()],
             screen=[bubble.x(), bubble.y()])

    def poll() -> None:
        global _ACK_SINK
        quit_now = False
        try:
            while True:
                cmd = cmds.get_nowait()
                _ACK_SINK = cmd.get("_ack")
                try:
                    kind = str(cmd.get("cmd") or "")
                    if kind == "show":
                        do_show(cmd)
                    elif kind == "hide":
                        bubble.dismiss()
                        _ack(id=cmd.get("id"), hidden=True)
                    elif kind == "quit":
                        quit_now = True
                finally:
                    _ACK_SINK = None
        except queue.Empty:
            pass
        if quit_now:
            app.quit()

    def follow() -> None:
        """照桌宠的做法：窗口动了用 reposition，缩放变了用 reflow。"""
        if not bubble.isVisible():
            return
        rect = read_pet_rect()
        if rect is None:
            return
        off = int(state.get("offset_y") or 0)
        if off:
            rect = QRect(rect.x(), rect.y() + off, rect.width(), rect.height())
        cur = (rect.x(), rect.y(), rect.width(), rect.height())
        scale = read_pet_scale(state.get("pet_scale", 1.0))
        try:
            if cur != state.get("anchor"):
                state["anchor"] = cur
                if state.get("manual_pos"):
                    center_bubble(rect)
                else:
                    bubble.reposition(rect)
                now = time.time()
                if now - state.get("last_follow_log", 0.0) >= 1.0:
                    state["last_follow_log"] = now
                    log("跟随：(%d,%d) → 气泡 (%d,%d)" % (rect.x(), rect.y(), bubble.x(), bubble.y()))
            if abs(scale - state.get("pet_scale", 1.0)) > 0.01:
                state["pet_scale"] = scale
                bubble.reflow(rect, pet_scale=scale)
                if state.get("manual_pos"):
                    center_bubble(rect)
            # 自愈：每 0.6 秒按当前锚点强制重算一次落点。首弹尺寸没 settle、
            # 窗口被系统搬走、跟随拍被去重跳过、锚点补位移算歪——都在一拍内回正，
            # 不用再靠「拖一下桌宠」把它拽回来。
            now = time.time()
            if now - state.get("last_force", 0.0) >= 0.6:
                state["last_force"] = now
                before = (bubble.x(), bubble.y())
                bubble.reposition(rect, force=True)
                after = (bubble.x(), bubble.y())
                if before != after:
                    log("自愈：气泡 (%d,%d) → (%d,%d)（锚点 (%d,%d)）"
                        % (before[0], before[1], after[0], after[1], rect.x(), rect.y()))
        except Exception:  # noqa: BLE001
            pass

    if "--demo" not in sys.argv:
        class BubbleHandler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for line in self.rfile:
                    try:
                        cmd = json.loads(line.decode("utf-8", "replace"))
                    except Exception:  # noqa: BLE001
                        continue
                    done = threading.Event()
                    box: dict = {}

                    def reply(fields: dict, _box=box, _done=done) -> None:
                        _box.update(fields)
                        _done.set()

                    cmd["_ack"] = reply
                    cmds.put(cmd)
                    if not done.wait(8.0):
                        box = {"ok": False, "error": "timeout"}
                    self.wfile.write(
                        (json.dumps(box, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()

        class BubbleServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        try:
            server = BubbleServer(("127.0.0.1", 9894), BubbleHandler)
            threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
            log("气泡 IPC：127.0.0.1:9894（网关重启后窗口句柄保持不变）")
        except OSError as exc:
            log("气泡 IPC 端口被占用，保留已有实例：%s" % exc)

    if "--demo" not in sys.argv:
        tray = TrayMenu(bubble, lambda text: do_show({"text": text, "duration_ms": 3200}))
        app.aboutToQuit.connect(tray.tray.hide)

    QTimer(bubble, timeout=poll).start(30)
    # 跟得紧一点：桌宠每帧都在重写 runtime json，30ms 一次（读缓存不解析）
    # 实测写入→气泡挪位 5~20ms；以前的 200ms 在她“跳着走”时会明显落后半步
    QTimer(bubble, timeout=follow).start(30)
    # --demo 是单独试用的：不接 stdin（不然没管道时立刻 EOF → 自己退出）
    if "--demo" not in sys.argv:
        threading.Thread(target=reader, name="stdin", daemon=True).start()
    log("就绪（配置：%s）" % PET_CONFIG)

    if demo:
        rect = demo_anchor or read_pet_rect()
        log("demo 位置：%s" % (rect,))
        if rect is not None:
            QTimer.singleShot(300, lambda: do_show({"text": demo, "anchor": [
                rect.x(), rect.y(), rect.width(), rect.height()]}))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
