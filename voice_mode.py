# -*- coding: utf-8 -*-
"""语音模式：按一下热键开始录，停顿够久自动结束，转写 → 模型 → 她说 + 头顶气泡。

会话（2026-11-16 定稿）：
- 热键按一下开录，再按一下取消；停顿 `vad_silence`（默认 1.2 秒）自动结束；
- 最长 `max_record`（30 秒）强制结束；短于 `min_record`（0.3 秒）当咳嗽丢掉；
- **没有提示音**（看视频时很吵），状态全靠头顶气泡：
    在录 → 「我在听」；录完 → 「唔……我想想」；开口 → 回答正文（跟 TTS 一起出）
- 半双工：她说话时不听（没有回声消除，听自己的声音会自激）；
- `call` 模式：她说完自动再开一个 `call_window`（8 秒）的听音窗，没声就安静等着。

素材：麦克风 16k 单声道 → 能量 VAD → 9881 转写。
要不要看屏：话里带「这个/看看/屏幕上/我这样」这种指示词才顺手带一张当前帧
（默认不看，每次强制截屏要多花 2~5 秒）。
"""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import threading
import time
import urllib.request
import wave

log = logging.getLogger("petgw")

from phrases import HEARD_NOTHING, LISTENING, STUCK, THINKING, VOICE_OFF_HINT

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.03
VK_CONTROL = 0x11
VK_MENU = 0x12

# 话里出现这些词，就顺手看她一眼再回答
LOOK_HINTS = ("这个", "看看", "看一眼", "屏幕", "我这样", "我这样儿", "你看", "上面")

# 她头顶的话（「我在听」只是提示，不出声）
BUBBLE_LISTENING = LISTENING
BUBBLE_THINKING = THINKING


class HotkeyThread(threading.Thread):
    """用 RegisterHotKey 注册热键（而不是轮询 GetAsyncKeyState）。

    为什么不用轮询：游戏（原神这类）前台窗口的完整性级别比我们高，
    `GetAsyncKeyState` 在这种时候**读不到按键**（Windows 故意这么做的），
    而 RegisterHotKey 是系统级注册，游戏里也能收到 WM_HOTKEY。
    """

    HOTKEY_ID = 0x5056  # 'PV'

    def __init__(self, on_hotkey, vk: int = 0x56, mods: int = 0x0001 | 0x0002 | 0x4000):
        super().__init__(name="voice-hotkey", daemon=True)
        self.on_hotkey = on_hotkey
        self.vk = vk                  # 0x56 = V
        self.mods = mods              # Alt | Ctrl | NoRepeat
        self.ready = threading.Event()
        self.ok = False
        self.error = ""
        self.tid = 0

    def run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.ready.set()
            return
        user32 = ctypes.windll.user32
        self.tid = int(ctypes.windll.kernel32.GetCurrentThreadId())
        try:
            if not user32.RegisterHotKey(None, self.HOTKEY_ID, self.mods, self.vk):
                self.error = "RegisterHotKey 失败（多半被别的程序占了这个组合）"
                self.ready.set()
                return
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)
            self.ready.set()
            return
        self.ok = True
        self.ready.set()
        log.info("语音热键：已注册 Ctrl+Alt+V（系统级，游戏里也认）")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:      # WM_HOTKEY
                try:
                    self.on_hotkey()
                except Exception as exc:  # noqa: BLE001
                    log.warning("热键回调出错：%s", exc)
        try:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)
        except Exception:  # noqa: BLE001
            pass


def send_hotkey_for_test(vk: int = 0x56) -> int:
    """测试用：注入一次 Ctrl+Alt+<vk>（验证 RegisterHotKey 真能收到）。

    返回 SendInput 真塞进去的条数（0 = 没塞进去，结构体尺寸多半不对）。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    seq = ((0x11, False), (0x12, False), (vk, False), (vk, True), (0x12, True), (0x11, True))
    arr = (INPUT * len(seq))()
    for i, (code, up) in enumerate(seq):
        arr[i].type = 1
        arr[i].ki = KEYBDINPUT(code, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    return int(user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT)))


def _to_wav_bytes(pcm, rate: int = SAMPLE_RATE) -> bytes:
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


def _multipart_wav(wav_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----paopao" + os.urandom(8).hex()
    body = b""
    body += ("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n" % boundary).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
             "Content-Type: audio/wav\r\n\r\n" % boundary).encode()
    body += wav_bytes + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    return body, "multipart/form-data; boundary=" + boundary


class VoiceMode(threading.Thread):
    """麦克风录音 + VAD + 转写；真正“回答”的部分交给网关。"""

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="voice-mode", daemon=True)
        conf = dict(cfg.get("voice") or {})
        self.cfg = cfg
        self.conf = conf
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", True))
        self.mode = str(conf.get("mode", "ptt")).lower()      # off / ptt / call
        self.hotkey = str(conf.get("hotkey", "ctrl+alt+v")).lower()
        self.vad_silence = float(conf.get("vad_silence", 1.2))
        self.max_record = float(conf.get("max_record", 30.0))
        self.min_record = float(conf.get("min_record", 0.3))
        self.threshold = float(conf.get("energy_threshold", 0.012))
        self.call_window = float(conf.get("call_window", 8.0))
        self.asr_url = str(conf.get("asr_url") or "http://127.0.0.1:9881/v1/audio/transcriptions")
        self.barge_in = bool(conf.get("barge_in", False))
        self.no_speech_timeout = float(conf.get("no_speech_timeout", 3.0))
        self.device = conf.get("device", "")      # "" = 自动挑；也可以写设备序号或名字片段
        self.state = "idle"          # idle / listening / thinking / speaking
        self.hotkey_ok = False
        self.lock = threading.Lock()
        self.recording = threading.Event()
        self.cancel = threading.Event()
        self.last_text = ""
        self.last_error = ""
        self.turns = 0
        self._last_rms = 0.0
        self._listening_shown = False
        # 这一次「开口」的时间戳（第一个超过阈值的音频块）：语音问答要拿这一刻的屏幕
        self.last_onset_at = 0.0

    # ------------------------------------------------------------ 对外
    def start(self) -> None:
        if not self.enabled:
            log.info("语音模式：配置里关着")
            return
        super().start()
        log.info("语音模式：%s（热键 %s，静音 %.1fs 结束）", self.mode, self.hotkey, self.vad_silence)

    def begin(self) -> bool:
        """开始录一句（热键 / HTTP）。已经在录就取消；语音关着就不理。"""
        if self.mode == "off":
            # 按键说话被关掉 = 热键不干活，只提醒一句
            self._bubble(VOICE_OFF_HINT, 3200)
            return False
        with self.lock:
            if self.recording.is_set():
                self.cancel.set()
                self._hide_listening()
                self._hide_bubble()
                return False
            self.recording.set()
            self.cancel.clear()
            self._listening_shown = False
            self.state = "listening"
        # 不在开录时冒「我在听」：只有真检测到出声才冒，免得电话模式一直占着气泡
        log.info("语音：开始录（%s）", self.mode)
        return True

    def status(self) -> dict:
        return {"enabled": self.enabled, "mode": self.mode, "state": self.state,
                "hotkey": self.hotkey, "hotkey_ok": self.hotkey_ok, "turns": self.turns,
                "last_text": self.last_text[:80], "error": self.last_error}

    # ------------------------------------------------------------ 内部
    def _bubble(self, text: str, duration_ms: int = 0) -> None:
        try:
            bridge = getattr(self.gateway, "bubble", None)
            if bridge is not None:
                bridge.show(text, duration_ms or None)
        except Exception as exc:  # noqa: BLE001
            log.debug("气泡没弹出来：%s", exc)

    def _hide_bubble(self) -> None:
        try:
            bridge = getattr(self.gateway, "bubble", None)
            if bridge is not None:
                bridge.hide()
        except Exception:  # noqa: BLE001
            pass

    def _show_listening(self) -> None:
        """真的检测到出声才冒「我在听」，一直留到这一句说完。"""
        if self._listening_shown:
            return
        self._listening_shown = True
        self._bubble(BUBBLE_LISTENING, int(self.max_record * 1000))

    def _hide_listening(self) -> None:
        if not self._listening_shown:
            return
        self._listening_shown = False
        self._hide_bubble()

    def _set_state(self, state: str) -> None:
        with self.lock:
            self.state = state

    # ------------------------------------------------------------ 主循环
    def run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self.last_error = "sounddevice 不可用：%s" % exc
            log.warning("语音模式关闭：%s", self.last_error)
            return
        try:
            self._device_index, self._device_name = self._pick_device(sd)
            log.info("麦克风：%s（#%s）", self._device_name, self._device_index)
        except Exception as exc:  # noqa: BLE001
            self.last_error = "没有可用麦克风：%s" % exc
            log.warning("语音模式关闭：%s", self.last_error)
            return

        # 系统级热键：游戏（高完整性级别前台窗）里轮询读不到，必须用 RegisterHotKey
        self.hotkey_thread = HotkeyThread(self.begin)
        self.hotkey_thread.start()
        self.hotkey_thread.ready.wait(2.0)
        self.hotkey_ok = bool(self.hotkey_thread.ok)
        if not self.hotkey_ok:
            log.warning("系统热键注册失败（%s），退回按键轮询（游戏里可能不灵）",
                        self.hotkey_thread.error or "未知原因")

        hotkey_was_down = False
        if self.mode == "call":
            self.begin()          # 电话模式：进来就先开一个听音窗
        while True:
            time.sleep(0.05)
            # 热键：按一下开，再按一下取消
            try:
                down = self._hotkey_down()
                if down and not hotkey_was_down and not getattr(self, "hotkey_ok", False):
                    # 系统热键已注册就不靠轮询了（否则会一次按键触发两次）
                    self.begin()
                hotkey_was_down = down
            except Exception:  # noqa: BLE001
                pass
            if not self.recording.is_set():
                continue
            pcm = self._record_once(sd, np)
            if pcm is None and not self.device and getattr(self, "_last_blocks", 0) == 0:
                # 启动时选的设备可能已经拔掉/序号变了：这一句 0 块就换系统默认输入重录
                try:
                    default_idx = sd.default.device[0]
                    if default_idx is not None and int(default_idx) >= 0 \
                            and int(default_idx) != self._device_index:
                        old_idx = self._device_index
                        self._device_index = int(default_idx)
                        self._device_name = str(sd.query_devices(self._device_index)["name"])
                        log.info("语音：设备 #%s 没数据，换默认输入 #%s %s 重录",
                                 old_idx, self._device_index, self._device_name)
                        pcm = self._record_once(sd, np)
                except Exception as exc:  # noqa: BLE001
                    log.info("语音：换默认输入失败：%s", exc)
            self.recording.clear()
            if self.cancel.is_set() or pcm is None or len(pcm) / SAMPLE_RATE < self.min_record:
                self._set_state("idle")
                log.info("语音：这次没录到有效内容（%.2fs）", (len(pcm) if pcm is not None else 0) / SAMPLE_RATE)
                self._reopen_call()
                continue
            self._set_state("thinking")
            self._bubble(BUBBLE_THINKING, 8000)
            text = ""
            try:
                text = self._transcribe(pcm)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("语音转写失败：%s", exc)
            if not text.strip():
                self._bubble(HEARD_NOTHING, 3200)
                self._set_state("idle")
                self._reopen_call()
                continue
            self.last_text = text
            self.turns += 1
            log.info("语音：听到「%s」", text[:40])
            try:
                self.gateway.voice_reply(text)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("语音回答失败：%s", exc)
                self._bubble(STUCK, 3200)
            self._set_state("idle")
            if self.mode == "call":
                self._reopen_call()

    def _reopen_call(self) -> None:
        """电话模式：没听到/没转出来也继续听，不要停下来等热键。"""
        if self.mode != "call" or self.cancel.is_set():
            return
        time.sleep(0.25)
        if self.mode == "call" and not self.recording.is_set():
            self.begin()

    def _hotkey_down(self) -> bool:
        import ctypes

        u = ctypes.windll.user32
        ctrl = u.GetAsyncKeyState(VK_CONTROL) & 0x8000
        alt = u.GetAsyncKeyState(VK_MENU) & 0x8000
        key = u.GetAsyncKeyState(ord("V")) & 0x8000
        return bool(ctrl and alt and key)

    def _probe_device(self, sd, idx: int) -> int:
        """开 0.35 秒看看这个设备到底出不出数据；返回收到的采样数。"""
        got = [0]

        def cb(indata, frames, time_info, status):  # noqa: ANN001
            got[0] += len(indata)

        for rate in (SAMPLE_RATE, None):
            try:
                kw = {"samplerate": rate} if rate else {}
                with sd.InputStream(device=idx, channels=1, dtype="float32",
                                    blocksize=480, callback=cb, **kw):
                    time.sleep(0.35)
                return got[0]
            except Exception:  # noqa: BLE001
                continue
        return 0

    def _pick_device(self, sd):
        """挑一个真能出声的麦克风。

        别选「立体声混音/线路输入」（那是系统声音，不是人说话）。顺序：
        配置指定 → 名字像麦克风/耳机的 → 其它 → 最后才是不出声的那个。
        挑的时候顺手试 0.35 秒，能收到数据才算数（插孔没插麦的设备会一块都不给）。
        """
        want = self.device
        inputs = [(i, d) for i, d in enumerate(sd.query_devices())
                  if d.get("max_input_channels", 0) > 0]
        if not inputs:
            raise RuntimeError("系统里没有输入设备")
        if want not in ("", None):
            if str(want).isdigit():
                idx = int(want)
                for i, d in inputs:
                    if i == idx:
                        return idx, d["name"]
                raise RuntimeError("配置的输入设备 #%d 不存在" % idx)
            text = str(want).lower()
            for i, d in inputs:
                if text in str(d["name"]).lower():
                    return i, d["name"]
        # 优先跟系统默认输入：换耳机/拔插后设备序号会变，死守启动时的序号会录到 0 块
        try:
            default_idx = sd.default.device[0]
            if default_idx is not None and int(default_idx) >= 0:
                for i, d in inputs:
                    if i == int(default_idx):
                        got = self._probe_device(sd, i)
                        log.info("  试默认输入 #%d %s → %d 采样", i, d["name"], got)
                        if got > 0:
                            return i, d["name"]
                        break
        except Exception:  # noqa: BLE001
            pass
        bad = ("立体声混音", "stereo mix", "line input", "线路输入", "what u hear")
        good = ("麦克风", "microphone", "mic", "耳机", "headset", "airpods")

        def rank(item):
            i, d = item
            name = str(d["name"]).lower()
            if any(b in name for b in bad):
                return 2
            if any(g in name for g in good):
                return 0
            return 1

        for i, d in sorted(inputs, key=rank):
            if rank((i, d)) == 2:
                continue
            got = self._probe_device(sd, i)
            log.info("  试设备 #%d %s → %d 采样", i, d["name"], got)
            if got > 0:
                return i, d["name"]
        # 全都没数据：退到名字最像的那个，至少别选混音
        for i, d in sorted(inputs, key=rank):
            return i, d["name"]
        return inputs[0][0], inputs[0][1]

    def _record_once(self, sd, np):
        """录到「静音够久 / 到上限 / 被取消」为止，返回 float32 单声道。"""
        blocks: "queue.Queue" = queue.Queue()
        self._last_blocks = 0

        def cb(indata, frames, time_info, status):  # noqa: ANN001
            if self.cancel.is_set():
                return
            blocks.put(indata.copy().reshape(-1))

        frames = []
        heard = False
        silence = 0.0
        started = time.time()
        rate = SAMPLE_RATE
        try:
            # 有些设备不吃 16k（WASAPI 会直接拒），那就按它的默认采样率录，事后再降
            def _open(r):
                return sd.InputStream(samplerate=r, channels=1, dtype="float32",
                                      blocksize=int(r * BLOCK_SECONDS), callback=cb,
                                      device=self._device_index)

            try:
                stream = _open(SAMPLE_RATE)
                stream.start()
            except Exception:
                info = sd.query_devices(self._device_index)
                rate = int(info.get("default_samplerate") or 48000)
                stream = _open(rate)
                stream.start()
            try:
                while True:
                    if self.cancel.is_set():
                        return None
                    try:
                        block = blocks.get(timeout=0.5)
                    except queue.Empty:
                        if time.time() - started > self.max_record:
                            break
                        # 设备根本不出数据时也要收工，别干等 30 秒
                        if not heard and time.time() - started > (
                                self.call_window if self.mode == "call" else self.no_speech_timeout):
                            log.info("语音：没听到人声（收到 %d 块），收工", len(frames))
                            self._last_blocks = len(frames)
                            return None
                        continue
                    rms = float(np.sqrt((block ** 2).mean()))
                    self._last_rms = rms
                    if rms >= self.threshold:
                        if not heard:
                            self.last_onset_at = time.time()
                            self._show_listening()
                        heard = True
                        silence = 0.0
                    elif heard:
                        silence += BLOCK_SECONDS
                    frames.append(block)
                    if time.time() - started > self.max_record:
                        break
                    if heard and silence >= self.vad_silence:
                        break
                    # 一直没人说话：别干等到 30 秒（电话模式给 call_window，按键模式给 3 秒）
                    if not heard and time.time() - started > (
                            self.call_window if self.mode == "call" else self.no_speech_timeout):
                        log.info("语音：没听到人声，收工")
                        self._last_blocks = len(frames)
                        return None
            finally:
                self._hide_listening()
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("录音失败：%s", exc)
            return None
        if not frames:
            self._last_blocks = 0
            return None
        self._last_blocks = len(frames)
        pcm = np.concatenate(frames)
        if rate != SAMPLE_RATE and len(pcm) > 1:
            # 设备默认采样率不是 16k：线性插值降到 16k（够 ASR 用，不引额外依赖）
            n_out = int(len(pcm) * SAMPLE_RATE / float(rate))
            pcm = np.interp(
                np.linspace(0, len(pcm) - 1, n_out, dtype="float32"),
                np.arange(len(pcm), dtype="float32"),
                pcm,
            ).astype("float32")
            log.info("语音：设备 %dHz → 16000Hz", rate)
        return pcm

    def _transcribe(self, pcm) -> str:
        wav = _to_wav_bytes(pcm)
        body, ctype = _multipart_wav(wav)
        req = urllib.request.Request(self.asr_url, data=body,
                                     headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return str(data.get("text") or "").strip()
