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

import base64
import collections
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
import phrases

log = logging.getLogger("petgw")


def _pb_fields(data: bytes) -> list:
    """极简 protobuf 顶层解析：返回 [(字段号, 值)]，值是 int 或 bytes。

    只为解 B站 `INTERACT_WORD_V2` 的 `data.pb`（进场事件现在多半走这个），
    不追求完整实现——解不出来的字段丢掉就行。
    """
    fields = []
    i, n = 0, len(data)

    def varint(pos: int) -> tuple[int, int]:
        shift = 0
        val = 0
        while pos < n:
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            if not b & 0x80:
                return val, pos
            shift += 7
            if shift > 63:
                raise ValueError("varint 太长")
        raise ValueError("数据断了")

    while i < n:
        key, i = varint(i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            val, i = varint(i)
            fields.append((num, val))
        elif wt == 2:
            ln, i = varint(i)
            fields.append((num, data[i:i + ln]))
            i += ln
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            break
    return fields


def _pb_name(fields: list) -> str:
    """在 pb 顶层找一个像用户名的 utf-8 字符串。"""
    for _num, val in fields:
        if not isinstance(val, bytes) or not (1 <= len(val) <= 24):
            continue
        try:
            s = val.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if any(ord(c) < 32 for c in s):
            continue
        s = s.strip()
        if s:
            return s
    return ""


def _pb_msg_type(fields: list) -> int:
    """进场事件类型：1 进场 2 关注 3 分享 4 特别关注 5 互粉 6 粉丝团。"""
    for num, val in fields:
        if num == 5 and isinstance(val, int) and 1 <= val <= 6:
            return val
    for _num, val in fields:
        if isinstance(val, int) and 1 <= val <= 6:
            return val
    return 0

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(os.environ.get("APPDATA", ""), "bili-cli", "credential.json")
OWNER_PATH = os.path.join(os.environ.get("APPDATA", ""), "bili-cli", "owner.json")
ROOM_API = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid=%s"
LIVE_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info?room_id=%s"
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


def _room_live_status(room_id: int) -> int | None:
    """直播间状态：0 没开播 / 1 直播中 / 2 轮播；查不到返回 None。

    2026-11-18 加：没开播就别连弹幕。连上去也是一条空流（还会被服务端踢），
    白占连接、白刷日志，老大也分不清「没开播」和「坏了」。
    """
    if int(room_id or 0) <= 0:
        return None
    try:
        req = urllib.request.Request(
            LIVE_INFO_API % room_id,
            headers={"User-Agent": UA,
                     "Referer": "https://live.bilibili.com/%d" % room_id},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        status = (data.get("data") or {}).get("live_status")
        return int(status) if status is not None else None
    except Exception as exc:  # noqa: BLE001
        log.info("直播弹幕：查开播状态失败（%s）", exc)
        return None


class DanmakuClient(threading.Thread):
    """一条 B站直播弹幕连接；断了自动重连，host.enabled 关了就不连。"""

    def __init__(self, host: "HostMode", on_message=None):
        super().__init__(name="danmaku-client", daemon=True)
        self.host = host
        # 收弹幕的去处：默认丢给 HostMode.push（回弹幕那条路）；
        # 「看直播模式」那条会传自己的收集器进来（2026-11-19）。
        self.on_message = on_message
        # 旁听者：HostMode 自己连着的时候，「看直播模式」不再另开一条 WS，
        # 挂到这里一起收（否则同一个直播间会开两条连接）。
        self.sinks: list = []
        self.ws = None
        self.connected = False
        self.last_error = ""
        self._live_state: int | None = None   # 上次查到的开播状态，只用来少刷日志
        self._pb_logged = 0                   # 进房事件（V2）调试日志打几条就够
        self._odd_cmds: dict[str, float] = {}  # 没见过的事件名，每种每分钟记一条

    def add_sink(self, fn) -> None:
        """挂个旁听回调（和回弹幕共用同一条连接时用）。"""
        if fn not in self.sinks:
            self.sinks.append(fn)

    def _emit(self, user: str, uid: str, text: str, priority: int) -> None:
        for fn in list(self.sinks):
            try:
                fn(user, uid, text, priority)
            except Exception:  # noqa: BLE001
                pass
        if self.on_message is not None:
            try:
                self.on_message(user, uid, text, priority)
            except Exception:  # noqa: BLE001
                pass
            return
        self.host.push(user, uid, text, priority)

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
                    "protover": 3, "platform": "web", "type": 2, "key": token,
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
            try:
                import brotli

                body = brotli.decompress(body)
            except Exception as exc:  # noqa: BLE001
                log.info("直播弹幕：brotli 解包失败（%s）", exc)
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
        # protover=3 的 op=5 body 前面常带一小段二进制帧头，不能从 0 直接
        # raw_decode；从第一个 { 开始找，后面还可能跟多条 JSON。
        idx = text.find("{")
        while idx >= 0:
            try:
                obj, end = dec.raw_decode(text, idx)
            except ValueError:
                idx = text.find("{", idx + 1)
                continue
            idx = text.find("{", end)
            cmd = str(obj.get("cmd") or "")
            if cmd.startswith("DANMU_MSG"):
                info = obj.get("info") or []
                try:
                    msg = str(info[1] or "").strip()
                    user = str((info[2] or [])[1] or "").strip()
                    uid = str((info[2] or [])[0] or "")
                except (IndexError, TypeError):
                    continue
                self._emit(user, uid, msg, 10)
            elif cmd == "SUPER_CHAT_MESSAGE":
                data = obj.get("data") or {}
                user = str(((data.get("user_info") or {}).get("uname")) or "")
                uid = str(data.get("uid") or "")
                msg = str(data.get("message") or "").strip()
                self._emit(user, uid, msg, 100)
            elif cmd == "SEND_GIFT":
                data = obj.get("data") or {}
                user = str(data.get("uname") or "")
                uid = str(data.get("uid") or "")
                gift = str(data.get("giftName") or "礼物")
                try:
                    num = int(data.get("num") or 1)
                except (TypeError, ValueError):
                    num = 1
                # 礼物价值：B站 100 金瓜子 = 1 电池；银瓜子（免费礼物）算 0。
                try:
                    total_coin = float(data.get("total_coin") or 0)
                except (TypeError, ValueError):
                    total_coin = 0.0
                if total_coin <= 0:
                    try:
                        total_coin = float(data.get("price") or 0) * num
                    except (TypeError, ValueError):
                        total_coin = 0.0
                coin = str(data.get("coin_type") or "").lower()
                battery = (total_coin / 100.0) if coin != "silver" else 0.0
                # 礼物单列：先进「合并缓冲」，攒一小会儿再一起谢（频繁送礼物不刷屏）
                gift_hook = getattr(self.host, "note_gift", None)
                if callable(gift_hook):
                    gift_hook(user, uid, gift, num, battery)
                self._emit(user, uid, "送出了 %s×%d" % (gift, num), 60)
            elif cmd.startswith("INTERACT_WORD"):
                # 进房/关注/分享：msg_type 1=进场 2=关注 3=分享 4=特别关注
                # 新版 `INTERACT_WORD_V2` 的 data 只有一段 pb（base64），
                # uname/uid/msg_type 都得自己解；V1 是明文字段，两种都认。
                data = obj.get("data") or {}
                user = str(data.get("uname") or "").strip()
                uid = str(data.get("uid") or "")
                try:
                    mtype = int(data.get("msg_type") or 0)
                except (TypeError, ValueError):
                    mtype = 0
                if (not user or not mtype) and data.get("pb"):
                    try:
                        fields = _pb_fields(base64.b64decode(str(data["pb"])))
                        user = user or _pb_name(fields)
                        if not uid:
                            uid = str(next((v for k, v in fields
                                            if k == 1 and isinstance(v, int)), ""))
                        mtype = mtype or _pb_msg_type(fields)
                        if self._pb_logged < 3:
                            self._pb_logged += 1
                            log.info("进房事件（V2）：%s｜uid=%s｜类型=%s｜字段=%s",
                                     user or "?", uid or "?",
                                     mtype, [(k, v if isinstance(v, int) else len(v))
                                             for k, v in fields[:10]])
                    except Exception as exc:  # noqa: BLE001
                        log.info("进房事件解不开（%s）：%s", cmd, exc)
                if mtype == 1 and user:
                    # 这条连接可能是「看直播」那个旁听客户端（LiveDanmaku），
                    # 它没有 note_enter——不能直接调，否则一有进房就断线重连。
                    enter = getattr(self.host, "note_enter", None)
                    if callable(enter):
                        enter(user, uid)
                    else:
                        log.info("进房事件没处送（这条连接没有 note_enter）：%s", user)
                elif mtype in (2, 4, 5) and user:
                    # 关注 / 特别关注 / 互粉 → 感谢关注（2026-11-22 老大要的）
                    thanks = getattr(self.host, "note_thanks", None)
                    if callable(thanks):
                        thanks("follow", user, uid)
                elif mtype == 6 and user:
                    # 多出来的 6 号（粉丝团一类）→ 感谢粉丝团
                    thanks = getattr(self.host, "note_thanks", None)
                    if callable(thanks):
                        thanks("fans", user, uid)
                elif self._pb_logged < 12:
                    self._pb_logged += 1
                    log.info("进房事件但不是「进场」：%s｜名字=%r｜类型=%s｜data 键=%s",
                             cmd, user, mtype, list(data.keys())[:8])
            elif cmd == "GUARD_BUY":
                # 上舰单独一类（2026-11-22 老大要的）：舰长/提督/总督分开谢
                data = obj.get("data") or {}
                user = str(data.get("username") or data.get("uname") or "").strip()
                uid = str(data.get("uid") or "")
                try:
                    level = int(data.get("guard_level") or 0)
                except (TypeError, ValueError):
                    level = 0
                if user:
                    guard = getattr(self.host, "note_guard", None)
                    if callable(guard):
                        guard(user, uid, level)
                    if self._pb_logged < 6:
                        self._pb_logged += 1
                        log.info("上舰事件：%s｜%s｜guard_level=%s", cmd, user, level)
            elif cmd == "USER_TOAST_MSG":
                # 上舰/续费提示：guard_level 有值才算上舰；粉丝团那类（level=0）才走粉丝团
                data = obj.get("data") or {}
                user = str(data.get("username") or data.get("uname") or "").strip()
                uid = str(data.get("uid") or "")
                try:
                    level = int(data.get("guard_level") or 0)
                except (TypeError, ValueError):
                    level = 0
                if user and level:
                    guard = getattr(self.host, "note_guard", None)
                    if callable(guard):
                        guard(user, uid, level)
                elif user:
                    thanks = getattr(self.host, "note_thanks", None)
                    if callable(thanks):
                        thanks("fans", user, uid)
            elif cmd and ("INTERACT" in cmd.upper() or "ENTRY" in cmd.upper()
                          or "WATCHED" in cmd.upper()):
                # 别的事件名字（B站 会改）：每种每分钟记一条，方便对着改解析
                if time.time() - self._odd_cmds.get(cmd, 0) > 60:
                    self._odd_cmds[cmd] = time.time()
                    log.info("直播事件（没专门处理）：%s｜data 键=%s", cmd,
                             list((obj.get("data") or {}).keys())[:8])

    def run(self) -> None:
        backoff = 3.0
        while True:
            if not self.host.enabled:
                self.connected = False
                time.sleep(1.0)
                continue
            # 回弹幕只钉「自己的直播间」：room_id=0 时按登录账号自动找（2026-11-19 回退：
            # 之前一版跟着「正在看的直播间」走，结果跑去回别人直播间的弹幕，是错的）
            room_id, err = _resolve_room(self.host.room_id)
            if not room_id:
                self.last_error = err
                self.host.last_error = err
                time.sleep(5.0)
                continue
            self.host.room_id = room_id
            # 没开播就不连：连上去只有空流，还会被服务端踢来踢去。
            # live_status：0 没开播、1 直播中、2 轮播（循环播放，不是真开播）。
            live = _room_live_status(room_id)
            if live is not None and live != 1:
                self.connected = False
                self.host.connected = False
                self.host.last_error = "直播间没开播（状态 %s）" % live
                if self._live_state != live:
                    self._live_state = live
                    log.info("直播弹幕：房间 %s 没开播（live_status=%s），先不连（60 秒后再看）",
                             room_id, live)
                time.sleep(60.0)
                continue
            if live is not None:
                self._live_state = live
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
        # 欢迎进场（2026-11-22 老大要的）：有人进直播间就欢迎一句——
        # 可以是纯欢迎、顺手问一句，或者介绍一下自己/我们正在干嘛。
        self.welcome = bool(conf.get("welcome", True))
        self.welcome_gap = max(8.0, float(conf.get("welcome_min_gap", 20)))
        # 只迎「刚进门」的人：进来超过这么多秒就不迎了。
        # 2026-11-22 老大说「没人进还一直在欢迎」——以前是攒着、45 秒迎一个，
        # 迎的是半分钟前进来的人，看着就像凭空在欢迎。现在只迎 8 秒内的。
        self.welcome_max_age = max(3.0, float(conf.get("welcome_max_age", 15)))
        self.welcome_style = ""          # 空 = 让她自己决定怎么说
        self.last_welcome_at = 0.0
        self.welcome_queue: "queue.Queue[dict]" = queue.Queue(maxsize=20)
        self._welcomed: dict[str, float] = {}
        # 感谢关注 / 感谢加粉丝团（2026-11-22 老大要的，照欢迎那套做）
        self.thanks_follow = bool(conf.get("thanks_follow", True))
        self.thanks_fans = bool(conf.get("thanks_fans", True))
        # 上舰单列（舰长/提督/总督），不再混进粉丝团
        self.thanks_guard = bool(conf.get("thanks_guard", True))
        # 礼物感谢：可设上限（每分钟最多谢几次），频繁送就合并成一条
        self.thanks_gift = bool(conf.get("thanks_gift", True))
        self.gift_merge_seconds = max(3.0, float(conf.get("gift_merge_seconds", 15)))
        self.gift_max_per_minute = max(1, int(conf.get("gift_max_per_minute", 6)))
        # 礼物门槛（电池）：低于这个价值的不谢。100 金瓜子 = 1 电池，10 电池 = 1 元。
        self.gift_min_value = max(0.0, float(conf.get("gift_min_value", 10)))
        self._gift_pending: dict[str, dict] = {}
        self._gift_sent: "collections.deque" = collections.deque(maxlen=200)
        self.thanks_gap = max(5.0, float(conf.get("thanks_min_gap", 10)))
        self.thanks_max_age = max(3.0, float(conf.get("thanks_max_age", 25)))
        self.last_thanks_at = 0.0
        self.thanks_queue: "queue.Queue[dict]" = queue.Queue(maxsize=20)
        self._thanked: dict[str, float] = {}

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
            self.last_error = ""
            try:
                if self.client.ws:
                    self.client.ws.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("直播弹幕：%s", "开" if self.enabled else "关")
        text = phrases.TRAY_FEEDBACK["host_on" if self.enabled else "host_off"]
        try:
            self.gateway.bubble.show(text, 3200)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.gateway.speech.say(text, tag="mode")
        except Exception:  # noqa: BLE001
            pass

    def set_speak(self, on: bool) -> None:
        self.speak = bool(on)
        self.cfg.setdefault("host", {})["speak"] = self.speak
        log.info("回弹幕出声：%s", "开" if self.speak else "关")
        text = phrases.TRAY_FEEDBACK["host_speak_on" if self.speak else "host_speak_off"]
        try:
            self.gateway.bubble.show(text, 3200)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.gateway.speech.say(text, tag="mode")
        except Exception:  # noqa: BLE001
            pass

    def set_welcome(self, on: bool) -> None:
        """欢迎进场开关（托盘「直播设置」里那一项）。"""
        self.welcome = bool(on)
        self.cfg.setdefault("host", {})["welcome"] = self.welcome
        log.info("欢迎进场：%s", "开" if self.welcome else "关")
        text = phrases.TRAY_FEEDBACK["host_welcome_on" if self.welcome else "host_welcome_off"]
        try:
            self.gateway.bubble.show(text, 3200)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.gateway.speech.say(text, tag="mode")
        except Exception:  # noqa: BLE001
            pass

    def _set_thanks(self, kind: str, on: bool) -> None:
        """感谢关注 / 粉丝团 / 上舰 / 礼物 的开关（照欢迎那套：气泡 + 出声一句反馈）。"""
        if kind == "follow":
            self.thanks_follow = bool(on)
            self.cfg.setdefault("host", {})["thanks_follow"] = self.thanks_follow
            on_now = self.thanks_follow
            text = phrases.TRAY_FEEDBACK[
                "thanks_follow_on" if on_now else "thanks_follow_off"]
            log.info("感谢关注：%s", "开" if on_now else "关")
        elif kind == "guard":
            self.thanks_guard = bool(on)
            self.cfg.setdefault("host", {})["thanks_guard"] = self.thanks_guard
            on_now = self.thanks_guard
            text = phrases.TRAY_FEEDBACK[
                "thanks_guard_on" if on_now else "thanks_guard_off"]
            log.info("感谢上舰：%s", "开" if on_now else "关")
        elif kind == "gift":
            self.thanks_gift = bool(on)
            self.cfg.setdefault("host", {})["thanks_gift"] = self.thanks_gift
            on_now = self.thanks_gift
            text = phrases.TRAY_FEEDBACK[
                "thanks_gift_on" if on_now else "thanks_gift_off"]
            log.info("感谢礼物：%s", "开" if on_now else "关")
        else:
            self.thanks_fans = bool(on)
            self.cfg.setdefault("host", {})["thanks_fans"] = self.thanks_fans
            on_now = self.thanks_fans
            text = phrases.TRAY_FEEDBACK[
                "thanks_fans_on" if on_now else "thanks_fans_off"]
            log.info("感谢加粉丝团：%s", "开" if on_now else "关")
        try:
            self.gateway.bubble.show(text, 3200)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.gateway.speech.say(text, tag="mode")
        except Exception:  # noqa: BLE001
            pass

    def set_thanks_follow(self, on: bool) -> None:
        self._set_thanks("follow", on)

    def set_thanks_fans(self, on: bool) -> None:
        self._set_thanks("fans", on)

    def set_thanks_guard(self, on: bool) -> None:
        self._set_thanks("guard", on)

    def set_thanks_gift(self, on: bool) -> None:
        self._set_thanks("gift", on)

    def set_gift_min_value(self, value: float) -> None:
        """礼物门槛（电池）：低于这个价值的不谢。托盘「礼物门槛」里改。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        self.gift_min_value = max(0.0, min(100000.0, v))
        self.cfg.setdefault("host", {})["gift_min_value"] = self.gift_min_value
        log.info("礼物门槛：%g 电池", self.gift_min_value)
        text = "礼物门槛改成了 %g 电池。" % self.gift_min_value
        try:
            self.gateway.bubble.show(text, 3200)
        except Exception:  # noqa: BLE001
            pass

    def set_room(self, room_id: int) -> None:
        """换房间：断开旧连接，客户端下一轮按新 room_id 重连（并写回 config.json）。"""
        self.room_id = int(room_id or 0)
        self.cfg.setdefault("host", {})["room_id"] = self.room_id
        self.connected = False
        try:
            import gateway as _gw
            _gw.save_config_section("host", self.cfg["host"])
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.client.ws:
                self.client.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {"enabled": self.enabled, "speak": self.speak,
                "welcome": self.welcome, "welcome_queued": self.welcome_queue.qsize(),
                "thanks_follow": self.thanks_follow,
                "thanks_fans": self.thanks_fans,
                "thanks_guard": self.thanks_guard,
                "thanks_gift": self.thanks_gift,
                "gift_min_value": self.gift_min_value,
                "gift_merge_seconds": self.gift_merge_seconds,
                "gift_max_per_minute": self.gift_max_per_minute,
                "thanks_queued": self.thanks_queue.qsize(),
                "room_id": self.room_id, "connected": self.connected,
                "queued": self.q.qsize(), "busy": self.busy,
                "live_status": self.client._live_state,
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

    # ---------------------------------------------------------- 欢迎进场
    def note_enter(self, user: str, uid: str, source: str = "") -> None:
        """有人进直播间：排一条欢迎（限频，同一个人 15 分钟内只迎一次）。"""
        if not self.enabled or not self.welcome:
            return
        name = (user or "").strip()
        if not name or len(name) > 24:
            return
        uid = str(uid or "")
        if uid and self._self_uid and uid == self._self_uid:
            return
        now = time.time()
        key = uid or name
        with self.lock:
            if now - self._welcomed.get(key, 0) < 900:
                return
            self._welcomed[key] = now
            if len(self._welcomed) > 800:
                self._welcomed = {k: v for k, v in self._welcomed.items()
                                  if now - v < 3600}
        try:
            self.welcome_queue.put_nowait({"ts": now, "user": name, "uid": uid})
        except queue.Full:
            return
        log.info("进直播间：%s（%s，排一条欢迎，前面还有 %d 个）",
                 name[:20], source or "自己直播间", max(0, self.welcome_queue.qsize() - 1))

    def _welcome_due(self) -> bool:
        if not self.welcome or self.welcome_queue.empty():
            return False
        return time.time() - self.last_welcome_at >= self.welcome_gap

    def note_thanks(self, kind: str, user: str, uid: str, source: str = "",
                    detail: str = "") -> None:
        """有人关注了 / 加了粉丝团 / 上舰了 / 送了礼物：排一条感谢。

        和欢迎同一套限频思路；`detail` 给上舰（舰长/提督/总督）和礼物（礼物名×数量）用。
        """
        kind = str(kind or "follow").lower()
        if kind not in ("follow", "fans", "guard", "gift"):
            kind = "follow"
        if not self.enabled:
            return
        if kind == "follow" and not self.thanks_follow:
            return
        if kind == "fans" and not self.thanks_fans:
            return
        if kind == "guard" and not self.thanks_guard:
            return
        if kind == "gift" and not self.thanks_gift:
            return
        name = (user or "").strip()
        if not name or len(name) > 24:
            return
        uid = str(uid or "")
        if uid and self._self_uid and uid == self._self_uid:
            return
        now = time.time()
        key = "%s|%s" % (kind, uid or name)
        # 礼物按「人」去重不能一小时一次——人家连送两波就该谢两次，
        # 频率交给 gift_max_per_minute 和合并窗口；这里只防同一条重复。
        dedupe = 120.0 if kind == "gift" else 3600.0
        with self.lock:
            if now - self._thanked.get(key, 0) < dedupe:
                return
            self._thanked[key] = now
            if len(self._thanked) > 800:
                self._thanked = {k: v for k, v in self._thanked.items()
                                 if now - v < 7200}
        try:
            self.thanks_queue.put_nowait({"ts": now, "user": name, "uid": uid,
                                          "kind": kind, "detail": detail})
        except queue.Full:
            return
        label = {"follow": "关注", "fans": "粉丝团", "guard": "上舰", "gift": "礼物"}[kind]
        log.info("%s：%s（%s，排一条感谢%s）", label, name[:20],
                 source or "自己直播间", ("，" + detail) if detail else "")

    # ---------------------------------------------------------- 上舰 / 礼物
    def note_guard(self, user: str, uid: str, level: int = 0) -> None:
        """上舰：舰长(3)/提督(2)/总督(1)，名字和头衔都带上。"""
        name = (user or "").strip()
        if not name:
            return
        title = {1: "总督", 2: "提督", 3: "舰长"}.get(int(level or 0), "舰长")
        self.note_thanks("guard", name, uid, detail=title)

    def note_gift(self, user: str, uid: str, gift: str, num: int = 1,
                  battery: float = 0.0) -> None:
        """礼物：先攒进合并缓冲，一会儿合成一条谢（频繁送不刷屏）。

        `battery` 是礼物价值（单位电池，100 金瓜子 = 1 电池；银瓜子免费礼物算 0）。
        低于 `host.gift_min_value`（默认 10 电池）的不谢——免得一句谢一个小心心。
        """
        if not self.enabled or not self.thanks_gift:
            return
        try:
            battery = float(battery or 0)
        except (TypeError, ValueError):
            battery = 0.0
        if battery < float(self.gift_min_value or 0):
            now = time.time()
            if now - getattr(self, "_gift_skip_logged", 0) > 60:
                self._gift_skip_logged = now
                log.info("礼物：%s 的 %s 价值 %.0f 电池，低于门槛 %.0f，不谢",
                         (user or "?")[:16], gift,
                         battery, float(self.gift_min_value or 0))
            return
        name = (user or "").strip()
        if not name or len(name) > 24:
            return
        uid = str(uid or "")
        if uid and self._self_uid and uid == self._self_uid:
            return
        now = time.time()
        key = uid or name
        with self.lock:
            slot = self._gift_pending.get(key)
            if slot is None:
                slot = {"user": name, "uid": uid, "items": [], "first": now,
                        "last": now}
                self._gift_pending[key] = slot
            if len(self._gift_pending) > 40:
                # 太多人同时送：把最早的那批直接冲掉（先谢最旧的）
                oldest = min(self._gift_pending.items(),
                             key=lambda kv: kv[1]["last"])[0]
                if oldest != key:
                    self._gift_pending.pop(oldest, None)
            slot["last"] = now
            slot["battery"] = float(slot.get("battery", 0)) + battery
            gift = re.sub(r"\s+", "", str(gift or "礼物"))[:10] or "礼物"
            try:
                num = max(1, int(num))
            except (TypeError, ValueError):
                num = 1
            merged = False
            for it in slot["items"]:
                if it[0] == gift:
                    it[1] += num
                    merged = True
                    break
            if not merged and len(slot["items"]) < 6:
                slot["items"].append([gift, num])

    def _gift_rate_ok(self) -> bool:
        """礼物感谢上限：最近 60 秒谢过几次。"""
        now = time.time()
        while self._gift_sent and now - self._gift_sent[0] > 60:
            self._gift_sent.popleft()
        return len(self._gift_sent) < self.gift_max_per_minute

    def _flush_gifts(self) -> None:
        """把「停了 gift_merge_seconds 秒没再送」的那批礼物合成一条感谢。"""
        if not self.thanks_gift or not self._gift_pending:
            return
        now = time.time()
        ready = []
        with self.lock:
            for key, slot in list(self._gift_pending.items()):
                if now - slot["last"] >= self.gift_merge_seconds:
                    ready.append(self._gift_pending.pop(key))
            ready.sort(key=lambda s: s["first"])
        for slot in ready:
            if not slot["items"]:
                continue
            if not self._gift_rate_ok():
                log.info("礼物：%s 这批不谢了（一分钟上限 %d 条已满）",
                         slot["user"][:16], self.gift_max_per_minute)
                continue
            detail = "、".join("%s×%d" % (g, n) for g, n in slot["items"][:6])
            self._gift_sent.append(time.time())
            self.note_thanks("gift", slot["user"], slot["uid"], detail=detail)

    def _thanks_due(self) -> bool:
        if self.thanks_queue.empty():
            return False
        return time.time() - self.last_thanks_at >= self.thanks_gap

    def _pick_thanks(self) -> dict | None:
        now = time.time()
        fresh = []
        while True:
            try:
                it = self.thanks_queue.get_nowait()
            except queue.Empty:
                break
            if now - it["ts"] <= self.thanks_max_age:
                fresh.append(it)
        if not fresh:
            return None
        # 花钱的优先：上舰 > 礼物 > 关注 > 粉丝团
        order = {"guard": 0, "gift": 1, "follow": 2, "fans": 3}
        fresh.sort(key=lambda x: (order.get(x["kind"], 9), -x["ts"]))
        return fresh[0]

    def _pick_welcome(self) -> dict | None:
        """挑「刚进来那位」欢迎：取最新的、30 秒内的，更早的都丢掉。

        直播间人多时一分钟能进来十几个，挨个欢迎既来不及也吵；
        欢迎的是**此刻进来的那位**，不是排队排到一分钟前的。
        """
        now = time.time()
        fresh = []
        while True:
            try:
                it = self.welcome_queue.get_nowait()
            except queue.Empty:
                break
            if now - it["ts"] <= 30:
                fresh.append(it)
        if not fresh:
            return None
        latest = max(fresh, key=lambda x: x["ts"])
        # 剩下的（同一批进来的）留 5 秒再看一眼，还赶得上就迎一句
        for it in fresh:
            if it is latest:
                continue
            if now - it["ts"] <= 5:
                try:
                    self.welcome_queue.put_nowait(it)
                except queue.Full:
                    break
        return latest

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
            # 礼物：把「停了一会儿没再送」的那批合成一条感谢（频繁送不刷屏）
            try:
                self._flush_gifts()
            except Exception as exc:  # noqa: BLE001
                log.info("礼物合并失败（不影响这轮）：%s", exc)
            if not self.enabled or self.busy:
                continue
            # 欢迎进场优先：人刚进来，隔太久再迎就没意思了（限频靠 welcome_gap）。
            # 出声时得等她嗓子空；只冒泡（speak=False）就不用等 TTS。
            if self._welcome_due() and (not self.speak or self._can_speak()):
                newbie = self._pick_welcome()
                if newbie is not None:
                    self.busy = True
                    try:
                        reply = self.gateway.host_welcome(
                            newbie["user"], self._context(), self.speak,
                            self.welcome_style)
                        if reply:
                            self.last_text = reply
                        self.last_welcome_at = time.time()
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = str(exc)[:120]
                        log.warning("欢迎进场失败：%s", exc)
                    finally:
                        self.busy = False
                    continue
            # 感谢关注 / 粉丝团：也用最新那条，限频 thanks_gap
            if self._thanks_due() and (not self.speak or self._can_speak()):
                fan = self._pick_thanks()
                if fan is not None:
                    self.busy = True
                    try:
                        reply = self.gateway.host_thanks(
                            fan["kind"], fan["user"], self._context(), self.speak,
                            fan.get("detail") or "")
                        if reply:
                            self.last_text = reply
                        self.last_thanks_at = time.time()
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = str(exc)[:120]
                        log.warning("感谢关注/粉丝团失败：%s", exc)
                    finally:
                        self.busy = False
                    continue
            if not self._can_speak():
                continue
            if time.time() - self.last_reply_at < self.interval:
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
        # 只有真的在「看片/看直播/看番/上课」时，才把「正在看 / 主播刚说」带进来；
        # 游戏模式、auto/normal/off 下这些是背景内容，带进来就串台
        # （2026-11-21 老大「后台挂直播、前台玩游戏」）。
        if str(mode).lower() in ("live", "video", "anime", "course"):
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
