# -*- coding: utf-8 -*-
"""通用视频模式：前台播放器 + 音频环回 + 键鼠低活跃 → 自动判定「他在看视频」。

素材优先走**音频环回转写**（9881），转不出来（外文片/纯音乐/噪音）再退**字幕条 OCR**。
B 站那两条专用管线不动，这里只管「不是 B 站 的视频」；两边共用同一个 OCR 小工。

判定/退出规则（对齐桌宠语音文档 11.2「进入门槛 + 宽容版离开检测」）：
- 进入：前台是浏览器/播放器 + 环回有声音 + 键鼠低活跃，同一窗口连续 ≥ 120 秒；
- 前台切走但还在播 → 宽限 5 分钟（只记账，不主动说话）；
- 还在播放器上但停了 → 宽限 10 分钟（可能去倒水）；
- 停了且前台也切走 → 2 分钟退出；
- 退出只把 auto 切回 auto，手动设过模式的话一概不碰。
"""
from __future__ import annotations

import collections
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
import wave

log = logging.getLogger("petgw")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 浏览器/播放器：进程名（小写、不带 .exe）
DEFAULT_PLAYERS = [
    "chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "chromium",
    "vlc", "mpv", "potplayermini64", "potplayermini", "mpc-hc64", "mpc-be64",
    "qqplayer", "qqpcmgr", "bilibili", "iqiyi", "youku", "tencentvideo",
    "qqlive", "migu", "baidunetdisk",
]


# --------------------------------------------------------------- Win32 小工具
def _proc_name(pid: int) -> str:
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    except Exception:  # noqa: BLE001
        pass
    finally:
        k32.CloseHandle(handle)
    return ""


def foreground() -> tuple[str, str]:
    """返回 (进程名, 窗口标题)。"""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _proc_name(int(pid.value)), buf.value.strip()


def input_idle_seconds() -> float:
    """键鼠已经安静多少秒。"""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        k32 = ctypes.windll.kernel32
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        ticks = k32.GetTickCount64()
        return max(0.0, (int(ticks) - int(info.dwTime)) / 1000.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _cjk_ratio(text: str) -> float:
    body = re.sub(r"\s+", "", text or "")
    if not body:
        return 0.0
    return len(CJK_RE.findall(body)) / float(len(body))


def _to_wav_bytes(pcm, rate: int = 16000) -> bytes:
    import numpy as np

    data = np.clip(pcm, -1.0, 1.0)
    ints = (data * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(ints.tobytes())
    return buf.getvalue()


# ------------------------------------------------------------------- 音频环回
class AudioLoop(threading.Thread):
    """WASAPI 环回：0.5 秒一块，记电平、攒 10 秒一包给转写。"""

    def __init__(self, conf: dict):
        super().__init__(name="video-audio", daemon=True)
        self.conf = conf
        self.rate = 16000
        self.block = 0.5
        self.chunk_seconds = float(conf.get("asr_chunk_seconds", 10))
        self.level = collections.deque(maxlen=200)   # (ts, rms)
        self.q: "queue.Queue" = queue.Queue(maxsize=4)
        self.lock = threading.Lock()
        self.alive = False
        self.error = ""

    def recent_level(self, seconds: float) -> float:
        now = time.time()
        with self.lock:
            vals = [v for ts, v in self.level if now - ts <= seconds]
        return max(vals) if vals else 0.0

    def has_signal(self, seconds: float = 3.0, threshold: float = 0.004) -> bool:
        return self.recent_level(seconds) > threshold

    def run(self) -> None:
        try:
            import numpy as np
            import soundcard as sc
        except Exception as exc:  # noqa: BLE001
            self.error = "soundcard 不可用：%s" % exc
            log.warning("环回录音不可用：%s", self.error)
            return
        import warnings

        # 换设备/游戏切窗口时会丢一点数据，soundcard 每次都喊一嗓子，压掉
        warnings.filterwarnings("ignore", message="data discontinuity.*")
        try:
            want = str(self.conf.get("loopback_device") or "").strip().lower()
            speaker = None
            if want:
                for sp in sc.all_speakers():
                    if want in str(sp.name).lower():
                        speaker = sp
                        break
            if speaker is None:
                speaker = sc.default_speaker()
                # 默认扬声器是蓝牙耳机（尤其 Hands-Free 那档）时，环回会录到一片安静，
                # 换回板载扬声器
                if any(k in str(speaker.name).lower() for k in ("hands-free", "bthhfenum", "蓝牙")):
                    for sp in sc.all_speakers():
                        if not any(k in str(sp.name).lower() for k in
                                   ("hands-free", "bthhfenum", "蓝牙")):
                            speaker = sp
                            break
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except Exception as exc:  # noqa: BLE001
            self.error = "找不到环回设备：%s" % exc
            log.warning("环回设备不可用：%s", self.error)
            return
        self.alive = True
        log.info("环回就绪：%s", speaker.name)
        buf = []
        need = int(self.rate * self.chunk_seconds)
        while True:
            try:
                with mic.recorder(samplerate=self.rate, channels=1) as rec:
                    while True:
                        block = rec.record(numframes=int(self.rate * self.block))
                        if block is None or len(block) == 0:
                            continue
                        rms = float(np.sqrt((block ** 2).mean()))
                        with self.lock:
                            self.level.append((time.time(), rms))
                        buf.append(block.reshape(-1))
                        if sum(len(x) for x in buf) >= need:
                            pcm = np.concatenate(buf)[:need]
                            buf = []
                            try:
                                self.q.put_nowait(pcm)
                            except queue.Full:
                                pass
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                log.warning("环回录音断了，2 秒后重开：%s", exc)
                time.sleep(2.0)


# --------------------------------------------------------------------- 转写
class AsrWorker(threading.Thread):
    def __init__(self, conf: dict, audio: AudioLoop, on_text):
        super().__init__(name="video-asr", daemon=True)
        self.conf = conf
        self.audio = audio
        self.on_text = on_text
        self.url = str(conf.get("asr_url") or "http://127.0.0.1:9881/v1/audio/transcriptions")
        self.active = threading.Event()
        self.error = ""

    def _post(self, wav: bytes) -> str:
        boundary = "----paopao" + os.urandom(8).hex()
        body = b""
        body += ("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n" % boundary).encode()
        body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                 "Content-Type: audio/wav\r\n\r\n" % boundary).encode()
        body += wav + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        req = urllib.request.Request(  # noqa: F821
            self.url, data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        return str(payload.get("text") or "").strip()

    def run(self) -> None:
        while True:
            pcm = self.audio.q.get()
            if not self.active.is_set():
                continue
            try:
                wav = _to_wav_bytes(pcm)
                text = self._post(wav)
                if text:
                    self.on_text("asr", text)
                    self.error = ""
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                log.warning("视频转写失败：%s", exc)


# --------------------------------------------------------------------- 字幕 OCR
class OcrWorker(threading.Thread):
    """常驻的 rapidocr 小工（独立 venv），每 2 秒读一次下三分之一的字幕条。"""

    def __init__(self, conf: dict, on_text):
        super().__init__(name="video-ocr", daemon=True)
        self.conf = conf
        self.on_text = on_text
        self.python = str(conf.get("ocr_python") or r"D:\app1\tools\ocrvenv\Scripts\python.exe")
        self.script = os.path.join(APP_DIR, "ocr_worker.py")
        self.bottom = float(conf.get("ocr_bottom_ratio", 0.34))
        self.interval = max(1.0, float(conf.get("ocr_interval_seconds", 2.0)))
        self.active = threading.Event()
        self.proc = None
        self.error = ""
        self._last_text = ""

    def _spawn(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.Popen(
            [self.python, self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _shot(self) -> str:
        from PIL import ImageGrab

        img = ImageGrab.grab()
        w, h = img.size
        crop = img.crop((0, int(h * (1 - self.bottom)), w, h))
        path = os.path.join(tempfile.gettempdir(), "paopao_subtitle_%d.png" % int(time.time() * 1000))
        crop.save(path)
        return path

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            if not self.active.is_set():
                continue
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self.proc = self._spawn()
                    self.proc.stdout.readline()   # ready
                path = self._shot()
                self.proc.stdin.write(path + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                try:
                    os.remove(path)
                except OSError:
                    pass
                if not line:
                    self.error = "OCR 小工退出"
                    self.proc = None
                    continue
                data = json.loads(line)
                text = str(data.get("text") or "").strip()
                if text and text != self._last_text:
                    self._last_text = text
                    self.on_text("ocr", text)
                self.error = ""
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                log.warning("字幕 OCR 失败：%s", exc)
                self.proc = None
                time.sleep(1.0)


# --------------------------------------------------------------------- 总控
class VideoWatcher(threading.Thread):
    def __init__(self, cfg: dict, gateway):
        super().__init__(name="video-watch", daemon=True)
        self.conf = dict(cfg.get("video_watch") or {})
        self.gateway = gateway
        self.enabled = bool(self.conf.get("enabled", True))
        self.poll = max(0.5, float(self.conf.get("poll_seconds", 2.0)))
        self.players = [str(x).lower().replace(".exe", "")
                        for x in (self.conf.get("players") or DEFAULT_PLAYERS)]
        self.enter_seconds = float(self.conf.get("enter_min_seconds", 120))
        self.audio_threshold = float(self.conf.get("audio_threshold", 0.004))
        self.audio_window = float(self.conf.get("audio_active_seconds", 30))
        self.idle_seconds = float(self.conf.get("input_idle_seconds", 60))
        self.events_window = float(self.conf.get("input_events_window", 180))
        self.events_max = int(self.conf.get("input_events_max", 15))
        self.away_grace = float(self.conf.get("away_grace_seconds", 300))
        self.paused_grace = float(self.conf.get("paused_grace_seconds", 600))
        self.paused_away_grace = float(self.conf.get("paused_away_grace_seconds", 120))
        self.keep = int(self.conf.get("transcript_keep", 12))
        self.min_cjk = float(self.conf.get("asr_min_cjk_ratio", 0.25))
        self.speak_hint = bool(self.conf.get("speak_hint", True))

        self.lock = threading.Lock()
        self.transcript: "collections.deque" = collections.deque(maxlen=self.keep)
        self.state = "idle"          # idle / probe / active / away
        self.active = False
        self.app = ""
        self.title = ""
        self.session_since = 0.0
        self.last_audio_at = 0.0
        self.last_fore_at = 0.0
        self.asr_bad = 0
        self.ocr_on = False
        self.first_hint = False
        self.error = ""
        self._prev_idle = 99.0
        self._events: "collections.deque" = collections.deque()
        self.audio = AudioLoop(self.conf)
        self.asr = AsrWorker(self.conf, self.audio, self._on_text)
        self.ocr = OcrWorker(self.conf, self._on_text)

    # ---------------------------------------------------------- 对外
    def start(self) -> None:
        if not self.enabled:
            log.info("通用视频模式：配置里关着")
            return
        self.audio.start()
        self.asr.start()
        self.ocr.start()
        super().start()
        log.info("通用视频模式：已就绪（前台播放器 + 环回有声 + 键鼠低活跃 %ds 后自动进入）",
                 int(self.enter_seconds))

    def transcript_count(self) -> int:
        with self.lock:
            return len(self.transcript)

    def force(self) -> None:
        """托盘里手动选「通用视频」：不等 2 分钟的条件，直接进。"""
        app, title = foreground()
        with self.lock:
            self.state = "active"
            self.active = True
            self.session_since = time.time()
            self.app, self.title = app, title
        log.info("通用视频模式：手动进入（%s / %s）", app or "?", (title or "")[:40])

    def context_block(self) -> str:
        with self.lock:
            rows = list(self.transcript)
            app, title = self.app, self.title
            ocr_on = self.ocr_on
        if not rows:
            return ""
        lines = []
        for ts, source, text in rows[-self.keep:]:
            tag = "（OCR）" if source == "ocr" else ""
            lines.append("- [%s]%s %s" % (time.strftime("%H:%M:%S", time.localtime(ts)), tag, text[:120]))
        head = "【你正在和老大一起看视频（通用模式%s）】" % ("，字幕是 OCR 读的" if ocr_on else "")
        return "\n".join([
            head,
            "窗口：%s｜%s" % (app, title[:60]),
            "最近听到/看到的（旧→新）：",
            *lines,
            "说话规矩：只在有值得说的点开口，一两句；不要复述台词、不要剧透、不要报进度。",
        ])

    def take_first_hint(self) -> str:
        with self.lock:
            if not self.first_hint:
                return ""
            self.first_hint = False
        return (
            "【刚进入看视频模式，这是你第一次开口】顺口带一句你也在看（比如「哦，看视频啊」），"
            "一句话融进你本来要说的话里，不要单独立一句，也不要每次都提。"
        )

    def status(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "state": self.state,
                "active": self.active,
                "app": self.app,
                "title": self.title[:60],
                "lines": len(self.transcript),
                "ocr": self.ocr_on,
                "audio": self.audio.has_signal(self.audio_window, self.audio_threshold),
                "error": self.error or self.audio.error or self.asr.error or self.ocr.error,
            }

    # ---------------------------------------------------------- 内部
    def _on_text(self, source: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self.lock:
            self.transcript.append((time.time(), source, text))
        if source == "asr":
            ratio = _cjk_ratio(text)
            with self.lock:
                self.asr_bad = self.asr_bad + 1 if ratio < self.min_cjk else 0
                if self.asr_bad >= 3 and not self.ocr_on:
                    self.ocr_on = True
                    self.ocr.active.set()
                    log.info("视频模式：转写不像中文（%.0f%%），改读字幕条", ratio * 100)

    def _is_player(self, app: str) -> bool:
        name = app.lower().replace(".exe", "")
        if not name:
            return False
        return any(name.startswith(p) or p in name for p in self.players)

    def _input_quiet(self) -> bool:
        idle = input_idle_seconds()
        now = time.time()
        if idle < self.poll:
            self._events.append(now)
        while self._events and now - self._events[0] > self.events_window:
            self._events.popleft()
        return idle >= self.idle_seconds or len(self._events) <= self.events_max

    def _enter(self) -> None:
        with self.lock:
            self.state = "active"
            self.active = True
            self.transcript.clear()
            self.asr_bad = 0
            self.ocr_on = False
            self.first_hint = self.speak_hint
        self.asr.active.set()
        self.last_audio_at = time.time()
        self.last_fore_at = time.time()
        try:
            cur = self.gateway.mode.get()
            if cur == "auto":
                self.gateway.mode.set("video", source="auto-video")
                log.info("通用视频模式：进入（%s｜%s）", self.app, self.title[:40])
        except Exception as exc:  # noqa: BLE001
            log.warning("切到 video 模式失败：%s", exc)

    def _leave(self, reason: str) -> None:
        with self.lock:
            self.state = "idle"
            self.active = False
            self.ocr_on = False
            self.app = ""
            self.title = ""
        self.asr.active.clear()
        self.ocr.active.clear()
        log.info("通用视频模式：退出（%s）", reason)
        try:
            if self.gateway.mode.get() == "video" and self.gateway.mode.source == "auto-video":
                self.gateway.mode.set("auto", source="auto-video")
        except Exception as exc:  # noqa: BLE001
            log.warning("切回 auto 失败：%s", exc)

    def run(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                log.warning("视频模式判定出错：%s", exc)
            time.sleep(self.poll)

    def _tick(self) -> None:
        now = time.time()
        # 模式选「关闭」= 什么都不看：已经在看就退出来，之后也不进
        try:
            if self.gateway.mode.get() == "off":
                if self.state != "idle" or self.active:
                    self._leave("模式关闭")
                return
        except Exception:  # noqa: BLE001
            pass
        app, title = foreground()
        playing = self.audio.has_signal(3.0, self.audio_threshold)
        player = self._is_player(app)
        same = (app and app == self.app and title == self.title)

        if self.state in ("idle", "probe"):
            if player and playing:
                if not same:
                    with self.lock:
                        self.app, self.title = app, title
                        self.session_since = now
                    self.state = "probe"
                elif now - self.session_since >= self.enter_seconds and self._input_quiet():
                    self.last_audio_at = now
                    self._enter()
            else:
                self.state = "idle"
                with self.lock:
                    self.app, self.title, self.session_since = "", "", 0.0
            return

        # active / away
        if playing:
            self.last_audio_at = now
        if player:
            self.last_fore_at = now

        self.state = "active" if (player or not playing) else "away"

        if not playing and not player and (now - self.last_audio_at) > self.paused_away_grace:
            self._leave("停了又切走 2 分钟")
        elif not playing and player and (now - self.last_audio_at) > self.paused_grace:
            self._leave("停在播放器上 10 分钟没动静")
        elif playing and not player and (now - self.last_fore_at) > self.away_grace:
            self._leave("前台离开太久（片子还在放但人不在）")
