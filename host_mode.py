# -*- coding: utf-8 -*-
"""直播弹幕搭子：独立于观看模式，和游戏/视频/番剧一起开。

弹幕只当输入源，不改 mode。回复由网关的 persona/LLM/TTS 管线出：
  * `host.speak=true` → 语音 + 气泡；
  * `host.speak=false` → 只气泡，不占 TTS。
每 `reply_interval_seconds` 开一个窗口，从最近 `message_ttl_seconds` 的弹幕里挑一条，
语音管线忙/老大在说话/上一轮还没完就继续等。

B站直播 WebSocket 实现（protover=2，zlib）；房间号留空时先按账号 mid 找一次。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import struct
import threading
import time
import urllib.parse
import urllib.request
import zlib

from voice_mode import HotkeyThread

log = logging.getLogger("petgw")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(os.environ.get("APPDATA", ""), "bili-cli", "credential.json")
OWNER_PATH = os.path.join(os.environ.get("APPDATA", ""), "bili-cli", "owner.json")
ROOM_API = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid=%s"
DANMU_API = ("https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
             "?id=%s&type=0")
CONF_API = ("https://api.live.bilibili.com/room/v1/Danmu/getConf"
            "?room_id=%s&platform=pc&player=web")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
URL_RE = re.compile(r"https?://|www\.", re.I)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _credential() -> tuple[dict, str, str]:
    c = _read_json(CRED_PATH)
    names = {"sessdata": "SESSDATA", "bili_jct": "bili_jct",
             "buvid3": "buvid3", "buvid4": "buvid4",
             "dedeuserid": "DedeUserID"}
        # 有些版本用小写 key
    cookie = "; ".join("%s=%s" % (names[k], c[k]) for k in names if c.get(k))
    uid = str(c.get("dedeuserid") or c.get("DedeUserID") or "")
    return c, cookie, uid


def _resolve_room(room_id: int) -> tuple[int, str]:
    """房间号：配置优先；没配就按账号 mid（再退 owner.json）找。"""
    if int(room_id or 0) > 0:
        return int(room_id), ""
    _, cookie, uid = _credential()
    mids = [uid]
    owner = _read_json(OWNER_PATH)
    if owner.get("mid"):
        mids.append(str(owner["mid"]))
    for mid in [m for m in mids if m]:
        try:
            req = urllib.request.Request(
                ROOM_API % urllib.parse.quote(mid),
                headers={"Cookie": cookie, "User-Agent": UA,
                         "Referer": "https://live.bilibili.com/"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            rid = int(((data.get("data") or {}).get("roomid") or 0))
            if rid:
                return rid, ""
        except Exception as exc:  # noqa: BLE001
            log.info("直播：查房间号失败（mid=%s）：%s", mid, exc)
    return 0, "没找到直播间房间号，请在 config.host.room_id 里填"


class _Packet:
    @staticmethod
    def pack(op: int, body: bytes) -> bytes:
        return struct.pack(">IHHII", 16 + len(body), 16, 1, op, 1) + body


class DanmakuClient(threading.Thread):
    """一条 B站直播弹幕连接；断了自动重连，host.enabled 关了就不连。"""

    def __init__(self, host: "HostMode"):
        super().__init__(name="danmaku-client", daemon=True)
        self.host = host
        self.ws = None
        self.connected = False
        self.last_error = ""

    def _connect(self, room_id: int) -> bool:
        _, cookie, uid = _credential()
        token = ""
        hosts = []
        # 新版 getDanmuInfo 偶尔 -352（风控）；旧版 getConf 还能用，做兜底
        for api in (DANMU_API, CONF_API):
            try:
                req = urllib.request.Request(
                    api % room_id,
                    headers={"Cookie": cookie, "User-Agent": UA,
                             "Referer": "https://live.bilibili.com/%d" % room_id,
                             "Origin": "https://live.bilibili.com"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                body = data.get("data") or {}
                token = str(body.get("token") or "")
                hosts = body.get("host_list") or body.get("host_server_list") or []
                if token and hosts:
                    break
            except Exception as exc:  # noqa: BLE001
                log.info("直播：取弹幕服务器失败（%s）：%s", api.split("?")[0].rsplit("/", 1)[-1], exc)
        if not token or not hosts:
            raise RuntimeError("getDanmuInfo/getConf 没给 token/host")
        last = None
        for h in hosts[:3]:
            url = "wss://%s:%s/sub" % (h.get("host"), h.get("wss_port") or 443)
            try:
                import websocket

                self.ws = websocket.create_connection(
                    url, timeout=10,
                    header=["User-Agent: " + UA,
                            "Origin: https://live.bilibili.com"],
                )
                self.ws.settimeout(1.0)
                auth = json.dumps({
                    "uid": int(uid or 0), "roomid": int(room_id),
                    "protover": 2, "platform": "web", "type": 2, "key": token,
                }).encode("utf-8")
                self.ws.send_binary(_Packet.pack(7, auth))
                self.connected = False
                self.host.connected = False
                log.info("直播弹幕：已连上房间 %s，等鉴权（%s）", room_id, url)
                return True
            except Exception as exc:  # noqa: BLE001
                last = exc
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None
        raise RuntimeError("连不上弹幕服务器：%s" % last)

    def _parse(self, raw: bytes) -> None:
        if len(raw) < 16:
            return
        total, hlen, ver, op, _seq = struct.unpack(">IHHII", raw[:16])
        body = raw[hlen:total]
        if ver == 2:
            try:
                body = zlib.decompress(body)
            except zlib.error:
                return
        elif ver == 3:
            # 我们 auth 的是 protover=2；真收到 3 就跳过，别把二进制当 JSON
            return
        if op == 8:
            try:
                obj = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return
            if int(obj.get("code") or 0) == 0:
                self.connected = True
                self.host.connected = True
                self.host.last_error = ""
                log.info("直播弹幕：鉴权成功，开始收弹幕")
            else:
                self.connected = False
                self.host.connected = False
                self.host.last_error = "弹幕鉴权失败：%s" % obj.get("code")
                log.warning("直播弹幕鉴权失败：%s", obj)
            return
        if op != 5:
            return
        text = body.decode("utf-8", "replace")
        dec = json.JSONDecoder()
        idx = 0
        while idx < len(text):
            try:
                obj, end = dec.raw_decode(text, idx)
            except ValueError:
                break
            idx = end
            cmd = str(obj.get("cmd") or "")
            if cmd.startswith("DANMU_MSG"):
                info = obj.get("info") or []
                try:
                    msg = str(info[1] or "").strip()
                    user = str((info[2] or [])[1] or "").strip()
                    uid = str((info[2] or [])[0] or "")
                except (IndexError, TypeError):
                    continue
                self.host.push(user, uid, msg, 10)
            elif cmd == "SUPER_CHAT_MESSAGE":
                data = obj.get("data") or {}
                user = str(((data.get("user_info") or {}).get("uname")) or "")
                uid = str(data.get("uid") or "")
                msg = str(data.get("message") or "").strip()
                self.host.push(user, uid, msg, 100)
            elif cmd == "SEND_GIFT":
                data = obj.get("data") or {}
                user = str(data.get("uname") or "")
                uid = str(data.get("uid") or "")
                gift = str(data.get("giftName") or "礼物")
                num = int(data.get("num") or 1)
                self.host.push(user, uid, "送出了 %s×%d" % (gift, num), 60)

    def run(self) -> None:
        backoff = 3.0
        while True:
            if not self.host.enabled:
                self.connected = False
                time.sleep(1.0)
                continue
            room_id, err = _resolve_room(self.host.room_id)
            if not room_id:
                self.last_error = err
                self.host.last_error = err
                time.sleep(5.0)
                continue
            self.host.room_id = room_id
            try:
                self._connect(room_id)
                self.host.last_error = ""
                backoff = 3.0
                beat = 0.0
                while self.host.enabled:
                    try:
                        raw = self.ws.recv()
                    except Exception as exc:  # noqa: BLE001
                        # timeout 是正常的，用来发心跳
                        if "timed out" not in str(exc).lower():
                            raise
                        raw = b""
                    if raw:
                        if isinstance(raw, str):
                            raw = raw.encode("utf-8", "replace")
                        self._parse(raw)
                    if time.time() - beat > 25:
                        beat = time.time()
                        self.ws.send_binary(_Packet.pack(2, b""))
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.host.connected = False
                self.last_error = str(exc)[:120]
                self.host.last_error = self.last_error
                log.info("直播弹幕断开：%s（%.0fs 后重连）", self.last_error, backoff)
                time.sleep(backoff)
                backoff = min(30.0, backoff * 1.6)
            finally:
                try:
                    if self.ws:
                        self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None


class HostMode(threading.Thread):
    """弹幕队列 + 定时窗口 + 出声开关；和观看模式完全独立。"""

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="host-mode", daemon=True)
        conf = dict(cfg.get("host") or {})
        self.cfg = cfg
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", False))
        self.speak = bool(conf.get("speak", False))
        self.room_id = int(conf.get("room_id") or 0)
        self.interval = max(10.0, float(conf.get("reply_interval_seconds", 25)))
        self.ttl = max(5.0, float(conf.get("message_ttl_seconds", 20)))
        self.max_queue = max(5, int(conf.get("max_queue", 30)))
        self.lock = threading.Lock()
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=self.max_queue)
        self.seen: dict[str, float] = {}
        self.busy = False
        self.connected = False
        self.last_reply_at = 0.0
        self.last_text = ""
        self.last_error = ""
        self.client = DanmakuClient(self)
        self._self_uid = ""

    # ---------------------------------------------------------- 开关
    def start_all(self) -> None:
        _, _, self._self_uid = _credential()
        self.start()
        self.client.start()
        self._start_hotkey()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        self.cfg.setdefault("host", {})["enabled"] = self.enabled
        if not on:
            self._clear()
            self.connected = False
            try:
                if self.client.ws:
                    self.client.ws.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("直播弹幕：%s", "开" if self.enabled else "关")

    def set_speak(self, on: bool) -> None:
        self.speak = bool(on)
        self.cfg.setdefault("host", {})["speak"] = self.speak
        log.info("回弹幕出声：%s", "开" if self.speak else "关")
        try:
            self.gateway.bubble.show("回弹幕出声：%s" % ("开" if self.speak else "关"), 2200)
        except Exception:  # noqa: BLE001
            pass

    def set_room(self, room_id: int) -> None:
        self.room_id = int(room_id or 0)
        self.cfg.setdefault("host", {})["room_id"] = self.room_id

    def status(self) -> dict:
        return {"enabled": self.enabled, "speak": self.speak,
                "room_id": self.room_id, "connected": self.connected,
                "queued": self.q.qsize(), "busy": self.busy,
                "last": self.last_text[:80], "error": self.last_error}

    # ---------------------------------------------------------- 弹幕
    def _clear(self) -> None:
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def push(self, user: str, uid: str, text: str, priority: int) -> None:
        if not self.enabled:
            return
        text = (text or "").strip()
        if len(text) < 2 or len(text) > 120:
            return
        if URL_RE.search(text) or text.startswith(("!", "/", "！")):
            return
        # 不按 uid 过滤自己：现在只读不发，自己发的测试弹幕也应该能回；
        # 以后真做自动发弹幕，再单独按“刚发出去的那条”去重。
        key = "%s|%s" % (uid or user, text[:40])
        now = time.time()
        with self.lock:
            if now - self.seen.get(key, 0) < 60:
                return
            self.seen[key] = now
            if len(self.seen) > 400:
                self.seen = {k: v for k, v in self.seen.items() if now - v < 600}
        # 问题/点名/礼物优先
        score = priority
        if "?" in text or "？" in text:
            score += 8
        if "泡泡" in text or "@" in text:
            score += 6
        item = {"ts": now, "user": user or "观众", "uid": uid,
                "text": text, "score": score}
        log.info("直播弹幕收到：%s：%s", item["user"][:20], text[:40])
        try:
            self.q.put_nowait(item)
        except queue.Full:
            # 满了丢分最低的：简单粗暴，但直播弹幕本来就不能全回
            try:
                old = self.q.get_nowait()
                if old.get("score", 0) >= score:
                    self.q.put_nowait(old)
                    return
                self.q.put_nowait(item)
            except Exception:  # noqa: BLE001
                pass

    def _pick(self) -> dict | None:
        now = time.time()
        items = []
        while True:
            try:
                it = self.q.get_nowait()
            except queue.Empty:
                break
            if now - it["ts"] <= self.ttl:
                items.append(it)
        if not items:
            return None
        items.sort(key=lambda x: (x["score"], x["ts"]), reverse=True)
        for it in items[1:]:
            try:
                self.q.put_nowait(it)
            except queue.Full:
                break
        return items[0]

    # ---------------------------------------------------------- 调度
    def _can_speak(self) -> bool:
        speech = getattr(self.gateway, "speech", None)
        voice = getattr(self.gateway, "voice", None)
        if speech is not None and speech.busy():
            return False
        if getattr(self.gateway, "turn_active", False):
            return False
        if voice is not None and voice.is_speaking():
            return False
        return True

    def run(self) -> None:
        while True:
            time.sleep(1.0)
            if not self.enabled or self.busy:
                continue
            if time.time() - self.last_reply_at < self.interval:
                continue
            if not self._can_speak():
                continue
            item = self._pick()
            if not item:
                continue
            self.busy = True
            try:
                reply = self.gateway.host_reply(item, self._context(), self.speak)
                if reply:
                    self.last_text = reply
                self.last_reply_at = time.time()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)[:120]
                log.warning("直播弹幕回复失败：%s", exc)
            finally:
                self.busy = False

    def _context(self) -> str:
        from video_watch import foreground

        try:
            app, title = foreground()
        except Exception:  # noqa: BLE001
            app, title = "", ""
        mode = self.gateway.mode.get()
        bits = ["当前模式：%s" % mode]
        if app or title:
            bits.append("前台：%s | %s" % (app or "?", (title or "?")[:80]))
        try:
            snap = self.gateway.player.snapshot() if self.gateway.player else {}
            if snap and snap.get("visible") is not False:
                bits.append("正在看：%s" % (snap.get("title") or "")[:80])
        except Exception:  # noqa: BLE001
            pass
        # 直播模式没有 B站字幕接口，靠环回转写；把主播刚说的带进上下文，
        # 不然回弹幕时她只会说“听不见”。
        try:
            video = getattr(self.gateway, "video", None)
            if video is not None and getattr(video, "active", False):
                lines = [str(x[2]) for x in list(video.transcript)[-6:] if str(x[2]).strip()]
                if lines:
                    bits.append("主播刚说：\n" + "\n".join(x[:80] for x in lines))
        except Exception:  # noqa: BLE001
            pass
        recent = []
        for it in list(self.q.queue)[-8:]:
            recent.append("%s：%s" % (it["user"], it["text"][:50]))
        if recent:
            bits.append("最近弹幕：\n" + "\n".join(recent))
        return "\n".join(bits)

    # ---------------------------------------------------------- 快捷键
    def _start_hotkey(self) -> None:
        """Ctrl+Alt+B：弹幕出声开关（和语音电话的 Ctrl+Alt+V 不冲突）。"""
        def _toggle():
            self.set_speak(not self.speak)
            try:
                self.gateway.save_host_config()
            except Exception:  # noqa: BLE001
                pass

        try:
            self.hotkey = HotkeyThread(_toggle, vk=0x42, hotkey_id=0x484F,
                                       label="回弹幕出声")  # B
            self.hotkey.start()
            self.hotkey.ready.wait(2.0)
            if not self.hotkey.ok:
                log.warning("直播弹幕快捷键注册失败：%s", self.hotkey.error or "未知")
        except Exception as exc:  # noqa: BLE001
            log.info("直播弹幕快捷键没起来（不影响托盘）：%s", exc)
