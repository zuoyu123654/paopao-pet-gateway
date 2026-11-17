# -*- coding: utf-8 -*-
"""泡泡桌宠网关 v2：桌宠 ⇄ DeepSeek 之间的本地代理 + 本地 TTS 朗读 + 识屏账本 + DSH 状态朗读。

为什么需要它
------------
桌宠是 PyInstaller 打包的闭源程序，TTS 只认内置四个后端（edge / MiMo / SAPI / 豆包），
地址写死，没有插件口子，所以用户自己练的 dafeiyu 音色接不进去。
但它的聊天走「OpenAI 兼容 provider」——base_url 可以改。指到本网关后，
每一次模型交互的完整文本都会经过这里：

  * 聊天回复      → 流式接、按标点切句、边生成边合成边播（不等整段）
  * 框选/主动识屏 → 先念一句「让我看看」，再念答案；同时写进识屏账本
  * 下一轮聊天    → 把账本注入请求，她接得上刚才识屏里聊过的东西
  * DSH 状态      → 盯 dsh-pet-bridge 的 jsonl 事件，用同一个音色播报
  * 记忆          → 推给生活桥的 /pet/inbox（private_pair，只有老大能看）

桌宠自己的语音必须关掉（chat_reply_voice_enabled=false 等），否则两套声音叠。

接口
----
POST /v1/chat/completions   OpenAI 兼容（支持 stream=true，桌宠用这个）
POST /say                   {"text": "..."} 直接念一句
GET  /health                状态
"""
from __future__ import annotations

import audioop
import base64
import hashlib
import socket
import subprocess
import collections
import io
import ctypes
import ctypes.wintypes as wt
import glob
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tts_backends import TtsBackend, TtsUnavailable
from video_watch import VideoWatcher
from bubble_bridge import BubbleBridge
from voice_mode import VoiceMode, LOOK_HINTS
from player_watch import PlayerWatch
from lore import Lore, LoreKeeper, ProgressKeeper, clean_query
import phrases as fixed_phrases

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "listen_host": "127.0.0.1",
    "listen_port": 9888,
    "upstream_base_url": "https://api.deepseek.com",
    "upstream_chat_path": "/v1/chat/completions",
    "api_key": "",
    "api_key_credential_target": "provider/openai-main@dsh-pet-standalone",
    "upstream_timeout": 300,
    "tts": {
        "enabled": True,
        "api": "http://127.0.0.1:9882",
        "ref_audio_path": "",
        "prompt_text": "",
        "prompt_lang": "zh",
        "text_lang": "zh",
        "max_sentences": 40,
        "max_chars_per_sentence": 60,
        "timeout": 180,
        "volume": 1.0,
        "synth_workers": 3,
        "synth_parallel": 2,
        "early_sentences": 3,
        "early_min_chars": 8,
        "first_min_chars": 24,
        "reading_fixes": {},
        "guard": {
            "enabled": True,
            "check_seconds": 15,
            "wait_seconds": 120,
            "relaunch_cooldown_seconds": 90,
            "launch": {"python": "", "script": "", "args": [], "workdir": ""},
        },
        "pre_cue": [],
        "pre_cue_min_interval": 20,
        "cache_pre_cue": True,
    },
    "speak_chat": True,
    "speak_vision": True,
    "inject_vision_ledger": True,
    "mouse_look": {
        "enabled": True,
        "cooldown_seconds": 3,
    },
    "look_guard": {
        "enabled": True,
        "idle_minutes": 5,
        "check_seconds": 30,
        "helper_port": 9893,
    },
    "divination_voice": {
        "enabled": True,
        "dedupe_minutes": 10,
    },
    "soften_self_recognition": True,
    "self_recognition": {
        "cooldown_minutes": 90,
        "lookback_minutes": 90,
    },
    "frame_buffer": {
        "enabled": True,
        "interval_seconds": 1.0,
        "keep": 8,
        "frame_width": 640,
        "jpeg_quality": 82,
        "grid_frames": 4,
        "frame_idle_interval": 4.0,
        "attach_to_vision": True,
    },
    "inject_vision_context": True,
    # 看片/看番/看直播/网课时：只聊内容，不提时间，不念字幕（老大 2026-11-17 要的）
    "watch_focus": True,
    # 识屏时先描述清楚场景、认得出作品就报名字（老大 2026-11-17 要的）
    "see_howto": True,
    # 黑屏守卫：连续几张几乎全黑就跳过这一轮；桌宠那张黑、我们截得到就换掉
    "skip_black_frames": True,
    "vision_black_ratio": 0.92,
    "vision_black_frames": 2,
    "fix_black_request_image": True,
    # 托盘选的模式锁几分钟：锁住期间自动判定不许动，到点只放开不退出
    "mode_lock_minutes": 5,
    # 前台是游戏稳着待够 enter_seconds 就自动进游戏模式（不看键鼠活跃度）
    "game_watch": {
        "enabled": True,
        "poll_seconds": 5,
        "enter_seconds": 90,
        "switch_seconds": 20,
        "away_grace_seconds": 180,
        "game_apps": [],
    },
    # B站视频模式：Edge 调试端口读播放进度 + bili CLI 拿字幕（方案 B）
    "player_watch": {
        "enabled": True,
        "cdp_port": 9222,
        "poll_seconds": 1.0,
        "fresh_seconds": 8,
        "subtitle_window_seconds": 90,
        "subtitle_cache_hours": 24,
        "bili_cli": "bili",
    },
    # 作品的「肚子里的资料」：后台自己查百科 + B站攻略，总结成本地手册；识屏/语音按需检索。
    # 手册不是常驻注入的（常驻她会逮着名词乱抖），只有画面/问题里出现线索才递一两段。
    "lore": {
        "enabled": True,
        "dir": "lore",
        "auto_learn": True,
        "prebuild": True,
        "poll_seconds": 45,
        "cooldown_minutes": 720,
        "refresh_days": 30,
        "stale_days": 60,
        "max_chunks": 3,
        "budget_chars": 520,
        "chunk_chars": 380,
        "model": "deepseek-chat",
        "summarize_max_tokens": 2400,
        "moegirl": {"enabled": True, "max_chars": 12000},
        "bili": {"enabled": True, "videos": 2, "max_chars": 2400, "timeout": 150},
        "progress": {"enabled": True, "minutes": 30, "min_rows": 2,
                     "window_minutes": 40, "max_age_days": 7, "max_chars": 120},
    },
    # 看到不认识的游戏/番剧，自己问一下写进 titles.json（作品小抄）
    "title_learner": {
        "enabled": True,
        "interval_seconds": 60,
        "min_checks": 2,
        "cooldown_minutes": 360,
    },
    "vision_context_entries": 6,
    "vision_context_minutes": 30,
    "vision_context_budget_chars": 600,
    "vision_context_item_chars": 90,
    "inject_recent_minutes": 45,
    "inject_recent_entries": 3,
    "ledger_digest": {
        "enabled": True,
        "interval_hours": 3,
        "min_entries": 8,
        "max_entries": 300,
        "max_chars": 240,
        "retention_hours": 24,
    },
    "vision_markers": ["截图", "识屏", "屏幕", "框选", "画面", "image_url"],
    "wipe_owner_word": True,
    "ledger_path": "ledger.jsonl",
    "log_path": "logs/gateway.log",
    "memory_inbox_url": "http://127.0.0.1:9889/pet/inbox",
    "memory_inbox_scenarios": ["vision"],
    "dsh": {
        "enabled": True,
        "event_dir": "",
        "speak_tools": True,
        "tool_min_interval": 25,
        "poll_seconds": 1.5,
    },
}

log = logging.getLogger("petgw")


# --------------------------------------------------------------- 作品小抄（游戏/番剧）
# 识别出「这是绝区零」之后她还得知道这游戏是啥，不然只会说「哦，绝区零啊」然后没话了。
# 这张表就是给她的小抄：键是标题/进程名里能匹配到的名字。
# 值可以是一句话（老格式），也可以是 {"kind": "game|anime|video", "note": "..."}——
# kind 是给「游戏模式自动进」判断用的（前台是游戏才自动进）。
TITLES_PATH = os.path.join(APP_DIR, "titles.json")
_TITLE_NOTES: dict[str, str] = {}
_TITLE_KINDS: dict[str, str] = {}
_TITLE_MTIME = 0.0


def title_notes(refresh: bool = False) -> dict[str, str]:
    """读 titles.json（改了不用重启，按修改时间自动重载）。"""
    global _TITLE_NOTES, _TITLE_KINDS, _TITLE_MTIME
    try:
        mtime = os.path.getmtime(TITLES_PATH)
    except OSError:
        return _TITLE_NOTES
    if refresh or mtime != _TITLE_MTIME:
        try:
            with open(TITLES_PATH, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            notes: dict[str, str] = {}
            kinds: dict[str, str] = {}
            for k, v in (data or {}).items():
                key = str(k).strip().lower()
                if not key:
                    continue
                if isinstance(v, dict):
                    note = str(v.get("note") or "").strip()
                    kind = str(v.get("kind") or "").strip().lower()
                else:
                    note, kind = str(v).strip(), ""
                if note:
                    notes[key] = note
                    kinds[key] = kind
            _TITLE_NOTES, _TITLE_KINDS = notes, kinds
            _TITLE_MTIME = mtime
        except Exception as exc:  # noqa: BLE001
            log.warning("titles.json 读不了（先不注入小抄）：%s", exc)
    return _TITLE_NOTES


def _title_key(app: str = "", title: str = "") -> str:
    """命中哪一条（返回小写的键），没命中返回空串。"""
    notes = title_notes()
    if not notes:
        return ""
    hay = ("%s %s" % (app or "", title or "")).lower()
    for key in notes:
        if key and key in hay:
            return key
    return ""


def title_hint(app: str = "", title: str = "") -> str:
    """按窗口标题/进程名在这张小抄里找一条说明，找不到就返回空。"""
    key = _title_key(app, title)
    return title_notes().get(key, "") if key else ""


def title_kind(app: str = "", title: str = "") -> str:
    """这一条是什么类型：game / anime / video / 空（不知道）。"""
    key = _title_key(app, title)
    return _TITLE_KINDS.get(key, "") if key else ""


def save_title_notes(updates: dict) -> int:
    """把几条写进 titles.json（原子替换），返回写入后的总条数。

    值可以是字符串，也可以是 {"kind": ..., "note": ...}；已有的条目原样保留。
    """
    try:
        with open(TITLES_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    for key, val in (updates or {}).items():
        key = str(key).strip()
        if not key or not val:
            continue
        if isinstance(val, dict):
            note = str(val.get("note") or "").strip()
            kind = str(val.get("kind") or "").strip().lower()
            if note:
                data[key] = {"kind": kind, "note": note} if kind else note
        else:
            data[key] = str(val).strip()
    tmp = TITLES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, TITLES_PATH)
    title_notes(refresh=True)
    return len(data)


def build_see_howto() -> str:
    """识屏时「怎么看」的规矩：先把场景看清楚，再挑一两句说出口。"""
    return "\n".join([
        "【先看清，再说话】",
        "1. 按顺序扫一遍：标题栏写的是什么 → 画面主体是谁、在哪儿、在干什么 → 角落的界面元素"
        "（血条、技能栏、进度条、弹幕、按钮、数字）。心里先把它描述成一句完整的话，"
        "**说出口时也要让人听得出这是哪儿、在干什么**，但别逐条罗列、别报数字。",
        "2. 认作品：**窗口标题里写着的作品名可以直接说出来**。上面那句「标题只是参考、不要逐字念」"
        "说的是别把整条标题念一遍，不是不让你说名字——老大问「我在玩什么」「这是哪部」，就报名字。"
        "标题和画面对不上时，先信画面，再说「看着倒像是……」；两个都判断不出来就说「像是什么什么」，别编。",
        "3. 认出来的作品，就接着聊你知道的（类型、玩法、角色、梗）；真不熟，就只说名字，"
        "再加上画面里能看到的，别装懂也别装不知道。",
        "4. 画面里的人物：能对上号就**直接叫名字**（「这不是 XXX 吗」），这是老大最爱听的；"
        "对不上号就说外形特征，别硬安名字、别把人认成别的作品里的角色。",
        "5. **给你的是几张连续画面（帧），当一段动作看**：不要用「第一张」「第二张」「第几张讲了什么」"
        "这种说法，也不要把它们当成几张不同的图分别描述；只说你从这几秒里看出来的事。",
        "6. 画面上的文字（字幕、台词、界面上的字）不要念出来。",
    ])


# --------------------------------------------------------------------------- 配置
PET_CONFIG_PATH = os.path.join(os.environ.get("APPDATA", ""),
                               "dsh-pet-standalone-webm-chat", "config.json")


def read_pet_proactive() -> bool | None:
    """桌宠设置里的「主动识屏」开着没。读不到返回 None。"""
    try:
        with open(PET_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        ps = data.get("proactive_screen") or {}
        return bool(ps.get("enabled"))
    except (OSError, ValueError):
        return None


def write_pet_proactive(on: bool) -> bool:
    """把「主动识屏」拨到 on/off（桌宠有 QFileSystemWatcher，会热重载）。

    先写临时文件再替换，免得她正好在读的时候读到半截。
    """
    try:
        with open(PET_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        ps = dict(data.get("proactive_screen") or {})
        if bool(ps.get("enabled")) == bool(on):
            return True
        ps["enabled"] = bool(on)
        data["proactive_screen"] = ps
        tmp = PET_CONFIG_PATH + ".paopao-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PET_CONFIG_PATH)
        log.info("主动识屏 → %s", "开" if on else "关")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("改主动识屏失败：%s", exc)
        return False


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            deep_merge(cfg, json.load(f))
    if not cfg["dsh"]["event_dir"]:
        cfg["dsh"]["event_dir"] = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "dsh-pet-bridge"
        )
    set_reading_fixes(cfg["tts"].get("reading_fixes"))
    return cfg


def deep_merge(base: dict, patch: dict) -> None:
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v


def setup_logging(cfg: dict) -> None:
    path = cfg["log_path"]
    if not os.path.isabs(path):
        path = os.path.join(APP_DIR, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(ch)


# --------------------------------------------------------------- Windows 凭据读取
CRED_TYPE_GENERIC = 1


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD), ("Type", wt.DWORD), ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR), ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR),
    ]


def read_credential(target: str) -> str:
    if not target:
        return ""
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.CredReadW.argtypes = [
            wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))
        ]
        advapi.CredReadW.restype = wt.BOOL
        ptr = ctypes.POINTER(_CREDENTIAL)()
        if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            return ""
        cred = ptr.contents
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        advapi.CredFree(ptr)
        return raw.decode("utf-16-le", "ignore").strip("\x00").strip()
    except Exception:
        return ""


# ------------------------------------------------------------------- 文本 → 能念的话
_MD_CODE_BLOCK = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_INLINE = re.compile(r"`([^`]*)`")
_MD_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)
_MD_BULLET = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d{1,2}[.)])[ \t]+", re.M)
_MD_EMPHASIS = re.compile(r"\*\*|__|~~")
_BRACKETS = re.compile(r"[\(\（\[【][^\）\)\]】]{0,60}[\)\）\]】]")
_ASTERISK_ACTION = re.compile(r"\*[^*\n]{1,40}\*")
_SPACES = re.compile(r"[ \t]{2,}")
_SENT_END = "。！？!?；;…~"
_SOFT_BREAK = re.compile(r"[,，、：:—\-\s]")
# 第一句的切分点：逗号、分号也算，凑够 8 个字就开口，不硬等句号
_FIRST_BREAK = "。！？!?；;…~，,、：:"


# 多音字/生僻字读错时的「换字读」表：键是屏幕上写的字，值是送去合成的字。
# 只影响念出来的声音，气泡、账本、记忆里还是原来的字。config.json 的
# tts.reading_fixes 一改就生效（重启网关），不用动 GPT-SoVITS。
READING_FIXES: dict[str, str] = {}


def set_reading_fixes(mapping) -> None:
    global READING_FIXES
    READING_FIXES = {str(k): str(v) for k, v in (mapping or {}).items() if str(k)}


def clean_for_speech(text: str) -> str:
    if not text:
        return ""
    text = _MD_CODE_BLOCK.sub("代码略。", text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_INLINE.sub(r"\1", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _BRACKETS.sub("", text)         # 括号里的动作不用念
    text = _ASTERISK_ACTION.sub("", text)   # *动作* 也不念
    text = _SPACES.sub(" ", text)
    if READING_FIXES:
        for src, dst in READING_FIXES.items():
            if src in text:
                text = text.replace(src, dst)
    return text.strip()


def split_long(piece: str, max_chars: int) -> list[str]:
    out = []
    piece = piece.strip()
    while len(piece) > max_chars:
        window = piece[:max_chars]
        cut = max((m.end() for m in _SOFT_BREAK.finditer(window)), default=0)
        if cut < max_chars // 2:
            cut = max_chars
        out.append(piece[:cut].strip())
        piece = piece[cut:].strip()
    if piece:
        out.append(piece)
    return out


def split_sentences(text: str, max_chars: int) -> list[str]:
    out: list[str] = []
    for para in text.splitlines():
        if not para.strip():
            continue
        buf = ""
        for ch in para:
            buf += ch
            if ch in _SENT_END:
                # 省略号/波浪号只是语气停顿：前面只有「唉」这种一两个字时
                # 不要切成一句，否则会先念一声「唉」再卡住等下一句。
                if ch in "…~" and len(clean_for_speech(buf)) < 6:
                    continue
                out.extend(split_long(buf, max_chars))
                buf = ""
        if buf.strip():
            out.extend(split_long(buf, max_chars))
    # 最后再兜一遍：不足 4 个字的碎片（「哎。」「唉……」）并到后一句，
    # 绝不单独送进合成 —— 单独念一个语气词听起来就是卡了一下。
    merged: list[str] = []
    for piece in out:
        if merged and len(clean_for_speech(merged[-1])) < 4:
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)
    out = merged
    return [s for s in out if re.search(r"[0-9A-Za-z\u4e00-\u9fff]", s)]


def take_first_sentence(buf: str, min_chars: int = 8) -> tuple[str, str]:
    """第一句：逗号/分号也算断点，够 8 个字（大于 7）就切走，剩下的正常合成。

    这样第一句至少是「哎，老大你在看」这种长度，不会单独蹦一个「哎」。
    """
    for i, ch in enumerate(buf):
        if ch in _FIRST_BREAK and i + 1 >= min_chars:
            return buf[: i + 1], buf[i + 1:]
    return "", buf


def shorten_pauses(pcm: bytes, state: list[int], rate: int,
                   max_silence: float = 0.28, thresh: int = 200) -> bytes:
    """把音频里超过 max_silence 的静音剪短。

    GPT-SoVITS 在语义分块/逗号处会塞 0.4～0.6 秒的静音，听起来就是
    「说两三个词，停一下，再说」。这里只掐静音，不动有声部分。
    state 跨块保存「已经连续静音多少采样」，流式时也能接上。
    """
    import array

    a = array.array("h")
    a.frombytes(pcm)
    out = array.array("h")
    silent = state[0]
    limit = int(rate * max_silence)
    for v in a:
        if -thresh < v < thresh:
            silent += 1
            if silent > limit:
                continue
        else:
            silent = 0
        out.append(v)
    state[0] = silent
    return out.tobytes()


def take_ready_sentence(buf: str, min_chars: int = 6) -> tuple[str, str]:
    """从流式缓冲里切出第一句。

    太碎的片段（「唉……」「唔，」）不单独成句：并回后面的文字，等下一句末标点。
    不然就会出现「先『唉』一声、卡住、再接着念」。
    """
    idx = 0
    while True:
        cut = next((i + 1 for i, ch in enumerate(buf[idx:], start=idx) if ch in _SENT_END), -1)
        if cut < 2:
            return "", buf
        sentence = buf[:cut]
        if len(clean_for_speech(sentence)) >= min_chars:
            return sentence, buf[cut:]
        idx = cut


# ----------------------------------------------------------------- 语音流水线（边合边播）
class SpeechPipeline:
    """句子进 → 多个合成线程并发出音频 → 播放线程按顺序播。

    两条体验规则：
    * 合成与播放重叠：第一句在播时，后面的句子已经在合成，延迟≈第一句合成时间；
    * **新的发声会打断旧的**：来新的一段话（新回复 / 新状态播报）时立刻停掉当前
      播放、丢掉还没播的句子，直接改念新的——不排队等前一段念完。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.seq = 0
        self.generation = 0
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.pending: "queue.Queue[tuple[int, int, str, str]]" = queue.Queue()
        self.ready: dict[int, tuple[int, str | None]] = {}
        self.play_next = 0
        self.last_text = ""
        self.last_at = 0.0
        self.last_pre_cue_at = 0.0
        self.active_priority = 0
        self.last_feed_at = 0.0
        self.spoken_sentences = 0
        self.cue_paths: list[str] = []
        self.cue_durations: list[float] = []
        # 首句真的开声了没有：后面的句子合成要等它，别抢 GPU；提示音也靠它判断
        self.first_audio_started = threading.Event()
        # 整句合成同一时间最多 `synth_parallel` 条（实测 2 条并发每条只慢 10%，总吞吐
        # 1.5 倍；3 条 1.7 倍、4 条 2.2 倍）。以前只跑 1 条：一条 20 字要合成 4 秒，
        # 而前一句只播 3.5 秒，尾巴上的句子就只能干等，「后面几句卡」就是这么来的。
        self.synth_parallel = max(1, int(cfg["tts"].get("synth_parallel", 2) or 2))
        # 但首句还在流式吐音频的时候只许一条陪跑：三条一起挤 GPU，流式那条会掉到
        # 实时以下，第一句就变成「念两个词停一下」。等首句的音频全到手了再放开。
        # 按「第几代」记，不是简单的布尔：上一代的流收尾时不能把这一代的额度提前放开。
        self.stream_done_gen = -1
        self.synth_gate = threading.Condition()
        self.synth_busy = 0     # 正在合成几条（也是「句间空档」监测用的）
        self.play_end_at = 0.0  # 上一句声音什么时候结束（量句间空档用）
        # 低优先级（DSH 状态）排队：等手上这段念完再补，别打断也别丢
        self.deferred: list[tuple[str, str]] = []
        self.turn_active = False   # 模型这一轮还在吐字/合成
        self._playing = False      # 声卡正在响
        # 句子两头留多少静音：GPT-SoVITS 每句后面拖 0.35~0.56 秒，留原样的话
        # 每接一句就多出半秒空白，听着像「一句一句往外蹦」。只掐两头，句中不动。
        self.tts_tight_edges = bool(cfg["tts"].get("tight_sentence_edges", True))
        # 这条锁留给「流式独占」和退路用：流式请求和整句合成偶尔还要互斥一下。
        self.synth_lock = threading.Lock()
        # 写声卡和关声卡必须错开（PortAudio 不允许 write 和 stop 同时进行，撞上会崩进程）
        self.stream_lock = threading.Lock()
        self.first_byte_ewma = 0.0      # 流式首字节平均耗时（秒）
        self.synth_ewma = 0.0           # 非流式整句合成平均耗时（秒）
        self.vision_text_ewma = 0.0     # 识屏从请求到出第一个字的平均耗时（秒）
        self.vision_total_last = 0.0    # 上一次识屏从请求到真正开口的实测耗时
        self._vision_totals = collections.deque(maxlen=3)
        self._vision_req_at = 0.0
        self._vision_req_gen = -1
        self._cue_token = 0
        self.text_ready_at = 0.0
        self._cue_gen = -1
        self.last_first_byte = 0.0
        # 说出口的和整句合成的可以分开两台：流式挤占 GPU 时，整句合成会把流饿死
        # （实测一条整句合成就能让流停 5 秒）。分家后实测两边互不影响。
        self.tts_stream_api = str(
            cfg["tts"].get("api_stream") or cfg["tts"]["api"]
        ).rstrip("/")
        self.tts_file_api = str(
            cfg["tts"].get("api_file") or cfg["tts"]["api"]
        ).rstrip("/")
        self.split_tts = self.tts_stream_api != self.tts_file_api
        # 语音后端：跟着桌宠的设置走（tts.backend = "pet"），也可以写死
        # gpt-sovits / doubao / edge / mimo / sapi。只有 GPT-SoVITS 能真流式，
        # 其它几家一律走「整句合成 → 顺序播放」那条老流程。
        self.tts_backend = TtsBackend(cfg["tts"])
        self._other_broken = False
        log.info("语音后端：%s", self.tts_backend.describe())
        self.stream_first = (
            bool(cfg["tts"].get("streaming_first", True)) and self.tts_backend.streams
        )
        self.stream_mode = int(cfg["tts"].get("streaming_mode", 3))
        # 流式分块大小：16 太小（每 ~2 秒就在接缝处漏一段 0.4~0.7 秒的静音，
        # 听感「念两三个词、停一下」），32 实测没有接缝停顿，首字节只慢 0.3 秒。
        self.stream_min_chunk = int(cfg["tts"].get("stream_min_chunk_length", 32))
        self.stream_min_chunk_first = int(
            cfg["tts"].get("stream_min_chunk_length_first", 16)
        )
        self.stream_overlap = int(cfg["tts"].get("stream_overlap_length", 4))
        # 只有短句才去标点（去标点是怕模型在逗号处切段）；长句保留标点，不然一口气念不完
        self.strip_stream_max = int(cfg["tts"].get("strip_stream_punct_max_chars", 24))
        # 首句之后，攒够这么多字再送一次，别一句一个请求（每次都要等 0.8 秒首字节）
        self.merge_chars = int(cfg["tts"].get("stream_merge_chars", 40))
        self._force_stream: set[int] = set()
        self.current_stream = None
        self._prefetched: dict[int, dict] = {}
        self._first_idx: dict[int, int] = {}
        self.audio_dir = os.path.join(APP_DIR, "audio")
        self.phrase_dir = os.path.join(self.audio_dir, "phrases")
        os.makedirs(self.audio_dir, exist_ok=True)
        os.makedirs(self.phrase_dir, exist_ok=True)
        workers = max(1, int(cfg["tts"].get("synth_workers", 2)))
        workers = max(workers, self.synth_parallel)
        for i in range(workers):
            threading.Thread(target=self._synth_loop, name="synth-%d" % i, daemon=True).start()
        threading.Thread(target=self._play_loop, name="player", daemon=True).start()
        threading.Thread(target=self._janitor_loop, name="audio-janitor", daemon=True).start()

    # ---------------------------------------------------------------- 对外
    PRIORITY = {"chat": 10, "vision": 10, "say": 10, "mode": 10,
                "divination": 9, "precue": 6, "dsh": 2}

    def say(self, text: str, tag: str = "chat") -> int:
        """整段文本按标点切句后入队；高优先级会打断低的，低的不打断高的。"""
        if not self.cfg["tts"].get("enabled"):
            return 0
        cleaned = clean_for_speech(text)
        if not cleaned or (cleaned == self.last_text and time.time() - self.last_at < 3):
            return 0
        priority = self.PRIORITY.get(tag, 5)
        now = time.time()
        self.last_text, self.last_at = cleaned, now
        max_chars = int(self.cfg["tts"].get("max_chars_per_sentence", 60))
        sentences = split_sentences(cleaned, max_chars)[: int(self.cfg["tts"].get("max_sentences", 40))]
        # 低优先级（DSH 状态）不许打断正事，也不许被吞：先记着，
        # 等手上这段（当前回合 + 已经排队的句子 + 正在响的音频）念完再补。
        if priority < self.active_priority and self._low_busy():
            # 同一类低优先级只留最新一条：DSH 的 working/thinking 不该在完成之后
            # 还排队补念，真正要补的是「完成工作」。
            if tag == "dsh":
                self.deferred = [(t, g) for t, g in self.deferred if g != "dsh"]
            self.deferred.append((text, tag))
            if len(self.deferred) > 6:
                self.deferred.pop(0)
            log.info("低优先级先排队（%s）：%s", tag, cleaned[:30])
            return 0
        self.active_priority, self.last_feed_at = priority, now
        self.interrupt()
        log.info("念(%s) %d 句 / %d 字", tag, len(sentences), len(cleaned))
        return self.feed_sentences(sentences, tag)

    def _low_busy(self) -> bool:
        """手上还有没有高优先级的话（当前回合、排队、合成、正在响）。"""
        return bool(self.turn_active or self._playing or self.pending_count() > 0
                    or bool(self.ready) or self.synth_busy > 0)

    def _drain_deferred(self) -> bool:
        """手上空了，补念低优先级状态（DSH 完成这类）。"""
        if not self.deferred or self._low_busy():
            return False
        text, tag = self.deferred.pop(0)
        cleaned = clean_for_speech(text)
        if not cleaned:
            return True
        max_chars = int(self.cfg["tts"].get("max_chars_per_sentence", 60))
        sentences = split_sentences(cleaned, max_chars)[: int(self.cfg["tts"].get("max_sentences", 40))]
        log.info("补念排队（%s）：%s", tag, cleaned[:30])
        self.feed_sentences(sentences, tag)
        return True

    def feed_sentences(self, sentences: list[str], tag: str = "chat",
                       force_stream: bool = False) -> int:
        n = 0
        with self.lock:
            gen = self.generation
        self.active_priority = max(self.active_priority, self.PRIORITY.get(tag, 5))
        self.last_feed_at = time.time()
        first_of_gen = None
        for s in sentences:
            with self.lock:
                idx = self.seq
                self.seq += 1
                if force_stream:
                    self._force_stream.add(idx)
            if first_of_gen is None:
                first_of_gen = idx
                # 同一个 gen 里只有【第一句】算首句；后面几批别把首句覆盖掉，
                # 否则每一句都被当流式、也绕过了「等首句开声」的让路逻辑。
                if gen not in self._first_idx:
                    self._first_idx[gen] = idx
                # 新一轮：先清掉上一轮的开声标记再入队
                if self._cue_gen != gen:
                    self._cue_gen = gen
                    self.first_audio_started.clear()
            self.pending.put((idx, gen, s, tag))
            n += 1
        return n

    def _gen_first_idx(self, gen: int):
        return getattr(self, "_first_idx", {}).get(gen)

    def _stream_url(self, text: str, first: bool) -> str:
        """流式请求的地址。第一句用小块（开口最快），后面用大块（接缝少、质量好）。"""
        tts = self.cfg["tts"]
        speak_text = text
        if tts.get("strip_stream_punct", False) and len(text) <= self.strip_stream_max:
            # 短句去掉标点再送：模型不会在逗号处切一小段、生成完再等下一段。
            # 长句必须留着标点，不然一口气念不完、也没气口。
            speak_text = re.sub(r"[，,、；;：:。！？!?…~]", "", text).strip() or text
            if speak_text != text:
                log.info("流式送 TTS（去标点）：%s", speak_text[:40])
        params = {
            "text": speak_text,
            "text_lang": tts.get("text_lang", "zh"),
            "ref_audio_path": tts.get("ref_audio_path", ""),
            "prompt_text": tts.get("prompt_text", ""),
            "prompt_lang": tts.get("prompt_lang", "zh"),
            "text_split_method": tts.get("stream_split_method", "cut0"),
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": self.stream_mode,
            "min_chunk_length": (
                self.stream_min_chunk_first if first else self.stream_min_chunk
            ),
            "overlap_length": self.stream_overlap,
            # 第一小句可以念慢一点（正常语速），拖长一点时间，好让后面那段合成赶上；
            # 后面照旧用加速的，不拖沓。
            "speed_factor": float(
                tts.get("speed_first" if first else "speed", 1.0) or 1.0
            ),
        }
        return self.tts_stream_api + "/tts?" + urllib.parse.urlencode(params)

    def _play_stream(self, text: str, gen: int, first: bool = False,
                     idx: int = -1) -> tuple[str, float]:
        """向 GPT-SoVITS 要流式音频（streaming_mode=2/3），边到边写声卡。

        返回 (状态, 已播秒数)：
          ok        正常播完
          broken    播到一半连接断了（已播秒数大于 0）
          failed    根本连不上，一个字节都没播
          cancelled 被新的发言打断

        api_v2 的流式回包是「一个 wav 头 + 连续 PCM」，所以先读 44 字节头
        拿到采样率/声道，后面的字节直接喂给 sounddevice。
        """
        import numpy as np
        import sounddevice as sd

        tts = self.cfg["tts"]
        url = ""
        volume = float(tts.get("volume", 1.0) or 1.0)
        started = time.time()

        # 流式读的时候独占 GPU。实测：只要同时再跑一条整句合成，这条流会整段停
        # 4～5 秒（两个请求在 GPU 上排队，流式那些小步直接被饿死），听感就是
        # 「念一句、卡死、再接上」。分了两台就不用了：整句合成在另一台上跑，
        # 实测流式保持 1.57x、最长空档 0.63s（跟独占时的 1.58x / 0.64s 一样）。
        played = 0.0
        stream = None
        lock_held = False
        if tts.get("exclusive_gpu", False) and not self.split_tts:
            lock_held = self.synth_lock.acquire(
                timeout=float(tts.get("stream_gpu_wait", 25))
            )
            if not lock_held:
                log.warning("等不到 GPU 独占（有整句合成卡住了？），这条流可能被抢")
        resp = None
        pre = b""
        pf = self._prefetched.pop(idx, None) if idx >= 0 else None
        if pf is not None and pf.get("gen") != gen:
            try:
                pf["resp"].close()
            except Exception:  # noqa: BLE001
                pass
            pf = None
        try:
            if pf is not None:
                # 上一句还在播的时候就把这句的请求开出去了，头和数据都到手，
                # 直接接上，不用再等一个首字节。
                resp = pf["resp"]
                rate, channels, pre = pf["rate"], pf["channels"], pf["buf"]
                log.info("接上预取好的下一句（%d 字，%.2fs 已到手）",
                         len(text), len(pre) / 2.0 / max(1, rate))
            else:
                url = self._stream_url(text, first)
                for attempt in (1, 2, 3):
                    try:
                        resp = urllib.request.urlopen(
                            urllib.request.Request(url), timeout=float(tts.get("timeout", 180))
                        )
                        break
                    except Exception as exc:  # noqa: BLE001
                        log.warning("流式请求没连上（第 %d/3 次）：%s", attempt, exc)
                        if attempt == 3:
                            return "failed", 0.0
                        self._wait_tts(25.0)
                        time.sleep(0.5 * attempt)

                header = b""
                while len(header) < 44:
                    more = resp.read(44 - len(header))
                    if not more:
                        break
                    header += more
                if len(header) < 44 or header[:4] != b"RIFF":
                    # 服务端出错时可能回一段 JSON/HTML，别把错误信息当 PCM 播出去
                    log.warning("流式回包不是 wav 头：%r", header[:16])
                    return "failed", 0.0
                rate, channels = 32000, 1
                if len(header) >= 44 and header[:4] == b"RIFF":
                    try:
                        import io
                        import wave

                        with wave.open(io.BytesIO(header + b"\x00" * 4096)) as w:
                            rate, channels = w.getframerate(), w.getnchannels()
                    except Exception:  # noqa: BLE001
                        pass
            stream = sd.RawOutputStream(samplerate=rate, channels=channels, dtype="int16",
                                        device=self._out_device())
            self.current_stream = stream
            # 先垫一点再开声：GPT-SoVITS 是一阵一阵吐音频的，第一阵往往只有
            # 0.4 秒，第二阵要再等 0.4 秒才来。直接开播就会「哎」一下卡住。
            prebuffer = float(
                (tts.get("prebuffer_first_seconds") if first else None)
                or tts.get("prebuffer_seconds", 0.6) or 0.0
            )
            start_evt = threading.Event()

            def _writer() -> None:
                try:
                    start_evt.wait(3.0)
                    stream.start()
                    while True:
                        item = pcm_q.get()
                        if item is None:
                            break
                        with self.lock:
                            if gen != self.generation:
                                break
                        # 写和收尾用同一把锁：PortAudio 不允许一边 write 一边 stop，
                        # 撞上就是 0xc0000005 崩进程（2026-11-17 01:36 实测）
                        with self.stream_lock:
                            stream.write(item)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    writer_done.set()

            pcm_q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)
            writer_done = threading.Event()
            threading.Thread(target=_writer, name="stream-writer", daemon=True).start()

            pending = b""
            pause_state = [0]
            max_pause = float(tts.get("max_internal_pause", 0.28) or 0.0)
            while True:
                with self.lock:
                    if gen != self.generation:
                        break
                if pre:
                    chunk, pre = pre, b""
                else:
                    chunk = resp.read(4096)
                if not chunk:
                    break
                pending += chunk
                usable = len(pending) - (len(pending) % (2 * channels))
                if usable <= 0:
                    continue
                data = np.frombuffer(pending[:usable], dtype=np.int16)
                pending = pending[usable:]
                if volume != 1.0:
                    data = np.clip(data.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
                raw = data.tobytes()
                if max_pause > 0:
                    raw = shorten_pauses(raw, pause_state, rate, max_pause)
                played += len(raw) / float(rate * channels * 2)
                try:
                    pcm_q.put(raw, timeout=10)
                except queue.Full:
                    break
                if not start_evt.is_set() and played >= prebuffer:
                    start_evt.set()
                    first = time.time() - started
                    self.last_first_byte = first
                    self.first_byte_ewma = (
                        first if self.first_byte_ewma <= 0 else 0.7 * self.first_byte_ewma + 0.3 * first
                    )
                    self._mark_first_audio(gen)
                    log.info("流式开播：%dHz %dch（垫 %.2fs，首字节 %.2fs）", rate, channels, prebuffer, first)
            start_evt.set()   # 短句：不等垫底，直接放
            if pending:
                usable = len(pending) - (len(pending) % (2 * channels))
                if usable > 0:
                    data = np.frombuffer(pending[:usable], dtype=np.int16)
                    if volume != 1.0:
                        data = np.clip(data.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
                    raw = data.tobytes()
                    if max_pause > 0:
                        raw = shorten_pauses(raw, pause_state, rate, max_pause)
                    played += len(raw) / float(rate * channels * 2)
                    try:
                        pcm_q.put(raw, timeout=10)
                    except queue.Full:
                        pass
            if not self.first_audio_started.is_set():
                self._mark_first_audio(gen)
            # 首句的音频已经全到手了：放开并发的合成额度，后面的句子两条一起上。
            self._mark_stream_done(gen)
            # 音频已经全进播放队列了：现在放开 GPU，后面的句子可以开始合成/开流，
            # 正好赶在这句还没播完的时候接上。
            if lock_held:
                self.synth_lock.release()
                lock_held = False
            # GPU 空出来了，这句也已经在播：把下一句的请求先开出去，
            # 等这句播完直接接上（句间只剩模型自己吐的那点气口）。
            if tts.get("prefetch_next", True):
                threading.Thread(target=self._prefetch_next, args=(gen,),
                                 name="prefetch", daemon=True).start()
            try:
                pcm_q.put(None, timeout=0.3)
            except queue.Full:
                pass
            t_put = time.time()
            # 等这段播完再返回，下一句才接得上；被打断时最多等 5 秒
            writer_done.wait(timeout=max(5.0, played + 5.0))
            log.info("这一小句说完：%.2fs 音频（收队 %.2fs）", played, time.time() - t_put)
            with self.lock:
                if gen != self.generation:
                    return "cancelled", played
            return "ok", played
        except Exception as exc:  # noqa: BLE001
            log.warning("流式中途断了（已播 %.1fs，发起 %.1fs）：%s", played, time.time() - started, exc)
            return "broken", played
        finally:
            if lock_held:
                self.synth_lock.release()
            # 流式这条路不管成没成，都别再让别人干等着额度
            self._mark_stream_done(gen)
            if stream is not None:
                try:
                    # 等写入线程退干净再收尾；万一它卡在 write 里，也拿同一把锁错开，
                    # 绝不在它写的时候 stop。
                    writer_done.wait(timeout=3.0)
                    with self.stream_lock:
                        t_stop = time.time()
                        stream.stop()
                        t_closed = time.time()
                        stream.close()
                    # 这两步会等声卡把缓冲里剩下的放完：偶尔会拖出一大截，
                    # 那段时间播放线程也在里面，听感就是「一句之后空了半拍」。
                    stop_s, close_s = t_closed - t_stop, time.time() - t_closed
                    if stop_s + close_s > 0.3:
                        log.info("流式收尾用了 %.2fs（stop %.2fs / close %.2fs）",
                                 stop_s + close_s, stop_s, close_s)
                except Exception:  # noqa: BLE001
                    pass
            self.current_stream = None
            t_resp = time.time()
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            t_close = time.time()
            with self.lock:
                if gen == self.generation and played > 0.2:
                    self.spoken_sentences += 1
            t_lock = time.time()
            # 收尾这三步（关响应 / 抢锁 / 记数）任何一步拖住，都会让下一句晚开口。
            if t_lock - t_resp > 0.3 or t_close - t_resp > 0.3:
                log.info("流式收尾明细：关响应 %.2fs / 抢锁 %.2fs",
                         t_close - t_resp, t_lock - t_close)

    def _prefetch_next(self, gen: int) -> None:
        """上一句的音频已经吐完、GPU 空着：把下一句的流式请求先开出去，
        读到头 + 垫底那点音频就停手存着。等这句播完，下一句直接接上，
        句间就只剩模型自己吐的那点气口，不再多等一个首字节。"""
        try:
            idx = None
            for _ in range(40):          # 下一句可能还在从模型那边流过来
                with self.lock:
                    if gen != self.generation:
                        return
                    cand = self.play_next
                    item = self.ready.get(cand)
                if item is not None:
                    if item[1] is None and cand not in self._prefetched:
                        idx = cand
                        break
                    return                # 下一句不是流式的（缓存文件），不用预取
                time.sleep(0.05)
            if idx is None:
                return
            with self.lock:
                if gen != self.generation:
                    return
                text = self.ready[idx][2]
            first = idx == self._gen_first_idx(gen)
            tts = self.cfg["tts"]
            prebuffer = float(
                (tts.get("prebuffer_first_seconds") if first else None)
                or tts.get("prebuffer_seconds", 0.5) or 0.0
            )
            url = self._stream_url(text, first)
            lock_ok = self.synth_lock.acquire(timeout=float(tts.get("stream_gpu_wait", 25)))
            try:
                resp = urllib.request.urlopen(
                    urllib.request.Request(url), timeout=float(tts.get("timeout", 180))
                )
                header = b""
                while len(header) < 44:
                    more = resp.read(44 - len(header))
                    if not more:
                        break
                    header += more
                if len(header) < 44 or header[:4] != b"RIFF":
                    resp.close()
                    return
                rate, channels = 32000, 1
                try:
                    import io
                    import wave

                    with wave.open(io.BytesIO(header + b"\x00" * 4096)) as w:
                        rate, channels = w.getframerate(), w.getnchannels()
                except Exception:  # noqa: BLE001
                    pass
                need = int(rate * channels * 2 * prebuffer)
                buf = b""
                while len(buf) < need:
                    more = resp.read(need - len(buf))
                    if not more:
                        break
                    buf += more
                with self.lock:
                    if gen != self.generation:
                        resp.close()
                        return
                    self._prefetched[idx] = {
                        "gen": gen, "resp": resp, "rate": rate,
                        "channels": channels, "buf": buf,
                    }
                log.info("下一句已预取（%d 字，%.2fs 到手）", len(text), len(buf) / 2.0 / rate)
            finally:
                if lock_ok:
                    self.synth_lock.release()
        except Exception as exc:  # noqa: BLE001
            log.warning("预取下一句失败（不影响正常播放）：%s", exc)

    def _mark_first_audio(self, gen: int) -> None:
        """首句真的出声了：记一下，顺便把「识屏从请求到开口」的实测值存下来。"""
        with self.lock:
            if gen != self.generation:
                return
            self.first_audio_started.set()
            if self._vision_req_at and self._vision_req_gen == gen:
                total = max(0.1, time.time() - self._vision_req_at)
                self.vision_total_last = total
                self._vision_totals.append(total)
                self._vision_req_at = 0.0

    def _mark_stream_done(self, gen: int) -> None:
        """首句的流式音频已经全部到手（或根本没有流）：这一代可以放开并发了。"""
        with self.synth_gate:
            self.stream_done_gen = max(self.stream_done_gen, gen)
            self.synth_gate.notify_all()

    def _synth_slot(self, gen: int) -> None:
        """领一个合成额度：这一代的流还没吐完时只有 1 个，之后 synth_parallel 个。"""
        with self.synth_gate:
            while True:
                cap = self.synth_parallel if self.stream_done_gen >= gen else 1
                if self.synth_busy < cap:
                    self.synth_busy += 1
                    return
                self.synth_gate.wait(timeout=0.5)

    def _synth_slot_done(self) -> None:
        with self.synth_gate:
            self.synth_busy = max(0, self.synth_busy - 1)
            self.synth_gate.notify_all()

    def interrupt(self) -> None:
        """停掉当前播放、丢掉还没播的句子与临时音频。"""
        with self.cond:
            self.generation += 1
            self.first_audio_started.clear()
            self._force_stream.clear()
            dropped = list(self._prefetched.values())
            self._prefetched.clear()
            stale = list(self.ready.values())
            self.ready.clear()
            self.play_next = self.seq
            self.cond.notify_all()
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break
        # 不在这里停 sounddevice 的流：那条流的写入线程正在 stream.write() 里，
        # 从别的线程调 stop() 会和它撞上，PortAudio 直接 0xc0000005 崩整个进程
        # （2026-11-17 01:36 就是这么崩的：新识屏打断了上一轮的流）。
        # 改法是只翻 generation，写入线程下一块就自己退出，_play_stream 在自己线程里收尾。
        try:
            import winsound

            winsound.PlaySound(None, 0)  # 立即停掉正在播的那句（winsound 是另一套接口，安全）
        except Exception:
            pass
        for item in stale:
            path = item[1] if len(item) > 1 else None
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        for pf in dropped:
            try:
                pf["resp"].close()
            except Exception:  # noqa: BLE001
                pass

    def _wait_tts(self, timeout: float = 25.0) -> bool:
        """TTS 连不上时，等它起来（网关的 TtsGuard 会顺手把它拉起来）。"""
        gw = getattr(self, "_gateway", None)
        guard = getattr(gw, "tts_guard", None)
        if guard is None:
            return False
        try:
            return bool(guard.ensure(timeout))
        except Exception:  # noqa: BLE001
            return False

    def _request_audio(self, params: dict, timeout: float, attempts: int = 3) -> bytes:
        """向 GPT-SoVITS 要一段完整音频（非流式）。失败重试 —— 它偶发 400/断连，
        一次失败就丢整句的话，老大听到的就是「说到一半没了」。
        分了两台时，专门合成的那台没起来，就退回主力那台，别让 /say 直接哑掉。"""
        backends = [self.tts_file_api]
        if self.split_tts:
            backends.append(self.tts_stream_api)
        last_exc = None
        for base in backends:
            url = base + "/tts?" + urllib.parse.urlencode(params)
            for attempt in range(1, attempts + 1):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
                        data = resp.read()
                    if len(data) < 1024:
                        raise RuntimeError("tts returned %d bytes" % len(data))
                    if data[:4] != b"RIFF":
                        raise RuntimeError("tts returned non-wav: %r" % data[:16])
                    return data
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    log.warning("TTS 请求失败（%s 第 %d/%d 次）：%s",
                                base.rsplit(":", 1)[-1], attempt, attempts, exc)
                    if attempt < attempts:
                        self._wait_tts(25.0)
                        time.sleep(0.4 * attempt)
        raise RuntimeError("TTS 连续 %d 次失败：%s" % (attempts * len(backends), last_exc))

    def synth_file(self, text: str, path: str) -> None:
        """合成到指定路径（预缓存「让我看看」用）。"""
        tts = self.cfg["tts"]
        params = {
            "text": text,
            "text_lang": tts.get("text_lang", "zh"),
            "ref_audio_path": tts.get("ref_audio_path", ""),
            "prompt_text": tts.get("prompt_text", ""),
            "prompt_lang": tts.get("prompt_lang", "zh"),
            "text_split_method": "cut5",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": "false",
            "speed_factor": float(tts.get("speed", 1.0) or 1.0),
        }
        data = self._request_audio(params, float(tts.get("timeout", 180)))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        self._apply_volume(path)

    def warm_phrases(self, phrases: list[str], force: bool = False) -> None:
        """把固定文案预先合成进缓存，播的时候直接用（不再当场等 TTS）。

        缓存键必须和 `_voice_key` 一致——早先这里少了后端身份，预热出来的文件
        永远命中不了，等于白热。
        """
        done = 0
        for text in phrases:
            text = (text or "").strip()
            if not text:
                continue
            path = os.path.join(self.phrase_dir, self._voice_key(text) + ".wav")
            if force and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                continue
            try:
                self.synth_file(text, path)
                done += 1
            except Exception as exc:  # noqa: BLE001
                log.info("预热失败：%s（%s）", text[:20], exc)
            time.sleep(0.35)   # 别把 TTS 打爆，连发会 400
        log.info("短语缓存预热完成：新增 %d 条", done)

    def _out_device(self):
        """输出设备：`tts.output_device` 里写名字片段就钉死，留空跟系统默认。

        为什么要有这个：蓝牙耳机一接上，Windows 会把默认输出切到耳机（有时候还是
        通话用的 Hands-Free 16k 那档），她的话就全跑耳机里去了——没戴耳机的人听起来
        就是「语音没了」。钉在扬声器上最稳。
        """
        if hasattr(self, "_out_dev_cache"):
            return self._out_dev_cache
        want = str(self.cfg["tts"].get("output_device") or "").strip().lower()
        self._out_dev_cache = None
        if want:
            try:
                import sounddevice as sd

                for i, d in enumerate(sd.query_devices()):
                    if d.get("max_output_channels", 0) > 0 and want in str(d.get("name", "")).lower():
                        self._out_dev_cache = i
                        log.info("输出设备：%s（#%d）", d["name"], i)
                        break
                else:
                    log.warning("找不到输出设备 %r，跟系统默认走", want)
            except Exception:  # noqa: BLE001
                pass
        return self._out_dev_cache

    def warm_pre_cues(self) -> None:
        """启动时把「让我看看」们合成好，识屏时直接播，不再等合成。"""
        cues = self.cfg["tts"].get("pre_cue") or []
        if not cues or not self.cfg["tts"].get("cache_pre_cue", True):
            return
        cache_dir = os.path.join(self.audio_dir, "precue")
        paths = []
        for text in cues:
            name = hashlib.md5(text.encode("utf-8")).hexdigest()[:10] + ".wav"
            path = os.path.join(cache_dir, name)
            paths.append(path)
            if os.path.exists(path) and os.path.getsize(path) > 1024:
                continue
            for attempt in range(3):
                try:
                    self.synth_file(text, path)
                    log.info("预缓存语音就绪：%s", text)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.info("预缓存失败（第 %d 次）：%s", attempt + 1, exc)
                    time.sleep(4)
        # 只给提示音剪静音：新合成的句子不剪（老大嫌效果怪）
        for path in paths:
            if os.path.exists(path):
                self._trim_silence(path)
        with self.lock:
            pairs = [(p, 0.0) for p in paths]
            durations = []
            for p, _ in pairs:
                dur = 0.0
                try:
                    with wave.open(p, "rb") as w:
                        dur = w.getnframes() / float(w.getframerate() or 1)
                except Exception:  # noqa: BLE001
                    dur = 0.0
                durations.append(dur)
            self.cue_paths = [p for (p, _) in pairs if os.path.exists(p)]
            # 和 cue_paths 一一对应；算不出来就按 0 处理（调度时用默认时长）
            self.cue_durations = durations

    def arm_vision_cue(self) -> None:
        """「让我看看」立刻播（2026-11-15 深夜回退：推迟反而让整体更慢）。

        试过按估算推迟到首句开口前，结果提示音和正片抢音频设备，
        反而让开口晚了半秒以上。老大要的是「出文字马上有声」，
        提示音就不做时间游戏了，老老实实在请求一开始播。
        """
        mode = getattr(getattr(self, "_gateway", None), "mode", None)
        # 只在「正在看片/看番/直播/网课」时不说这句——那些场景本来就有声音，插一句很吵。
        # 模式「关闭」不拦：那是老大主动让她看一眼，提示音当然要有。
        if mode is not None and mode.get() in FOCUS_MODES:
            return
        cues = self.cfg["tts"].get("pre_cue") or []
        interval = float(self.cfg["tts"].get("pre_cue_min_interval", 20))
        if not cues or time.time() - self.last_pre_cue_at < interval:
            return
        self.last_pre_cue_at = time.time()
        with self.lock:
            paths = list(self.cue_paths or [])
            durations = list(self.cue_durations or [])
            self._vision_req_at = time.time()
            self._vision_req_gen = self.generation
        if not paths:
            # 还没缓存（刚启动）：退回原来的即时播
            self.say(random.choice(cues), tag="precue")
            return
        pick = random.randrange(len(paths))
        path = paths[pick]
        with self.lock:
            self._cue_token += 1
            token = self._cue_token
        threading.Thread(
            target=self._pre_cue_worker, args=(token, 0.0, path), name="precue", daemon=True
        ).start()

    def _pre_cue_worker(self, token: int, delay: float, path: str) -> None:
        try:
            if delay > 0:
                time.sleep(delay)
            with self.lock:
                if token != self._cue_token:      # 又来了新一轮，这条作废
                    return
                if self.first_audio_started.is_set():   # 已经开口了，别插嘴
                    return
                # 原文已经到了，TTS 只要半秒就开口 —— 这时候再起一句提示音
                # 只会和正片撞在一起，宁可不播。
                if self.text_ready_at and time.time() - self.text_ready_at < 1.5:
                    return
                self._cue_token = -1
            import winsound

            dev = self._out_device()
            if dev is None:
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                # 钉了输出设备就不能用 winsound 了（它只认系统默认）
                import wave as _wave

                import numpy as _np
                import sounddevice as _sd

                with _wave.open(path, "rb") as w:
                    sr = w.getframerate()
                    ch = w.getnchannels()
                    raw = w.readframes(w.getnframes())
                data = _np.frombuffer(raw, dtype=_np.int16)
                if ch > 1:
                    data = data.reshape(-1, ch)
                _sd.play(data, sr, device=dev)
            log.info("念(precue) 贴上去（推迟 %.2fs）", delay)
        except Exception as exc:  # noqa: BLE001
            log.warning("播缓存提示失败：%s", exc)

    def _schedule_pre_cue(self, first_sentence: str) -> None:
        """旧入口，保留兼容；主路径改成请求一开始就 arm_vision_cue。"""
        self.arm_vision_cue()

    def pre_cue(self) -> None:
        # 「让我看看」只在平时（含 auto/关闭）说。看番/看视频/直播/网课时不打扰——
        # 那些场景本来就在放声音，再来一句提示音很吵。
        mode = getattr(getattr(self, "_gateway", None), "mode", None)
        if mode is not None and mode.get() in FOCUS_MODES:
            return
        cues = self.cfg["tts"].get("pre_cue") or []
        interval = float(self.cfg["tts"].get("pre_cue_min_interval", 20))
        if not cues or time.time() - self.last_pre_cue_at < interval:
            return
        self.last_pre_cue_at = time.time()
        # 有预缓存就直接播，一秒都不用等；正在说话时先不打断
        with self.lock:
            paths = list(getattr(self, "cue_paths", []) or [])
            busy = bool(self.ready) or self.pending.qsize() > 0
        if paths and not busy:
            path = random.choice(paths)
            try:
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
                log.info("念(precue) 缓存直出")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("播缓存提示失败，退回在线合成：%s", exc)
        self.say(random.choice(cues), tag="precue")

    def pending_count(self) -> int:
        return self.pending.qsize()

    # ---------------------------------------------------------------- 合成
    def _synth_loop(self) -> None:
        while True:
            idx, gen, sentence, tag = self.pending.get()
            with self.lock:
                if gen != self.generation:      # 已经被新的一段话打断
                    continue
            path = None
            # 第一句走「边生成边播」：不等整段合成完，声音 0.4~0.8 秒就出来。
            # 但固定文案（DSH 状态这类）已经在短语缓存里，直接播文件更快，别绕流式。
            cached = self._phrase_cache_path(sentence)
            if cached:
                with self.cond:
                    if gen == self.generation:
                        self.ready[idx] = (gen, cached, None)
                        self.cond.notify_all()
                        if idx == self._gen_first_idx(gen):
                            self.first_audio_started.set()
                            self._mark_stream_done(gen)   # 首句是缓存文件，没有流要保护
                continue
            # 第一句一律走流式：整句合成要 1～2 秒，流式首字节 0.5 秒就能开口。
            # 以前短句会退到整句合成，结果就是「文字都出来了，声音还要再等一两秒」。
            # 后面攒好的段落也跟着走流式：整句合成要等它全部合成完才出声（一条 25 字
            # 的句子要 4 秒多），流式 0.8 秒就能接着念，句间停顿短得多。
            if self.stream_first and (
                idx == self._gen_first_idx(gen) or idx in self._force_stream
            ):
                with self.cond:
                    if gen == self.generation:
                        self.ready[idx] = (gen, None, sentence)
                        self.cond.notify_all()
                continue
            # 后面几句等第一句真的开声了再合成：这样第一小句的开口不会被抢，
            # 而那点合成时间正好落在第一小句还在说话的时候。
            if idx != self._gen_first_idx(gen):
                self.first_audio_started.wait(timeout=1.5)
            if idx == self._gen_first_idx(gen):
                # 首句本来就该走整句合成（后端不支持流式）：那就没有流要保护，
                # 后面几句直接放开并发。
                self._mark_stream_done(gen)
            # 并发额度：首句还在吐音频时只给 1 条，吐完了才放到 synth_parallel 条。
            # 一条 20 字的句子合成要 4 秒，播出来只有 3.5 秒——只跑一条的话越往后
            # 越跟不上，尾巴上那几句只能干等，听感就是「后面几句卡」。
            self._synth_slot(gen)
            try:
                with self.lock:
                    if gen != self.generation:      # 等额度的时候被新的一段话顶掉了
                        continue
                path = self._synthesize(sentence)
            except Exception as exc:
                log.warning("合成失败(%s): %s | %s", tag, exc, sentence[:30])
            finally:
                self._synth_slot_done()
            with self.cond:
                if gen != self.generation:
                    if path:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    continue
                self.ready[idx] = (gen, path, None)
                self.cond.notify_all()

    def _voice_key(self, text: str) -> str:
        """短句缓存的键：文本 + 语速/音量 + 后端身份（换音色/换后端要换键）。"""
        tts = self.cfg["tts"]
        return hashlib.md5(
            ("%s|%s|%.2f|%.2f" % (
                self.tts_backend.flavor(), text,
                float(tts.get("speed", 1.0) or 1.0),
                float(tts.get("volume", 1.0) or 1.0))).encode("utf-8")
        ).hexdigest()[:16]

    def _phrase_cache_path(self, text: str) -> str:
        """短句的缓存路径；不存在或太长则返回空串。"""
        tts = self.cfg["tts"]
        if len(text) > int(tts.get("phrase_cache_max_chars", 40)):
            return ""
        path = os.path.join(self.phrase_dir, self._voice_key(text) + ".wav")
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            return path
        return ""

    def _synthesize(self, text: str) -> str:
        tts = self.cfg["tts"]
        # 不是 GPT-SoVITS 的时候，交给对应的后端渲染（豆包/edge/MiMo/SAPI），
        # 出来的是一段 wav，后面照样按顺序播。渲染失败一次就整轮退回 GPT-SoVITS，
        # 别每句都去试。
        if self.tts_backend.name != "gpt-sovits" and not self._other_broken:
            try:
                t0 = time.time()
                path = os.path.join(self.audio_dir, "say-%s.wav" % uuid.uuid4().hex[:12])
                self.tts_backend.render(text, path)
                dur = time.time() - t0
                log.info("整句合成好（%s）：%d 字，用了 %.2fs",
                         self.tts_backend.name, len(text), dur)
                self.synth_ewma = dur if self.synth_ewma <= 0 else 0.7 * self.synth_ewma + 0.3 * dur
                self._apply_volume(path)
                return path
            except Exception as exc:  # noqa: BLE001
                log.warning("语音后端 %s 用不了（%s），这一轮退回 GPT-SoVITS",
                            self.tts_backend.name, exc)
                self._other_broken = True
        t0 = time.time()
        # 短句（状态播报、提示音这类固定文案）走磁盘缓存：合成一次，以后直接播。
        use_cache = len(text) <= int(tts.get("phrase_cache_max_chars", 40))
        cache_path = ""
        if use_cache:
            cache_path = os.path.join(self.phrase_dir, self._voice_key(text) + ".wav")
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1024:
                return cache_path
        params = {
            "text": text,
            "text_lang": tts.get("text_lang", "zh"),
            "ref_audio_path": tts.get("ref_audio_path", ""),
            "prompt_text": tts.get("prompt_text", ""),
            "prompt_lang": tts.get("prompt_lang", "zh"),
            "text_split_method": "cut5",
            "batch_size": 1,
            "media_type": "wav",
            "streaming_mode": "false",
            "speed_factor": float(tts.get("speed", 1.0) or 1.0),
        }
        data = self._request_audio(params, float(tts.get("timeout", 180)))
        dur = time.time() - t0
        log.info("整句合成好：%d 字，用了 %.2fs", len(text), dur)
        self.synth_ewma = dur if self.synth_ewma <= 0 else 0.7 * self.synth_ewma + 0.3 * dur
        path = cache_path or os.path.join(self.audio_dir, "say-%s.wav" % uuid.uuid4().hex[:12])
        with open(path, "wb") as f:
            f.write(data)
        self._apply_volume(path)
        self._shorten_file_pauses(path)
        return path

    def _shorten_file_pauses(self, path: str) -> None:
        """把整句合成里的长静音也剪短（和流式一个标准）。"""
        max_pause = float(self.cfg["tts"].get("max_internal_pause", 0.28) or 0.0)
        if max_pause <= 0:
            return
        try:
            with wave.open(path, "rb") as w:
                params = w.getparams()
                if params.sampwidth != 2 or params.nchannels != 1:
                    return
                frames = w.readframes(w.getnframes())
            out = shorten_pauses(frames, [0], params.framerate, max_pause)
            if len(out) < len(frames):
                with wave.open(path, "wb") as w:
                    w.setparams(params)
                    w.writeframes(out)
        except Exception as exc:  # noqa: BLE001
            log.warning("剪内部静音失败（保持原样）：%s", exc)

    def _apply_volume(self, path: str) -> None:
        gain = float(self.cfg["tts"].get("volume", 1.0) or 1.0)
        if abs(gain - 1.0) >= 0.01:
            try:
                with wave.open(path, "rb") as w:
                    params = w.getparams()
                    frames = w.readframes(w.getnframes())
                scaled = audioop.mul(frames, params.sampwidth, gain)
                with wave.open(path, "wb") as w:
                    w.setparams(params)
                    w.writeframes(scaled)
            except Exception as exc:
                log.warning("音量处理失败（按原音量播）: %s", exc)
        # 注意：不给新合成的句子剪静音了（老大嫌效果怪）。只给「让我看看」这类
        # 预存提示音剪（见 warm_pre_cues）。

    def _trim_silence(self, path: str, head_pad: float = 0.05, tail_pad: float = 0.12,
                      thresh: float = 0.015) -> None:
        """剪掉波形两端的静音（保留一点点气口）。幂等，已经干净的文件不会被改。"""
        try:
            with wave.open(path, "rb") as w:
                params = w.getparams()
                if params.sampwidth != 2 or params.nchannels != 1:
                    return
                frames = w.readframes(w.getnframes())
            if not frames:
                return
            import array

            a = array.array("h")
            a.frombytes(frames)
            peak = max(abs(min(a)), abs(max(a))) or 1
            limit = int(peak * thresh)
            n = len(a)
            start = 0
            while start < n and abs(a[start]) < limit:
                start += 1
            end = n - 1
            while end > start and abs(a[end]) < limit:
                end -= 1
            if end <= start:
                return
            rate = params.framerate or 1
            start = max(0, start - int(rate * head_pad))
            end = min(n - 1, end + int(rate * tail_pad))
            if start == 0 and end == n - 1:
                return
            tmp = path + ".trim"
            with wave.open(tmp, "wb") as w:
                w.setparams(params)
                w.writeframes(frames[start * 2:(end + 1) * 2])
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("剪静音失败（保持原样）：%s", exc)

    # ---------------------------------------------------------------- 播放
    def _play_loop(self) -> None:
        import winsound

        while True:
            if self._drain_deferred():
                continue
            with self.cond:
                while True:
                    for stale_idx in [k for k in self.ready if k < self.play_next]:
                        item = self.ready.pop(stale_idx)
                        if item[1]:
                            try:
                                os.remove(item[1])
                            except OSError:
                                pass
                    if self.play_next in self.ready:
                        idx = self.play_next
                        gen, path, stream_text = self.ready.pop(idx)
                        self.play_next += 1
                        break

                    self.cond.wait(timeout=0.5)
            # 跟上一次开口之间隔了多久（0 = 接着上一句，没断）：
            # 这就是「后面几句卡」的量，别的都是猜。
            lead = (time.time() - self.play_end_at) if self.play_end_at else 0.0

            # 「后面几句卡」的量化指标：这一句开口之前，上一句早就念完了，中间是空的。
            # 只算同一段话里的空档（这一轮是在上一句念完之前就喂进来的），
            # 两段话之间闲着的那几秒不算。
            if (0.35 < lead < 8 and self.last_feed_at < self.play_end_at
                    and (self.pending.qsize() > 0 or self.synth_busy > 0)):
                log.info("句间空档 %.2fs（后面那句还没合成好）", lead)
            if stream_text:
                self._playing = True
                try:
                    status, played = self._play_stream(
                        stream_text, gen, first=(idx == self._gen_first_idx(gen)), idx=idx
                    )
                finally:
                    self._playing = False
                self.play_end_at = time.time()
                if status in ("ok", "cancelled"):
                    continue
                with self.lock:
                    interrupted = gen != self.generation
                if interrupted:
                    continue
                # 已经播出去半句了就别整句重来 —— 老大听到的是「同一句说两遍」，
                # 比少个尾巴更难受。只有几乎没出声时才值得补一遍。
                if status == "broken" and played >= 0.5:
                    log.warning("流式断在 %.1fs，这句不再重念", played)
                    continue
                try:
                    with self.synth_lock:
                        path = self._synthesize(stream_text)
                    self._playing = True
                    try:
                        winsound.PlaySound(path, winsound.SND_FILENAME)
                    finally:
                        self._playing = False
                    os.remove(path)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("流式失败后退回普通合成也没成: %s", exc2)
                continue
            if not path:
                continue
            with self.lock:
                if gen != self.generation:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
            try:
                self._mark_first_audio(gen)
                # 句首句尾的静音剪到很短：GPT-SoVITS 每句前面垫 0.06~0.2 秒、后面拖
                # 0.35~0.56 秒，一句一句排下来，每接一句就多出半秒空白——听着就是
                # 「一句一句往外蹦」。只掐两头，句中一个字都不动。
                if self.tts_tight_edges:
                    self._trim_silence(
                        path,
                        head_pad=float(self.cfg["tts"].get("sentence_head_pad", 0.03) or 0.0),
                        tail_pad=float(self.cfg["tts"].get("sentence_tail_pad", 0.08) or 0.0),
                    )
                # 记一下这句多长：查「哪一句后面空了一下」就靠它和上面那行空档日志对时间
                dur = 0.0
                try:
                    with wave.open(path, "rb") as _w:
                        dur = _w.getnframes() / float(_w.getframerate() or 1)
                except Exception:  # noqa: BLE001
                    pass
                log.info("念第%d句：%.2fs 音频（跟上一句隔着 %.2fs）", idx, dur, lead)
                self._playing = True
                try:
                    winsound.PlaySound(path, winsound.SND_FILENAME)  # 阻塞；被打断时会被 PlaySound(None) 截断
                finally:
                    self._playing = False
                self.play_end_at = time.time()
                with self.lock:
                    if gen == self.generation:
                        self.spoken_sentences += 1
            except Exception as exc:
                log.warning("播放失败: %s", exc)
            finally:
                if not path.startswith(self.phrase_dir):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    # ------------------------------------------------------- 音频文件定时清理
    def _janitor_loop(self) -> None:
        days = float(self.cfg["tts"].get("retention_days", 3) or 3)
        while True:
            try:
                cutoff = time.time() - days * 86400
                removed = 0
                for name in os.listdir(self.audio_dir):
                    path = os.path.join(self.audio_dir, name)
                    try:
                        if os.path.getmtime(path) < cutoff:
                            os.remove(path)
                            removed += 1
                    except OSError:
                        continue
                if removed:
                    log.info("清理 %d 个过期语音文件（>%g 天）", removed, days)
            except Exception as exc:
                log.warning("语音文件清理失败: %s", exc)
            time.sleep(6 * 3600)


# --------------------------------------------------------------------- 识屏账本
def press_look(port: int = 9893, cmd: str = "look") -> bool:
    """让沙箱外的按键助手触发一次「看看屏幕」。

    cmd="look" 走她的全局热键（推荐，不弹菜单）；
    cmd="menulook" 走右键菜单（热键失效时的备用）。
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=4) as s:
            s.sendall((cmd + "\n").encode())
            reply = s.recv(64).decode("utf-8", "replace").strip()
        log.info("按键助手：%s", reply)
        return reply.startswith("ok")
    except OSError as exc:
        log.warning("按键助手不可用：%s", exc)
        return False


class MouseLook(threading.Thread):
    """中键点桌宠 = 让她看一眼屏幕。

    她本身没有中键动作（源码里只有左键互动、右键菜单），所以这是我们加的：
    轮询真实鼠标中键（40ms，钩子在这台机器上收不到事件，轮询可以），
    命中她当前位置就替她走「右键 → 菜单选看看屏幕」。
    """

    VK_MBUTTON = 0x04

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="mouse-look", daemon=True)
        conf = cfg.get("mouse_look") or {}
        self.enabled = bool(conf.get("enabled", True))
        self.cooldown = float(conf.get("cooldown_seconds", 3))
        self.cfg = cfg
        self.gateway = gateway
        self.last_fire = 0.0
        self.pet_rect = None
        self.pet_at = 0.0

    def _refresh_pet(self):
        now = time.time()
        if self.pet_rect and now - self.pet_at < 0.5:
            return self.pet_rect
        try:
            import glob

            base = os.path.join(os.environ.get("APPDATA", ""), "dsh-pet-standalone-webm-chat")
            files = sorted(glob.glob(os.path.join(base, "pet-runtime-v2-*.json")), key=os.path.getmtime)
            if files:
                data = json.load(open(files[-1], encoding="utf-8"))
                pad = 6
                self.pet_rect = (data["x"] - pad, data["y"] - pad,
                                 data["x"] + data["w"] + pad, data["y"] + data["h"] + pad)
        except Exception:
            pass
        self.pet_at = now
        return self.pet_rect

    def run(self) -> None:
        if not self.enabled:
            return
        try:
            import ctypes
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
        except Exception as exc:  # noqa: BLE001
            log.warning("中键看屏关闭：%s", exc)
            return
        was_down = False
        while True:
            try:
                down = bool(user32.GetAsyncKeyState(self.VK_MBUTTON) & 0x8000)
                if down and not was_down:
                    rect = self._refresh_pet()
                    if rect:
                        pt = wt.POINT()
                        user32.GetCursorPos(ctypes.byref(pt))
                        if rect[0] <= pt.x <= rect[2] and rect[1] <= pt.y <= rect[3]:
                            if time.time() - self.last_fire >= self.cooldown:
                                self.last_fire = time.time()
                                log.info("中键命中桌宠 @(%d,%d) → 让她看一眼", pt.x, pt.y)
                                press_look(int((self.cfg.get("look_guard") or {}).get("helper_port", 9893)))
                was_down = down
            except Exception as exc:  # noqa: BLE001
                log.warning("中键轮询出错：%s", exc)
            time.sleep(0.04)


class LookGuard(threading.Thread):
    """保底看屏：看视频/直播/网课时，画面可能长时间不动，
    她的观察器就不触发。超过 N 分钟没动静，我们就替她按一次「看看屏幕」，
    免得她一直沉默。平时模式不管，她自己会看。
    """

    MODES = ("video", "anime", "live", "course")

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="look-guard", daemon=True)
        conf = cfg.get("look_guard") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", True))
        self.idle = float(conf.get("idle_minutes", 5.0)) * 60
        self.check = max(10.0, float(conf.get("check_seconds", 30)))
        self.port = int(conf.get("helper_port", 9893))
        self.last_pressed = 0.0

    def run(self) -> None:
        if not self.enabled:
            return
        while True:
            time.sleep(self.check)
            try:
                mode = self.gateway.mode.get()
                if mode not in self.MODES:
                    continue
                idle_for = time.time() - getattr(self.gateway, "last_vision_at", 0)
                if idle_for < self.idle:
                    continue
                if time.time() - self.last_pressed < self.idle:
                    continue
                self.last_pressed = time.time()
                log.info("保底看屏：%s 模式已 %.0f 秒没动静，按一次看看屏幕", mode, idle_for)
                press_look(self.port)
            except Exception as exc:  # noqa: BLE001
                log.warning("保底看屏出错：%s", exc)


class DivinationWatcher(threading.Thread):
    """盯桌宠日志里的「抽签」行，抽到就用我们自己的声音念。

    桌宠自己「抽签时念出来」是**在它进程内**合成播放的，我们插不进去。
    但它在抽完的那一刻会把整段签文写进 pet-<pid>.log：

        2026-11-15 19:40:09,376 INFO [抽签] 〔中平〕朝三暮四 / … / 解签：… ｜ 当天重现

    所以这里的做法是「只认刚写下来的那一行」：
      - 启动时直接 seek 到文件末尾（历史签文一条都不念）；
      - 每 0.5 秒扫一次新行；
      - 只接受**时间戳在最近 90 秒内**的行（防止文件被重写/回放）；
      - 同一段签文 1 小时内只念一次。

    这样既不会「点了没反应」，也不会「过一会儿莫名其妙冒出来」。
    """

    LOG_DIR = os.path.join(os.environ.get("APPDATA", ""), "dsh-pet-standalone-webm-chat")
    PAT = re.compile(r"\[抽签\]\s*(.+?)(?:\s*｜.*)?$")
    TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")

    def __init__(self, cfg: dict, speech):
        super().__init__(name="divination", daemon=True)
        conf = cfg.get("divination_voice") or {}
        self.cfg = cfg
        self.speech = speech
        self.enabled = bool(conf.get("enabled", True))
        self.max_age = float(conf.get("max_line_age_seconds", 90))
        self.dedupe = float(conf.get("dedupe_minutes", 60)) * 60
        self.pos: dict[str, int] = {}
        self.spoken: dict[str, float] = {}
        self.last_text = ""
        self.last_at = 0.0
        self.started = time.time()

    def _speak(self, text: str) -> None:
        now = time.time()
        if text in self.spoken and now - self.spoken[text] < self.dedupe:
            return
        self.spoken[text] = now
        n = self.speech.say(text, tag="divination")
        log.info("抽签朗读：%d 句 / %d 字 — %s", n, len(text), text[:40])

    def _prime(self) -> None:
        """记下每个日志当前的末尾位置：历史内容一律不念。"""
        try:
            files = [f for f in os.listdir(self.LOG_DIR) if f.startswith("pet-") and f.endswith(".log")]
        except OSError:
            return
        for name in files:
            path = os.path.join(self.LOG_DIR, name)
            try:
                self.pos[path] = os.path.getsize(path)
            except OSError:
                pass

    def _scan_once(self) -> None:
        try:
            files = [f for f in os.listdir(self.LOG_DIR) if f.startswith("pet-") and f.endswith(".log")]
        except OSError:
            return
        for name in files:
            path = os.path.join(self.LOG_DIR, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            start = self.pos.get(path)
            if start is None:
                # 新出现的日志文件：从现在开始算
                self.pos[path] = size
                continue
            if size < start:
                start = 0
            if size == start:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(start)
                    chunk = f.read()
            except OSError:
                continue
            self.pos[path] = size
            for line in chunk.splitlines():
                if "[抽签]" not in line:
                    continue
                m = self.PAT.search(line)
                if not m:
                    continue
                text = m.group(1).strip()
                if not text or "语音" in text or "跳过预合成" in text:
                    continue
                # 只认「刚刚发生」的那一行
                mt = self.TS.match(line.strip())
                if mt:
                    try:
                        ts = time.mktime(time.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        ts = time.time()
                    age = time.time() - ts
                    if age > self.max_age or ts < self.started - 5:
                        continue
                self._speak(text)

    def run(self) -> None:
        if not self.enabled:
            return
        self._prime()
        while True:
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("抽签监视出错：%s", exc)
            time.sleep(0.5)


class Mode:
    """陪伴模式：auto / normal / video / anime / live / course / game / off。

    只存一个值，落到 mode.json；插件（AstrBot）通过 HTTP 读写它。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = os.path.join(APP_DIR, "mode.json")
        self.lock = threading.Lock()
        self.value = "auto"
        self.source = "default"
        self.since = time.time()
        self.lock_forever = False
        self.on_change = None       # 切换成功后的回调（网关挂上去，播提示音 + 气泡）
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("mode"):
                self.value = str(data["mode"])
                self.source = str(data.get("source") or "file")
                self.since = float(data.get("since") or time.time())
                self.lock_forever = bool(data.get("lock_forever", False))
        except (OSError, ValueError):
            pass

    def _write(self) -> None:
        data = {"mode": self.value, "source": self.source, "since": self.since,
                "lock_forever": bool(self.lock_forever)}
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError:
            pass

    def get(self) -> str:
        with self.lock:
            return self.value

    def release(self, why: str = "lock-timeout") -> None:
        """锁到期但不退出：只把 source 标成 released，交给自动判定（老大 2026-11-17 要的）。

        以前是「满 10 分钟直接回自动」，等于托盘选的模式被悄悄取消；现在改成
        「锁 N 分钟，期间谁都不许动；到点就放开，由视频/游戏那些自动判定决定进还是退」。
        """
        with self.lock:
            if not str(self.source).startswith("tray"):
                return
            # 保留「通用视频」这个身份：只把 -released 后缀加上，托盘同步还认得出来
            if str(self.source) == "tray-generic":
                self.source = "tray-generic-released"
            else:
                self.source = "tray-released"
            self.since = time.time()
            self._write()
        log.info("模式锁到期（%s），「%s」交回自动判定", why, self.value)

    def set(self, mode: str, source: str = "api") -> dict:
        mode = (mode or "").strip().lower()
        if mode not in ("auto", "normal", "video", "anime", "live", "course", "game", "off"):
            raise ValueError("mode 只能是 auto/normal/video/anime/live/course/game/off")
        changed = False
        with self.lock:
            changed = mode != self.value
            self.value = mode
            self.source = source
            self.since = time.time()
            self._write()
        # 回调放在锁外面：它要播语音、弹气泡，里面还会再读 mode
        if changed and callable(getattr(self, "on_change", None)):
            try:
                self.on_change(mode, source)
            except Exception as exc:  # noqa: BLE001
                log.info("模式切换提示出错（不影响切换）：%s", exc)
        return {"mode": self.value, "source": self.source, "since": self.since,
                "lock_forever": bool(self.lock_forever)}

    def set_lock_forever(self, on: bool) -> None:
        """托盘「手动锁」：False=5 分钟（默认），True=一直保持。"""
        with self.lock:
            self.lock_forever = bool(on)
            self._write()


class TitleLearner(threading.Thread):
    """自己把不认识的游戏/番剧补进 titles.json（老大 2026-11-17 要的）。

    只看「像作品」的前台窗口：游戏进程（排除浏览器/编辑器/终端）或带番剧字样的浏览器标题。
    同一个标题要连着看到 `min_checks` 次才动手（切一下就走的窗口不浪费一次调用），
    问过的不再重复问（`cooldown_minutes` 内）。写进去的值是 2~3 句：游戏写玩法，
    番剧写剧情概要 + 主要人物，**不要公司/平台/发售时间这类共识**。
    """

    SKIP_APPS = ("explorer", "code", "devenv", "pycharm", "idea", "notepad", "wezterm",
                 "windowsterminal", "cmd", "powershell", "conhost", "taskmgr", "python",
                 "pythonw", "astrbot", "dsh", "chrome", "msedge", "firefox", "brave")
    BROWSER_LIKE = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "browser")
    VIDEO_HINTS = ("番剧", "动画", "第", "集", "ova", "动漫", "bilibili", "哔哩哔哩",
                   "直播", "电影", "纪录片", "综艺", "字幕")

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="title-learner", daemon=True)
        conf = cfg.get("title_learner") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", True))
        self.interval = max(20.0, float(conf.get("interval_seconds", 60)))
        self.min_checks = max(1, int(conf.get("min_checks", 2)))
        self.cooldown = float(conf.get("cooldown_minutes", 360)) * 60

    def _interesting(self, app: str, title: str) -> bool:
        low_app = (app or "").lower().replace(".exe", "")
        if not low_app or not title.strip():
            return False
        if low_app in ("", "-"):
            return False
        if any(low_app.startswith(skip) for skip in self.SKIP_APPS) and not any(
                h in title for h in self.VIDEO_HINTS):
            return False
        if len(title.strip()) < 2 or title.strip() in ("Program Manager", "Microsoft​ Edge"):
            return False
        return True

    def run(self) -> None:
        if not self.enabled:
            return
        seen: dict[tuple[str, str], list] = {}
        while True:
            time.sleep(self.interval)
            try:
                from video_watch import foreground

                app, title = foreground()
                if not self._interesting(app, title):
                    continue
                if title_hint(app, title):
                    continue                       # 小抄里已经有了
                key = (app, title)
                rec = seen.setdefault(key, [0, 0.0])
                rec[0] += 1
                if rec[0] < self.min_checks:
                    continue
                if time.time() - rec[1] < self.cooldown:
                    continue
                rec[1] = time.time()
                self.learn(app, title)
            except Exception as exc:  # noqa: BLE001
                log.warning("作品小抄自动补条目出错：%s", exc)

    def learn(self, app: str, title: str) -> None:
        parts = []
        try:
            snap = self.gateway.frames.snapshot(1)
            if snap:
                from PIL import Image

                buf = io.BytesIO()
                snap[-1][1].save(buf, "JPEG", quality=78)
                parts.append({"type": "text", "text": "这张是老大此刻的屏幕。"})
                parts.append({"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
        except Exception:  # noqa: BLE001
            pass
        ask = (
            "窗口标题：%s\n进程：%s\n\n"
            "如果这明显是一款游戏、一部番剧/动画、或者正在播的影视，只回一个 JSON：\n"
            '{"kind":"game|anime|video|other","name":"作品的中文常用名","note":"2~3 句：'
            '游戏写怎么玩/核心机制/大致流程；番剧和影视写大体剧情 + 主要人物名字。'
            '不要写公司、平台、发售时间、评分这类常识。"}\n'
            "其他情况（网页、文档、代码、聊天、工具）就回 {\"kind\":\"other\"}。只回 JSON，不要解释。"
            % (title, app)
        )
        parts.insert(0, {"type": "text", "text": ask})
        payload = {
            "model": self.cfg.get("model") or "deepseek-v4-flash",
            "max_tokens": 400, "stream": False,
            "messages": [
                {"role": "system", "content": "你在给桌面宠物维护一张作品小抄，只回 JSON。"},
                {"role": "user", "content": parts},
            ],
        }
        try:
            data = self.gateway.post_once(payload)
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("作品小抄：问不出来（%s）：%s", title[:30], exc)
            return
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            obj = json.loads(text)
        except ValueError:
            log.info("作品小抄：回答不是 JSON，跳过（%s）", title[:30])
            return
        if str(obj.get("kind") or "").lower() == "other":
            log.info("作品小抄：判断不是作品，跳过（%s）", title[:30])
            return
        name = str(obj.get("name") or "").strip()
        note = str(obj.get("note") or "").strip()
        kind = str(obj.get("kind") or "").strip().lower()
        if not name or not note:
            return
        entry = {"kind": kind, "note": note}
        updates = {name: entry}
        low_app = (app or "").lower().replace(".exe", "")
        if low_app and low_app not in self.BROWSER_LIKE:
            updates.setdefault(low_app, entry)   # 进程名也记一条，标题被吞也能命中
        try:
            total = save_title_notes(updates)
        except Exception as exc:  # noqa: BLE001
            log.warning("作品小抄：写不进去（%s）", exc)
            return
        log.info("作品小抄：自动记下「%s」（%s，共 %d 条）", name, kind or "未知", total)
        # 光知道名字不够：顺手排一课，把玩法/术语/角色也补成手册（后台慢慢查）
        if kind in ("game", "anime") and getattr(self.gateway, "lore", None) is not None:
            try:
                self.gateway.lore.ensure(name, kind, aliases=list(updates.keys()))
            except Exception as exc:  # noqa: BLE001
                log.info("资料库排课没成（不影响小抄）：%s", exc)


class GameWatcher(threading.Thread):
    """前台是游戏、并且**稳着待够 enter_seconds**，就自动进游戏模式（老大 2026-11-17 要的）。

    判定只看两样：titles.json 里 kind=game 的作品，或者 config 里点名过的进程。
    **不看键鼠活跃度**（老大专门说了不要这个条件）——挂着游戏看攻略也是在看游戏。
    退出：窗口切走 / 游戏关掉，超过 away_grace 就回自动。模式被托盘锁住的期间不许动。
    """

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="game-watch", daemon=True)
        conf = cfg.get("game_watch") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", True))
        self.poll = max(3.0, float(conf.get("poll_seconds", 5)))
        self.enter_seconds = max(10.0, float(conf.get("enter_seconds", 90)))
        self.switch_seconds = max(5.0, float(conf.get("switch_seconds", 20)))
        self.away_grace = max(30.0, float(conf.get("away_grace_seconds", 180)))
        self.extra_apps = [str(x).strip().lower().replace(".exe", "")
                           for x in (conf.get("game_apps") or []) if str(x).strip()]
        self.first_seen = 0.0
        self.last_seen = 0.0
        self.app = ""

    def _is_game(self, app: str, title: str) -> bool:
        if title_kind(app, title) == "game":
            return True
        low = (app or "").lower().replace(".exe", "")
        return bool(low) and low in self.extra_apps

    def _locked(self) -> bool:
        mode = self.gateway.mode
        if str(mode.source).startswith("tray") and not str(mode.source).endswith("released"):
            return True
        return time.time() < getattr(self.gateway, "mode_lock_until", 0.0)

    def run(self) -> None:
        if not self.enabled:
            return
        while True:
            time.sleep(self.poll)
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.warning("游戏模式判定出错：%s", exc)

    def _tick(self) -> None:
        from video_watch import foreground

        app, title = foreground()
        now = time.time()
        cur = self.gateway.mode.get()
        if self._is_game(app, title):
            self.last_seen = now
            if self.app != app:
                self.app, self.first_seen = app, now
            if not self._locked():
                if cur == "auto":
                    if now - self.first_seen >= self.enter_seconds:
                        self.gateway.mode.set(GAME_MODE, source="auto-game")
                        log.info("游戏模式：自动进入（%s｜%s）", app, title[:40])
                elif (cur in ("video", "anime", "live", "course")
                      and str(self.gateway.mode.source) in (
                          "auto-video", "auto-video-bili", "auto-anime", "auto-live", "video-leave")):
                    # 刚才自动进了视频模式，现在前台换成游戏：不用等视频那边 5 分钟才走，
                    # 短暂确认后就接管（老大 2026-11-17 说「一堆问题」里的一个）。
                    if now - self.first_seen >= self.switch_seconds:
                        self.gateway.mode.set(GAME_MODE, source="auto-game")
                        log.info("游戏模式：从视频接管（%s｜%s）", app, title[:40])
            return
        # 不是游戏了：只负责「自己自动进来的」那一次退出
        self.app = ""
        self.first_seen = 0.0
        if (cur == GAME_MODE and self.gateway.mode.source == "auto-game"
                and not self._locked() and self.last_seen
                and now - self.last_seen > self.away_grace):
            self.gateway.mode.set("auto", source="auto-game")
            log.info("游戏模式：自动退出（切走 %.0f 秒）", now - self.last_seen)



class FrameBuffer(threading.Thread):
    """每秒截一张进环形缓冲（只在内存里），识屏时把「前面几秒」拼成一张 2×2 附上。"""

    def __init__(self, cfg: dict):
        super().__init__(name="frames", daemon=True)
        conf = cfg.get("frame_buffer") or {}
        self.enabled = bool(conf.get("enabled", True))
        self.interval = max(0.2, float(conf.get("interval_seconds", 1.0)))
        self.keep = max(2, int(conf.get("keep", 5)))
        self.width = int(conf.get("frame_width", 640))
        self.quality = int(conf.get("jpeg_quality", 82))
        self.grid_frames = int(conf.get("grid_frames", 4))
        self.attach = bool(conf.get("attach_to_vision", True))
        self.idle_interval = max(1.0, float(conf.get("frame_idle_interval", 4.0)))
        self.buf: "collections.deque" = collections.deque(maxlen=self.keep)
        # 每帧的「黑屏比例」：游戏独占全屏 / 锁屏 / DRM 画面截出来就是一片黑，
        # 这种帧不该送进模型，也不该进拼图（2026-11-17 老大提的）
        self.darks: "collections.deque" = collections.deque(maxlen=max(12, self.keep))
        self.black_ratio = float(conf.get("black_ratio", 0.92))
        self.lock = threading.Lock()
        self.last_error = ""

    def is_black(self, n: int = 2, fresh: float = 15.0, ratio: float | None = None) -> bool:
        """最近 n 张是不是都几乎全黑（默认 92% 以上像素接近纯黑）。"""
        thr = self.black_ratio if ratio is None else float(ratio)
        now = time.time()
        with self.lock:
            rows = [(ts, r) for ts, r in list(self.darks) if now - ts <= fresh]
        if len(rows) < n:
            return False
        return all(r >= thr for _ts, r in rows[-n:])

    def dark_now(self) -> float:
        with self.lock:
            return self.darks[-1][1] if self.darks else 0.0

    def run(self) -> None:
        if not self.enabled:
            return
        try:
            from PIL import ImageGrab
        except Exception as exc:  # noqa: BLE001
            self.last_error = "PIL 不可用：%s" % exc
            log.warning("帧缓冲关闭：%s", self.last_error)
            return
        # 自适应采样：
        #   画面在动 → 1 秒一张（拼图要「前几秒」）
        #   连续几次没变化 → 放慢到 frame_idle_interval（默认 4 秒）
        #   但即使完全静止，也每 idle 秒存一张，免得缓冲里全是十分钟前的旧帧
        idle = self.idle_interval
        still_rounds = 0
        last_sig = None
        last_store = time.time()
        while True:
            now = time.time()
            try:
                shot = ImageGrab.grab()
                h = max(1, int(shot.height * self.width / shot.width))
                shot = shot.resize((self.width, h)).convert("RGB")
                # 32x18 灰度指纹 + 逐像素差：只有「整片画面变了」才算变化。
                # 桌宠自己在角落动、光标闪、进度条走，都不该把采样率拉满。
                small = shot.resize((32, 18)).convert("L")
                px = list(small.getdata())
                if last_sig is None:
                    changed = True
                else:
                    diff = sum(1 for a, b in zip(px, last_sig) if abs(a - b) > 12)
                    changed = diff / len(px) > 0.03   # 超过 3% 的像素变了才算
                last_sig = px
                # 这一张有多黑：纯黑占 92% 以上就不进缓冲（不送给模型看）
                dark = sum(1 for v in px if v < 24) / max(1, len(px))
                with self.lock:
                    self.darks.append((now, dark))
                stale = (now - last_store) >= idle
                if dark >= self.black_ratio:
                    still_rounds += 1          # 黑屏不算「画面变了」，也放慢采样
                    time.sleep(idle)
                    continue
                if changed or stale:
                    with self.lock:
                        self.buf.append((now, shot))
                    last_store = now
                    still_rounds = 0
                else:
                    still_rounds += 1
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
            time.sleep(idle if still_rounds >= 2 else self.interval)

    def snapshot(self, n: int) -> list:
        with self.lock:
            items = list(self.buf)
        return items[-n:] if n > 0 else items

    # ---- 拼图
    def grid_jpeg(self, count: int = 0, cols: int = 2) -> tuple[str, int]:
        """把缓冲里前 N 张按时间拼成 cols 列的网格（左上→右下），返回 (base64, 张数)。

        游戏模式会要更多帧（6 张、3 列），因为「精彩操作」得连着几秒看才看得出来。
        """
        n = max(1, int(count or self.grid_frames))
        cols = max(1, int(cols))
        items = self.snapshot(n)
        if not items:
            return "", 0
        try:
            from PIL import Image
        except Exception:
            return "", 0
        w, h = items[0][1].size
        rows = (len(items) + cols - 1) // cols
        canvas = Image.new("RGB", (w * cols, h * rows), (24, 24, 24))
        for i, (_ts, im) in enumerate(items):
            canvas.paste(im, ((i % cols) * w, (i // cols) * h))
        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=self.quality)
        return base64.b64encode(buf.getvalue()).decode(), len(items)

    def attach_to(self, msg: dict, count: int = 0, cols: int = 2) -> int:
        """把拼图插进这条消息（在它自己的当前截图之前）。"""
        b64, n = self.grid_jpeg(count, cols)
        if not b64:
            return 0
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return 0
        idx = next((i for i, p in enumerate(content)
                    if isinstance(p, dict) and p.get("type") in ("image_url", "input_image")), -1)
        note = {
            "type": "text",
            "text": ("这 %d 张是刚才几秒里按时间截的，拼成 %d 列（左上 → 右下，从左往右、从上往下看）；"
                     "紧接着的那张才是此刻的屏幕。先别逐格描述，只在说得出变化时才提。" % (n, cols)),
        }
        image = {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}}
        if idx >= 0:
            content[idx:idx] = [note, image]
        else:
            content.extend([note, image])
        msg["content"] = content
        return n


class Ledger:
    """识屏记录 + 「较久远那部分」的定时摘要。

    粗账本全留着（谁都可能要用），但**注入时只给两样**：最近一小段原文 + 一段摘要，
    这样窗口换得再多，塞进提示词的也只是一两行近况加两三句概括。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.path = cfg["ledger_path"]
        if not os.path.isabs(self.path):
            self.path = os.path.join(APP_DIR, self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.digest_path = os.path.join(APP_DIR, "digest.json")
        self.lock = threading.Lock()

    # ---- 写
    def add(self, scenario: str, user_text: str, reply: str, extra: dict | None = None) -> None:
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "epoch": int(time.time()),
            "scenario": scenario,
            "user": (user_text or "")[:1200],
            "assistant": (reply or "")[:2000],
        }
        if extra:
            row.update(extra)
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- 读
    def rows(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("scenario") == "vision":
                        out.append(row)
        except OSError:
            return []
        return out

    @staticmethod
    def app_of(row: dict) -> str:
        text = str(row.get("user") or "")
        m = APP_RE.search(text)
        if m:
            return m.group(1).strip()[:40]
        return ""

    def recent_vision(self, limit: int, max_minutes: float) -> list[dict]:
        """最近的识屏：同一时间窗内、同一进程只留最新一条，避免十个浏览器标签占满。"""
        cutoff = time.time() - max_minutes * 60
        picked: list[dict] = []
        seen_apps: dict[str, int] = {}
        for row in reversed(self.rows()):
            if row.get("epoch", 0) < cutoff:
                break
            app = self.app_of(row) or "-"
            if seen_apps.get(app, 0) >= 1:
                continue
            seen_apps[app] = 1
            picked.append(row)
            if len(picked) >= limit:
                break
        return list(reversed(picked))

    def recent_vision_for_app(
        self,
        app: str,
        *,
        max_minutes: float,
        budget_chars: int,
        item_chars: int,
        max_items: int,
    ) -> list[dict]:
        """同 App 优先、按字符预算装：先装满当前窗口的，再用别的窗口补。

        为什么不按「条数」：同一段时间里，看番那 10 条里可能有 6 条是「好可爱」，
        按条数装就白装了；按字符预算装，才有空间把有用的都带上。
        """
        cutoff = time.time() - max_minutes * 60
        rows = [r for r in self.rows() if r.get("epoch", 0) >= cutoff]
        key = (app or "").strip().lower()
        same = [r for r in rows if key and self.app_of(r).strip().lower() == key]
        other = [r for r in rows if not (key and self.app_of(r).strip().lower() == key)]

        picked: list[dict] = []
        used = 0
        for row in list(reversed(same)) + list(reversed(other)):
            text = (row.get("assistant") or "").strip()
            if not text:
                continue
            cost = min(len(text), item_chars) + 24  # 24 ≈ 时间戳/程序名的开销
            if picked and used + cost > budget_chars:
                continue
            picked.append(row)
            used += cost
            if len(picked) >= max_items:
                break
        return list(reversed(picked))

    # ---- 摘要
    def digest(self) -> dict:
        try:
            with open(self.digest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_digest(self, text: str, covered_until: int) -> None:
        with self.lock:
            with open(self.digest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"text": text, "covered_until": covered_until, "ts": int(time.time())},
                    f,
                    ensure_ascii=False,
                )

    def pending_for_digest(self, since: int, until: int, limit: int) -> list[dict]:
        out = [r for r in self.rows() if since < r.get("epoch", 0) <= until]
        return out[-limit:]


APP_RE = re.compile(r"前台应用为\s*([^|\n）)]+)")
# 同一行里「应用 | 窗口标题」两段都抓出来：标题里通常就写着游戏/番剧的名字
SCREEN_META_RE = re.compile(r"前台应用为\s*([^|\n）)]+)(?:\s*\|\s*([^\n）)]+))?")


class Facts:
    """老大亲口教过的事（「那个人是遐蝶，不是希尔」这种）。

    为什么要单独存：`ledger.jsonl` 的识屏上下文只挑 `scenario=vision` 的行，
    聊天里教的名字传不到下一次识屏；而「角色认错」正是这么来的。
    这里把「像在纠正/教她」的话单独记一份，聊天、语音、识屏都读同一份，7 天有效。
    """

    # 只收「像在教她」的话，避免把普通聊天都记成事实
    TEACH_RE = re.compile(
        r"(?:那个人|这个人|这个角色|那个角色|这角色|那角色|这姑娘|那姑娘|她|他|它|这货|那货)"
        r"\s*(?:是|叫|名叫|叫做|念作|读作)"
        r"|不是.{1,18}(?:而是|，|,)\s*是"
        r"|(?:记住|记一下|记着|记好)[，,:：]?\s*.{2,60}"
        r"|(?:名字|名字叫|叫)\s*[:：]\s*[\u4e00-\u9fffA-Za-z·]{2,12}"
    )
    BLOCK = ("是不是", "是什么", "是谁", "哪", "怎么", "为什么", "什么", "？", "?")

    def __init__(self, cfg: dict):
        conf = cfg.get("facts") or {}
        self.enabled = bool(conf.get("enabled", True))
        self.path = os.path.join(APP_DIR, str(conf.get("path") or "facts.jsonl"))
        self.max_age = float(conf.get("max_age_hours", 168)) * 3600
        self.max_items = max(20, int(conf.get("max_items", 300)))
        self.inject_items = max(1, int(conf.get("inject_items", 3)))
        self.lock = threading.Lock()
        self.items = self._load()

    def _load(self) -> list[dict]:
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("text"):
                        out.append(row)
        except OSError:
            pass
        return out[-self.max_items:]

    def _append(self, row: dict) -> None:
        with self.lock:
            self.items.append(row)
            if len(self.items) > self.max_items * 2:
                self.items = self.items[-self.max_items:]
                try:
                    with open(self.path, "w", encoding="utf-8", newline="\n") as f:
                        for item in self.items:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                except OSError:
                    pass
                return
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.debug("事实没存下（不影响这轮）：%s", exc)

    def add(self, text: str, app: str = "", title: str = "", source: str = "chat") -> bool:
        if not self.enabled:
            return False
        text = re.sub(r"\s+", " ", str(text or "")).strip(" 　")
        text = re.sub(r"^(?:记住|记一下|记着|记好)[，,:：]?\s*", "", text).strip(" 　")
        if not (3 <= len(text) <= 120):
            return False
        if any(w in text for w in self.BLOCK):
            return False
        if not self.TEACH_RE.search(text):
            return False
        now = time.time()
        with self.lock:
            for row in reversed(self.items[-8:]):
                if row.get("text") == text and now - float(row.get("ts") or 0) < 3600:
                    return False
        row = {"ts": int(now), "text": text, "app": (app or "")[:40],
               "title": (title or "")[:80], "source": source}
        self._append(row)
        log.info("记下老大教的事（%s）：%s", source, text[:40])
        return True

    def block(self, app: str = "", title: str = "", limit: int | None = None,
              max_chars: int = 320) -> str:
        if not self.enabled or not self.items:
            return ""
        now = time.time()
        rows = [r for r in self.items
                if now - float(r.get("ts") or 0) <= self.max_age and str(r.get("text") or "").strip()]
        if not rows:
            return ""
        low_app, low_title = (app or "").lower(), (title or "").lower()

        def rank(row: dict) -> tuple:
            score = 0
            ra = str(row.get("app") or "").lower()
            rt = str(row.get("title") or "").lower()
            if low_app and ra and ra == low_app:
                score += 3
            if low_title and rt and (low_title in rt or rt in low_title):
                score += 3
            if not ra and not rt:
                score += 1
            return score, float(row.get("ts") or 0)

        rows.sort(key=rank, reverse=True)
        picked: list[str] = []
        used = 0
        for row in rows:
            text = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
            tag = str(row.get("title") or row.get("app") or "").strip()
            line = "- %s%s" % (text, ("（%s）" % tag) if tag else "")
            if used + len(line) > max_chars:
                break
            picked.append(line)
            used += len(line)
            if len(picked) >= (limit or self.inject_items):
                break
        if not picked:
            return ""
        return ("【老大教过你的】（他亲口说的，比你猜的准；认出来就直接用，别反驳、别装不知道）\n"
                + "\n".join(picked))


def build_ledger_block(rows: list[dict], digest: str = "") -> str:
    if not rows and not digest:
        return ""
    lines = [
        "【你之前在桌面上亲眼看到、并亲口说过的话】（同一台电脑上的识屏记录；不是你用户现在说的话，",
        "不要在回复里复述原文，也不要提「记录」「账本」「摘要」。时间是给你判断的，**别念数字**：",
        "10 分钟内才说「刚才」，10～30 分钟说「刚才那会儿/前一会儿」，半小时以上只能说「之前」「早先」，",
        "而且只有跟眼下接得上才提；这些只是参考，不是必须提。）",
    ]
    if digest:
        lines.append("更早之前（几十分钟到几个小时前的那些，已经压成一段了）：" + digest.strip())
    for row in rows:
        stamp = row.get("ts", "")[11:16]
        app = Ledger.app_of(row)
        user = (row.get("user") or "").strip().replace("\n", " ")
        reply = (row.get("assistant") or "").strip().replace("\n", " ")[:220]
        age_min = max(0, int((time.time() - float(row.get("epoch", time.time()))) / 60))
        age = "刚刚" if age_min <= 1 else (
            "%d分钟前" % age_min if age_min < 60 else "%d小时%d分前" % (age_min // 60, age_min % 60))
        head = "桌面上" + ("（%s）" % app if app else "")
        if user:
            lines.append("- %s（%s）%s：%s" % (stamp, age, head, user[:160]))
        if reply:
            lines.append("- %s（%s）你说过：%s" % (stamp, age, reply))
    return "\n".join(lines)


SELF_REF_RE = re.compile(r"(就是本鲸鱼娘|不就是我|那是我|是我自己|长得像我|我的化身|坐在你桌面上|是我呀)")
ID_HINT_RE = re.compile(r"（身份提示：.*?）", re.S)

# 桌宠原来塞进来的这句，等于每次都提醒她「看到你就说你自己」——她桌面上永远有自己，
# 于是就变成每张截图都要认领一次。这里换成「可以说，但别老说」。
NEW_ID_HINT = (
    "（身份提示：画面角落那个动漫风格的桌宠就是你自己。"
    "但不要每次都提——只有老大主动问你、或者你正好在做一件明显的事时才顺口说一句；"
    "同一个「那个是不是我」的梗，隔很久才用一次。）"
)


def soften_identity_hint(text: str) -> str:
    if not text or "身份提示" not in text:
        return text
    return ID_HINT_RE.sub(NEW_ID_HINT, text)


def self_ref_recent(rows: list[dict], minutes: float) -> bool:
    """最近有没有刚说过「那个是我」这类话。"""
    cutoff = time.time() - minutes * 60
    for row in rows:
        if row.get("epoch", 0) < cutoff:
            continue
        if SELF_REF_RE.search(str(row.get("assistant") or "")):
            return True
    return False


WATCH_MODES = ("video", "anime", "live", "course")
# 游戏模式：跟看片一样「专心」，但**不吃字幕/转写**（游戏没有字幕可读，也不该念界面文字）
GAME_MODE = "game"
FOCUS_MODES = WATCH_MODES + (GAME_MODE,)
# 问到「屏幕上的东西」的信号：这类问题必须带上当前前台/截图才答得出来
SCREEN_QUESTION_HINTS = (
    "游戏", "番", "视频", "画面", "屏幕", "直播", "在玩", "在看", "玩什么", "看什么",
    "这部", "那部", "这个是", "那个是", "我在干", "我这样",
)
_MODE_WORD = {"video": "视频", "anime": "番剧", "live": "直播", "course": "网课", "game": "游戏"}


def build_watch_focus(mode: str) -> str:
    """看片/看番/**打游戏**时的「专心」规矩。

    老大原话：看番的时候她老说「这个点还不睡」，很影响体验；字幕她也当成稿子念。
    所以这里把两件事一起按住：**不许拿时间/作息当话题**，**字幕只当剧情参考**。
    游戏模式（2026-11-17 加）同一套，但不吃字幕，改成一件事：别指挥、别剧透、别念界面文字。
    另外开头先明确一句「老大此刻在干嘛」，免得她把状态说错（比如明明在打游戏却说「你在看视频」）。
    """
    kind = _MODE_WORD.get(mode, "视频")
    if mode == GAME_MODE:
        return "\n".join([
            "【此刻】老大正在**打游戏**，你是坐在旁边陪玩的那只鲸鱼娘——不是观众，也不是解说。",
            "【陪玩的规矩】这一轮只聊游戏里的内容。硬规矩：",
            "1. 不许提时间，也不许拐着弯提：不说几点、不说「这么晚」「这个点」「后半夜」「天都快亮了」，"
            "不催他睡觉、不劝他歇会儿、不操心眼睛、不说「明天还要上班」。打游戏的时候听到这些很扫兴。",
            "2. 认得出是什么游戏，就按你记得的说（玩法、这个角色是谁、这关大概什么机制）；"
            "认得出画面里的角色也直接说名字。不熟就只说名字和看得到的样子，别编。",
            "3. 前面附的那几张是刚才几秒的连续画面，**当一段动作看**——不要用「第一张/第二张/第几张」这种说法。"
            "能看到一串动作（连招、极限躲、反打、一波清完、开大爆发）才算精彩，真看到了就带着情绪喊一句，别泼冷水。",
            "4. 挑一个真有意思的点说，一两句就够；不报进度、不刷屏。",
        ])
    return "\n".join([
        "【此刻】老大正在**%s**（你陪着他一起看），不是在做别的事，也没在跟你闲聊。"
        % ("看番" if mode == "anime" else ("看视频" if mode == "video" else ("看直播" if mode == "live" else "上网课"))),
        "【陪看的规矩】这一轮只聊内容：",
        "1. 不许提时间，也不许拐着弯提：不说几点、不说「这么晚」「这个点」「后半夜」「天都快亮了」，"
        "不催他睡觉、不劝他歇会儿、不操心眼睛、不说「明天还要上班」。看片的时候听到这些很扫兴，一个字都别提。",
        "2. 字幕、台词、语音转写都是给你看懂剧情用的参考，**不是你该念的稿子**："
        "不要念字幕原文、不要整句复述台词、不要说「字幕说」「上面写着」「台词里」「旁白说」这种话。"
        "用你自己的话讲发生了什么、你怎么看、你猜接下来会怎样。",
        "3. 画面也别逐条播报。挑一个真有意思的点说，一两句就够；不报进度、不剧透、不总结本集剧情。",
        "4. 认得出画面里是哪个角色，就直接叫名字；认不出就说外形（蓝头发、戴帽子那个）。",
        "5. 更早记录里带的时间只是给你判断新旧用的，不是话题。",
    ])


def build_vision_context(rows: list[dict], hide_clock: bool = False) -> str:
    """识屏请求的前情提要。带「多久以前」的标签，但**标签只给她判断，不许念出来**。"""
    if not rows:
        return ""
    now = time.time()
    if hide_clock:
        # 看片模式：连钟表时间都不给（给了她就会拿「都一点了」当话题）
        lines = [
            "【你之前看到过的】（同一台电脑上的识屏记录，不是现在这张图）",
            "每条只标「多久以前」。这些数字是给你判断新旧用的，**不要念出来**；",
            "说的时候只能换成「刚才」「刚才那会儿」「之前」「早先」这类词，或者直接用内容指代。",
            "这些只是参考，不是必须提——眼下这张图够答就只说这张图，别每次都往回带。",
        ]
    else:
        lines = [
            "【你之前看到过的】（同一台电脑上的识屏记录，不是现在这张图）",
            "每条后面标着「钟表时间（多久以前）」。这些数字是给你判断用的，**不要念出来**；",
            "说的时候只能换成「刚才」「刚才那会儿」「之前」「早先」「下午那会儿」这类词，或者直接用内容指代。",
            "时间分档：10 分钟以内才可以说「刚才/刚刚」；10～30 分钟说「刚才那会儿」「前一会儿」；",
            "半小时以上只有跟眼下明显相关才提，而且只能说「之前」「早先」，不许说「刚才」，也不许报「多少分钟前」。",
            "这些只是参考，不是必须提——眼下这张图够答就只说这张图，别每次都往回带。",
        ]
    for row in rows:
        stamp = row.get("ts", "")[11:16]
        age_min = max(0, int((now - float(row.get("epoch", now))) / 60))
        if age_min <= 10:
            age = "%d分钟前" % age_min if age_min > 0 else "刚刚"
        elif age_min < 60:
            age = "%d分钟前" % age_min
        else:
            age = "%d小时%d分前" % (age_min // 60, age_min % 60)
        app = Ledger.app_of(row) or "某个窗口"
        said = (row.get("assistant") or "").strip().replace("\n", " ")
        if SELF_REF_RE.search(said):
            # 旧话里那句「那不就是我」不再回喂，不然她会照着复读
            said = SELF_REF_RE.sub("", said).strip("，,。 　")
        said = said[:int(90)]
        if said:
            # 看片时连钟表时间都不给：给了她就会拿「都一点了」当话题
            when = age if hide_clock else "%s（%s）" % (stamp, age)
            lines.append("- %s 在 %s，你说过：%s" % (when, app, said))
    return "\n".join(lines)


class Digester(threading.Thread):
    """每隔几小时，把「较久远」的识屏记录压成一段摘要，注入时用它代替一堆原文。"""

    def __init__(self, cfg: dict, ledger: Ledger, gateway):
        super().__init__(name="digester", daemon=True)
        self.cfg = cfg
        self.ledger = ledger
        self.gateway = gateway
        conf = cfg.get("ledger_digest") or {}
        self.enabled = bool(conf.get("enabled", True))
        self.interval = float(conf.get("interval_hours", 3)) * 3600
        self.min_entries = int(conf.get("min_entries", 8))
        self.max_entries = int(conf.get("max_entries", 300))
        self.max_chars = int(conf.get("max_chars", 240))
        self.retention_hours = float(conf.get("retention_hours", 24))
        self.recent_minutes = float(cfg.get("inject_recent_minutes", 45))

    def run(self) -> None:
        if not self.enabled:
            return
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("识屏摘要失败: %s", exc)
            time.sleep(self.interval)

    def run_once(self, force: bool = False) -> dict:
        """返回 {"ok": bool, "text": str, "count": n, "reason": str}。"""
        digest = self.ledger.digest()
        since = int(digest.get("covered_until") or 0)
        now = time.time()
        until = int(now - self.recent_minutes * 60)
        if until <= since:
            return {"ok": False, "text": "", "count": 0, "reason": "没有更早的记录可摘"}
        rows = self.ledger.pending_for_digest(since, until, self.max_entries)
        if len(rows) < self.min_entries and not force:
            return {"ok": False, "text": "", "count": len(rows), "reason": "记录还不够多"}
        if not rows:
            return {"ok": False, "text": "", "count": 0, "reason": "没有记录"}

        lines = []
        for row in rows[-self.max_entries:]:
            stamp = row.get("ts", "")[5:16].replace("T", " ")
            app = Ledger.app_of(row) or "某个窗口"
            said = (row.get("assistant") or "").strip().replace("\n", " ")[:80]
            lines.append("[%s] 前台=%s | 她说：%s" % (stamp, app, said))
        system = (
            "你是桌面识屏记录的整理员。把下面这些「泡泡在桌面上看到并说过的话」浓缩成两三句中文，"
            "只说老大这段时间主要在用什么、在做什么、你当时怎么反应的；不要逐条罗列，不要编造，"
            "不要提「记录」「摘要」这类词，也不要出现小括号。"
        )
        payload = {
            "model": self.cfg.get("digest_model") or "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": "\n".join(lines)},
            ],
            "max_tokens": max(200, self.max_chars * 3),
            "stream": False,
        }
        data = self.gateway.post_once(payload)
        try:
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            text = ""
        if not text:
            return {"ok": False, "text": "", "count": len(rows), "reason": "模型没给出摘要"}
        text = text[: self.max_chars]
        covered = int(rows[-1].get("epoch") or until)
        if not force:
            self.ledger.save_digest(text, covered)
        log.info("识屏摘要：%d 条 → %d 字（覆盖到 %s）", len(rows), len(text),
                 time.strftime("%H:%M", time.localtime(covered)))
        return {"ok": True, "text": text, "count": len(rows), "reason": ""}


# ------------------------------------------------------------------------ 判定
def message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(i.get("text", "")) for i in content if isinstance(i, dict) and i.get("type") == "text"]
        return "\n".join(parts)
    return ""


def has_image(content) -> bool:
    if isinstance(content, list):
        return any(
            isinstance(i, dict) and i.get("type") in ("image_url", "input_image") for i in content
        )
    return False


def detect_scenario(messages: list[dict], markers: list[str]) -> str:
    if any(has_image(m.get("content")) for m in messages):
        return "vision"
    system_text = "\n".join(message_text(m.get("content")) for m in messages if m.get("role") == "system")
    tail = message_text(messages[-1].get("content")) if messages else ""
    joined = system_text + "\n" + tail
    return "vision" if any(m and m in joined for m in markers) else "chat"


def replace_owner_word(messages: list[dict]) -> None:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and "主人" in content:
            msg["content"] = content.replace("主人", "老大")
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str) and "主人" in item["text"]:
                    item["text"] = item["text"].replace("主人", "老大")


# ------------------------------------------------------------- DSH 状态播报（读事件流）
class DshWatcher(threading.Thread):
    """盯 dsh-pet-bridge/*.jsonl，用同一个音色念桌宠会显示的那些状态。"""

    TEMPLATES = {
        "start": ["{name}开工了，本鲸鱼娘替你盯着。", "又干活啊……行吧，本小姐看着。"],
        "tool": ["正在用{tool}……", "{tool}跑起来了。"],
        "done": ["这轮干完了，老大去看看。", "收工，本鲸鱼娘要吃口白饭。"],
        "error": ["呜，{tool}报错了。", "{tool}没跑通，老大看一眼。"],
        "ask": ["老大，有个问题在等你拍板。", "喂，该你决定了。"],
    }

    def __init__(self, cfg: dict, speech: SpeechPipeline):
        super().__init__(name="dsh-watcher", daemon=True)
        self.cfg = cfg
        self.speech = speech
        self.dir = cfg["dsh"]["event_dir"]
        self.offsets: dict[str, int] = {}
        self.tools: dict[str, float] = {}
        self.last_tool_at = 0.0
        self.started = False
        self._seen: dict[tuple, float] = {}

    def run(self) -> None:
        if not self.cfg["dsh"].get("enabled"):
            return
        poll = float(self.cfg["dsh"].get("poll_seconds", 1.5))
        while True:
            try:
                self._tick()
            except Exception as exc:
                log.warning("DSH 事件处理失败: %s", exc)
            time.sleep(poll)

    def _tick(self) -> None:
        files = sorted(
            glob.glob(os.path.join(self.dir, "dsh*.jsonl")), key=os.path.getmtime, reverse=True
        )[:3]
        for path in files:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if path not in self.offsets:
                # 首轮：直接跳到文件末尾，不重播历史事件
                self.offsets[path] = size
                continue
            if size < self.offsets[path]:
                self.offsets[path] = 0
            with open(path, "rb") as f:
                f.seek(self.offsets[path])
                chunk = f.read()
                self.offsets[path] = f.tell()
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._handle(json.loads(line))
                except Exception:
                    continue

    def _name(self, row: dict) -> str:
        return (row.get("sessionName") or row.get("projectName") or "会话").strip()[:20]

    def _handle(self, row: dict) -> None:
        event = row.get("event")
        now = time.time()
        # 同一个事件别念两遍：桥接可能重写同一行，或者（更常见的）两份网关同时
        # 在跑、各读一遍。按「事件 + 会话 + step + callId」在 5 秒内去重。
        key = (
            str(event or ""),
            str(row.get("sessionId") or ""),
            str(row.get("step") or ""),
            str(row.get("callId") or ""),
        )
        last = self._seen.get(key)
        if last is not None and now - last < 5.0:
            log.debug("DSH 重复事件，跳过：%s", key)
            return
        self._seen[key] = now
        if len(self._seen) > 400:
            cutoff = now - 60.0
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        if event == "turn/start":
            self.speech.say(random.choice(self.TEMPLATES["start"]).format(name=self._name(row)), "dsh")
        elif event == "turn/end":
            self.speech.say(random.choice(self.TEMPLATES["done"]), "dsh")
        elif event == "tool/call" and self.cfg["dsh"].get("speak_tools", True):
            tool = str(row.get("tool") or "工具")[:12]
            gap = float(self.cfg["dsh"].get("tool_min_interval", 25))
            if now - self.last_tool_at < gap or now - self.tools.get(tool, 0) < gap * 2:
                return
            self.last_tool_at, self.tools[tool] = now, now
            self.speech.say(random.choice(self.TEMPLATES["tool"]).format(tool=tool), "dsh")
        elif event == "tool/result" and row.get("ok") is False:
            tool = str(row.get("tool") or "工具")[:12]
            if now - self.tools.get("err:" + tool, 0) > 60:
                self.tools["err:" + tool] = now
                self.speech.say(random.choice(self.TEMPLATES["error"]).format(tool=tool), "dsh")
        elif event == "question/requested":
            self.speech.say(random.choice(self.TEMPLATES["ask"]), "dsh")


# ------------------------------------------------------------------------- 网关
class TtsGuard(threading.Thread):
    """盯着 TTS 端口：没起来就拉起来，起不来就等着。

    2026-11-17 的问题：开机时网关先起、GPT-SoVITS 还在加载（60~90 秒），
    这期间第一次识屏根本没声音；TTS 中途崩了也没人管。这个线程就是补这个洞：
      * 每 check_seconds 探一次 9882；
      * 没在跑就按 config 里的 launch 命令拉起来（顺手写 .gsv-started，免得守护脚本再拉一个）；
      * 语音请求遇到「连不上」时会调 ensure(timeout)，等它起来再重试，而不是直接丢句。
    """

    def __init__(self, cfg: dict):
        super().__init__(name="tts-guard", daemon=True)
        tts = cfg.get("tts") or {}
        conf = tts.get("guard") or {}
        self.cfg = cfg
        self.enabled = bool(conf.get("enabled", True))
        self.check = max(5.0, float(conf.get("check_seconds", 15)))
        self.wait = float(conf.get("wait_seconds", 120))
        self.cooldown = float(conf.get("relaunch_cooldown_seconds", 90))
        api = str(tts.get("api") or "http://127.0.0.1:9882")
        try:
            parsed = urllib.parse.urlparse(api)
            self.host = parsed.hostname or "127.0.0.1"
            self.port = int(parsed.port or 9882)
        except ValueError:
            self.host, self.port = "127.0.0.1", 9882
        launch = conf.get("launch") or {}
        self.python = str(launch.get("python") or "")
        self.script = str(launch.get("script") or "")
        self.args = [str(a) for a in (launch.get("args") or [])]
        self.workdir = str(launch.get("workdir") or APP_DIR)
        self.stdout = str(launch.get("stdout") or os.path.join(APP_DIR, "logs", "gsv_stdout.log"))
        self.stderr = str(launch.get("stderr") or os.path.join(APP_DIR, "logs", "gsv_stderr.log"))
        self.marker = str(launch.get("marker") or os.path.join(APP_DIR, "logs", ".gsv-started"))
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._last_launch = 0.0
        self.launches = 0
        self.last_error = ""

    def port_up(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=1.0):
                return True
        except OSError:
            return False

    def can_launch(self) -> bool:
        return bool(self.python and self.script
                    and os.path.exists(self.python) and os.path.exists(self.script))

    def _launch(self) -> None:
        with self._lock:
            now = time.time()
            if self._proc is not None and self._proc.poll() is None:
                return
            if now - self._last_launch < self.cooldown:
                return
            self._last_launch = now
            if not self.can_launch():
                self.last_error = "没配 launch 命令（tts.guard.launch）"
                return
            try:
                os.makedirs(os.path.dirname(self.stdout) or ".", exist_ok=True)
                out = open(self.stdout, "ab", buffering=0)
                err = open(self.stderr, "ab", buffering=0)
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                self._proc = subprocess.Popen(
                    [self.python, self.script] + self.args,
                    cwd=self.workdir, stdout=out, stderr=err, creationflags=flags,
                )
                try:
                    with open(self.marker, "w", encoding="utf-8") as f:
                        f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
                except OSError:
                    pass
                self.launches += 1
                log.warning("TTS 没在跑，拉起 GPT-SoVITS（pid %s）", self._proc.pid)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("TTS 拉不起来（%s）：%s", self.python, exc)

    def ensure(self, timeout: float = 0.0) -> bool:
        """确保 TTS 可用：没起来就拉，最多等 timeout 秒。"""
        if self.port_up():
            return True
        if not self.enabled:
            return False
        self._launch()
        if timeout <= 0:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            if self.port_up():
                log.info("TTS 已就绪（等了 %.0f 秒）", timeout - max(0.0, deadline - time.time()))
                return True
        self.last_error = "等了 %.0f 秒还没起来" % timeout
        return False

    def status(self) -> dict:
        return {"enabled": self.enabled, "up": self.port_up(), "launches": self.launches,
                "error": self.last_error}

    def run(self) -> None:
        if not self.enabled:
            return
        time.sleep(2.0)
        while True:
            try:
                if not self.port_up():
                    self._launch()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
            time.sleep(self.check)


class Gateway:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.speech = SpeechPipeline(cfg)
        self.speech._gateway = self
        # TTS 守护：开机时 TTS 往往还在加载，没它就绪就不开口（第一次识屏哑掉就是这么来的）
        self.tts_guard = TtsGuard(cfg)
        self.tts_guard.start()
        self.ledger = Ledger(cfg)
        # 老大亲口教的事（「那个人是遐蝶」）：聊天/语音/识屏共用一份，跨通道不认错
        self.facts = Facts(cfg)
        self.api_key = cfg.get("api_key") or read_credential(cfg.get("api_key_credential_target", ""))
        if not self.api_key:
            log.error("没有拿到 API Key：config.api_key 与 Windows 凭据都为空")
        else:
            log.info("API Key 就绪（%s...）", self.api_key[:6])
        self.dsh = DshWatcher(cfg, self.speech)
        self.dsh.start()
        self.digester = Digester(cfg, self.ledger, self)
        self.digester.start()
        self.mode = Mode(cfg)
        self.mode_lock_until = 0.0
        try:
            _src = str(self.mode.source)
            if (getattr(self.mode, "lock_forever", False)
                    and _src.startswith("tray") and not _src.endswith("released")):
                self.mode_lock_until = float("inf")
        except Exception:  # noqa: BLE001
            pass
        # 每次模式真的变了，就播一句 + 冒个泡（两种都是预合成好的固定句子）
        self.mode.on_change = self._announce_mode
        self.divination = DivinationWatcher(cfg, self.speech)
        self.last_vision_at = time.time()
        self.frames = FrameBuffer(cfg)
        self.frames.start()
        threading.Thread(target=self.speech.warm_pre_cues, name="precue-warmer", daemon=True).start()
        threading.Thread(target=self._warm_dsh_phrases, name="dsh-warmer", daemon=True).start()
        self.divination.start()
        self.look_guard = LookGuard(cfg, self)
        self.look_guard.start()
        self.mouse_look = MouseLook(cfg, self)
        self.mouse_look.start()
        # 通用视频模式：前台播放器 + 环回有声 + 键鼠低活跃 → 自动判定；不是 B 站就靠它
        self.video = VideoWatcher(cfg, self)
        self.video.start()
        # 看到不认识的游戏/番剧，自己查一下补进 titles.json（作品小抄）
        self.title_learner = TitleLearner(cfg, self)
        self.title_learner.start()
        # 作品的「肚子里的资料」：自己查百科+攻略 → 总结成本地手册；识屏/语音按需检索
        self.lore = Lore(cfg, self)
        self.lore_keeper = LoreKeeper(cfg, self, self.lore)
        self.lore.keeper = self.lore_keeper
        self.lore_keeper.start()
        self.lore_progress = ProgressKeeper(cfg, self, self.lore)
        self.lore_progress.start()
        # 前台是游戏、稳着待够 2 分钟：自动进游戏模式（不看键鼠活跃度）
        self.game_watch = GameWatcher(cfg, self)
        self.game_watch.start()
        # B站视频模式：从 Edge 调试端口读播放进度（方案 B），按时间轴切字幕
        self.player = PlayerWatch(cfg, self)
        self.player.start()
        # 头顶原生气泡（独立 Qt 小工，按需拉起）
        self.bubble = BubbleBridge(cfg)
        self.bubble.start()
        # 托盘里手动选的模式锁 10 分钟，超时回自动
        threading.Thread(target=self._mode_lock_loop, name="mode-lock", daemon=True).start()
        # 桌宠自己的「主动识屏」开关 = 我们的总开关：她开我们就自动，她关我们就关闭
        self._proactive_self: tuple[bool, float] | None = None
        threading.Thread(target=self._proactive_sync_loop, name="proactive-sync", daemon=True).start()
        # 语音模式：热键开录 → VAD 断句 → 9881 转写 → 模型 → 她开口 + 气泡
        self.voice = VoiceMode(cfg, self)
        self.voice.start()
        self.stats = {"requests": 0, "vision": 0, "spoken": 0}

    # ---- upstream
    def upstream_url(self) -> str:
        return self.cfg["upstream_base_url"].rstrip("/") + self.cfg["upstream_chat_path"]

    def open_stream(self, payload: dict):
        body = dict(payload)
        body["stream"] = True
        req = urllib.request.Request(
            self.upstream_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "Accept": "text/event-stream",
            },
        )
        return urllib.request.urlopen(req, timeout=float(self.cfg.get("upstream_timeout", 300)))

    def post_once(self, payload: dict) -> dict:
        """非流式打一次上游（摘要这类内部调用用）。"""
        body = dict(payload)
        body["stream"] = False
        req = urllib.request.Request(
            self.upstream_url(),
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.api_key,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=float(self.cfg.get("upstream_timeout", 300))) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def own_look(self, prompt: str = "") -> str:
        """我们自己看一眼：**走和她自主识屏完全同一条 prepare/stream_reply 管线**。

        老大 2026-11-17 的要求：「除了气泡，其他和他自己的自主识屏几乎一样」。
        所以这里不再自己拼 system/抓图/写账本，而是伪造一条和她一样的 vision 请求：
          * 同样的 `（参考元数据：前台应用为 …）` + 「这是老大当前的屏幕截图」；
          * `prepare()` 负责挂帧、注入近况/资料/facts/播放器、黑屏替换、看片规矩；
          * `stream_reply()` 负责流式合成 + 用同一个 `vision` 标签念；
          * `finish_turn()` 负责账本、统计、记忆信箱、事实学习。
        唯一的区别：这条路不冒气泡（气泡是桌宠自己的 UI）。
        """
        # 独占全屏/锁屏时截出来是黑的：这种时候别硬看，省得她开口就是「一片黑」
        if self.vision_is_black():
            log.info("自己看一眼：画面几乎是黑的（%.0f%%），这次不开口", self.vision_dark_ratio() * 100)
            return ""

        app = title = ""
        try:
            from video_watch import foreground

            app, title = foreground()
        except Exception:  # noqa: BLE001
            pass
        head = ("（参考元数据：老大当前前台应用为 %s | %s）\n" % (app or "?", title or "?")) \
            if (app or title) else ""
        body = prompt or "看看老大现在屏幕上是什么，用你的人设口吻说一两句。"
        if "截图" not in body:
            body = "这是老大当前的屏幕截图。" + body
        text = head + "你自己主动看了一眼屏幕（不是老大叫你）。" + body

        payload = {
            "model": self.cfg.get("model") or "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": self._persona()},
                # 用 list 形式，prepare() 才能把帧拼图挂到这条 user 消息上
                {"role": "user", "content": [{"type": "text", "text": text}]},
            ],
            "max_tokens": 400,
        }
        # 她自主识屏时带的是「桌宠截的当前图 + 我们的帧拼图」；这条也补一张当前帧，
        # 两边看到的素材就一致了（prepare 会把帧拼图插在图片前面）。
        try:
            snap = self.frames.snapshot(1)
            if snap:
                from PIL import Image

                buf = io.BytesIO()
                snap[-1][1].save(buf, "JPEG", quality=82)
                payload["messages"][1]["content"].extend([
                    {"type": "text", "text": "这张是此刻的屏幕。"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
                ])
        except Exception as exc:  # noqa: BLE001
            log.debug("自己看一眼：当前帧没带上：%s", exc)
        # 跟 _handle_chat 完全一样的前置：prepare → 黑屏守卫 → 打断上一轮 → 预置「让我看看」
        try:
            payload, scenario, user_text = self.prepare(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("自己看一眼：prepare 失败：%s", exc)
            return ""
        if self.vision_is_black():
            log.info("自己看一眼：这一帧太黑，跳过")
            return ""
        self.speech.interrupt()
        try:
            self.speech.arm_vision_cue()
        except Exception:  # noqa: BLE001
            pass

        sink: list[str] = []
        t0 = time.time()
        try:
            for _ in self.stream_reply(payload, scenario, sink):
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("自己看一眼：生成/合成失败：%s", exc)
            return ""
        reply = "".join(sink).strip()
        if reply:
            # 和她自主识屏同一本账、同一份统计、同一个记忆信箱
            self.finish_turn(scenario, user_text, reply, int((time.time() - t0) * 1000))
            # 气泡也补上：跟 voice_reply 同一套（3.2s 起步，最多 5s）
            try:
                self.bubble.show(reply, max(3200, min(5000, len(reply) * 160)))
            except Exception as exc:  # noqa: BLE001
                log.info("自己看一眼：气泡没弹出来（%s）", exc)
        return reply

    def _proactive_sync_loop(self) -> None:
        """跟着桌宠的「主动识屏」开关走：她关 → 我们关闭；她开 → 我们自动。

        我们自己写配置（托盘点「B站视频」之类）时会在 `_proactive_self` 里记一笔，
        这一笔不收，免得刚点的模式被她自己的开关立刻改回「自动」。
        """
        last: bool | None = None
        while True:
            time.sleep(1.5)
            try:
                cur = read_pet_proactive()
                if cur is None:
                    continue
                if last is None:
                    last = cur
                    # 第一次看到：如果她关着而我们在看，就跟着关掉
                    if not cur and self.mode.get() != "off":
                        self.mode.set("off", source="pet-proactive")
                        log.info("主动识屏是关的 → 模式切换为关闭")
                    # 反过来：上次是被她关掉留下的 off，这次她开着，就回自动
                    elif cur and self.mode.get() == "off" \
                            and str(self.mode.source).startswith("pet-proactive"):
                        self.mode.set("auto", source="pet-proactive")
                        log.info("主动识屏是开的 → 模式回自动")
                    continue
                if cur == last:
                    continue
                last = cur
                stamp = self._proactive_self
                if stamp and stamp[0] == cur and time.time() - stamp[1] < 6:
                    continue                     # 我们自己刚写的，不动模式
                if not cur:
                    self.mode.set("off", source="pet-proactive")
                    # 桌宠那边关掉时不冒泡，所以这句由我们冒 + 出声
                    self.bubble.show(fixed_phrases.PROACTIVE_OFF)
                    self.speech.say(fixed_phrases.PROACTIVE_OFF, tag="mode")
                    log.info("桌宠关掉主动识屏 → 关闭")
                else:
                    self.mode.set("auto", source="pet-proactive")
                    # 桌宠那边开的时候自己会冒气泡（"主动识屏已开启~…"），我们只出声
                    self.speech.say(fixed_phrases.PROACTIVE_ON, tag="mode")
                    log.info("桌宠打开主动识屏 → 自动模式")
            except Exception:  # noqa: BLE001
                pass

    def set_proactive(self, on: bool) -> bool:
        """我们这边切模式时顺手把她那边的「主动识屏」也拨到一致。"""
        ok = write_pet_proactive(on)
        if ok:
            self._proactive_self = (bool(on), time.time())
        return ok

    def _mode_lock_loop(self) -> None:
        """托盘里手动选的模式锁几分钟（默认 5 分钟），**锁到期不退出**。

        老大 2026-11-17 的原话：「锁这个模式多少分钟，不会退出，过了这几分钟按退出判断算」。
        所以这里只调用 `mode.release()`（把 source 标成 released），退出交给
        VideoWatcher / GameWatcher 各自的判断——没有自动判定的模式（番剧/直播/网课）
        就一直保持，直到手动换掉。
        """
        lock_minutes = max(1.0, float(self.cfg.get("mode_lock_minutes", 5)))
        while True:
            time.sleep(5)
            try:
                if self.mode.get() == "off":
                    continue
                if getattr(self.mode, "lock_forever", False):
                    continue
                src = str(self.mode.source)
                if src.startswith("tray") and not src.endswith("released"):
                    until = float(getattr(self, "mode_lock_until", 0.0) or 0.0)
                    if until <= 0:
                        # 兼容重启前就锁着、但没记 until 的旧状态
                        until = self.mode.since + lock_minutes * 60
                    if time.time() >= until:
                        self.mode.release("锁到期")
                        self.mode_lock_until = 0.0
            except Exception:  # noqa: BLE001
                pass

    def voice_reply(self, text: str) -> str:
        """语音问一句 → 模型 → 她开口 + 头顶气泡（和 TTS 一起出）。"""
        text = str(text or "").strip()
        if not text:
            return ""
        # 语音问「我在玩什么游戏」原来答不出来：这条路上**没有眼睛也没有前台信息**，
        # 模型手里只有一句问话，只能回「不知道」。现在把三样东西一起给她：
        # 当前前台（应用 + 窗口标题）、titles.json 里的小抄、刚才看到过的记录。
        app = title = ""
        try:
            from video_watch import foreground

            app, title = foreground()
        except Exception:  # noqa: BLE001
            pass
        screen_q = any(w in text for w in SCREEN_QUESTION_HINTS)
        want_look = (any(w in text for w in LOOK_HINTS) or screen_q
                     or self.mode.get() in FOCUS_MODES)
        context = []
        if title or app:
            context.append("【老大此刻的前台】%s（%s）" % (title or app, app or "?"))
        note = title_hint(app, title)
        if note:
            context.append("【这个作品你认识】%s" % note)
        # 问到的作品不熟时顺手排一课（后台查，不挡这句话）；有资料就递一两段最相关的。
        try:
            lore_block = self.lore.block(app=app, title=title, query=text, allow_queue=True)
            if lore_block:
                context.append(lore_block)
            facts_block = self._facts_block(app, title)
            if facts_block:
                context.append(facts_block)
        except Exception as exc:  # noqa: BLE001
            log.info("语音资料检索没成（不影响回答）：%s", exc)
        try:
            rows = self.ledger.recent_vision(1, 15)
            if rows:
                last = rows[-1]
                said = (last.get("assistant") or "").strip().replace("\n", " ")[:80]
                if said:
                    context.append("【你刚才看到过】%s，你说过：%s" % (Ledger.app_of(last) or "某处", said))
        except Exception:  # noqa: BLE001
            pass
        parts = [{"type": "text", "text": text}]
        if context:
            parts.insert(0, {"type": "text", "text": "\n".join(context)})
        # 语音截图（2026-11-17 老大定的）：**取「开口那一刻」的帧**，从帧缓冲里挑最近的，
        # 不额外触发截屏（每秒本来就在截），零额外延迟。默认 always = 每句话都带一张。
        shot_mode = str((self.cfg.get("voice") or {}).get("screenshot", "always")).lower()
        take_shot = shot_mode == "always" or (shot_mode == "auto" and want_look)
        if take_shot:
            try:
                from PIL import Image

                onset = float(getattr(getattr(self, "voice", None), "last_onset_at", 0) or 0)
                snap = None
                frames = getattr(self, "frames", None)
                if frames is not None:
                    with frames.lock:
                        items = list(frames.buf)
                    if items:
                        if onset > 0:
                            # 挑离「开口」最近的那一帧（黑帧本来就没进缓冲）
                            snap = min(items, key=lambda it: abs(it[0] - onset))
                        else:
                            snap = items[-1]
                age = 0.0
                if snap is None:
                    live = frames.snapshot(1) if frames is not None else []
                    if live:
                        snap = live[-1]
                if snap is not None:
                    ts, im = snap
                    age = max(0.0, time.time() - ts)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=82)
                    parts.append({"type": "text", "text":
                                  "这张是你开口说话那一刻的屏幕，大约 %.0f 秒前——**不是现在的**。"
                                  "这期间画面可能已经变了，别把它当此刻，也别拿它当「很久以前的图」。" % age})
                    parts.append({"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
                    log.info("语音：带上开口那一刻的帧（%.0f 秒前）", age)
            except Exception as exc:  # noqa: BLE001
                log.debug("语音带图失败（不带也照答）：%s", exc)
        system = self._persona() + (
            "\n\n【语音模式】老大是在用嘴跟你说话。回答短一点、口语一点，"
            "不要分点、不要标题、不要长段；一两句就够，像平时聊天那样。"
            "\n【问屏幕上的事】老大问你在玩什么、在看什么、这是哪部的时候，"
            "照【老大此刻的前台】和【你刚才看到过】回答：标题里写着的名字直接说；"
            "认得出来就报名字，再顺手说一句你知道的；真判断不出来就说「看不太出来，像是什么什么」，别编。"
        )
        content = parts if len(parts) > 1 else text
        payload = {"model": self.cfg.get("model") or "deepseek-v4-flash",
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": content}],
                   "max_tokens": 300, "stream": False}
        data = self.post_once(payload)
        try:
            reply = (data["choices"][0]["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            reply = ""
        if not reply:
            return ""
        self.speech.say(reply, tag="voice")
        # 气泡停留：跟字数和语音差不多长，但别赖着——3.2 秒起步，最多 5 秒
        self.bubble.show(reply, max(3200, min(5000, len(reply) * 160)))
        self.ledger.add("chat", text, reply, {"voice": True})
        self._learn_fact("voice", text, app, title)
        return reply

    def _persona(self) -> str:
        """优先用桌宠配置里的人设（泡泡）。"""
        try:
            p = os.path.join(os.environ.get("APPDATA", ""), "dsh-pet-standalone-webm-chat", "config.json")
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            sp = ((cfg.get("chat") or {}).get("default_system_prompt") or "").strip()
            if sp:
                return sp
        except Exception:  # noqa: BLE001
            pass
        return "你是泡泡，一只住在他电脑里的蓝色鲸鱼娘，只叫他老大。"

    def _announce_mode(self, mode: str, source: str) -> None:
        """模式一变就出声 + 冒泡（老大 2026-11-17 要的：每次切换都要有提示）。

        这些都是 phrases.py 里的固定句子，启动时就预合成好了，播的时候零等待。
        """
        if not self.cfg.get("mode_feedback", True):
            return
        # 跟桌宠「主动识屏」开关联动的切换，由 _proactive_sync_loop 自己发声
        # （开 → 那句「主动识屏已开启…」；关 → 气泡+「行，不看了」）。
        # 这里再念一遍模式提示，就会出现两个声音（老大 2026-11-17 反馈）。
        if source == "pet-proactive":
            return
        # 通用视频跟 B站视频都是 mode=video，靠 source 区分用哪套提示
        key = mode
        if mode == "video" and source in ("tray-generic", "auto-video"):
            key = "video_any"
        # 每个模式备两句，随机挑一句（都预合成好了，零等待）
        choices = fixed_phrases.MODE_PHRASES.get(key) or []
        if not choices:
            return
        text = random.choice(list(choices))
        if not text:
            return
        try:
            self.bubble.show(text, 3600)
        except Exception as exc:  # noqa: BLE001
            log.info("模式提示气泡没冒出来：%s", exc)
        try:
            self.speech.say(text, tag="mode")
        except Exception as exc:  # noqa: BLE001
            log.info("模式提示没说成：%s", exc)
        log.info("模式提示：%s（%s / %s）", text, mode, source)

    # ---- 一次对话
    def stream_reply(self, payload: dict, scenario: str, sink: list[str]):
        """生成器：逐块产出正文增量；同时按标点把句子塞进朗读流水线。"""
        # 模式 off 现在只表示「别自动进看视频/看番」，**不再把识屏说哑**：
        # 之前一按「关闭」，她照常识屏却一句不出声，看着就像语音坏了。
        # 真要静音（夜间/开会）就打开 mute_vision_when_off。
        speak_this = True
        if (scenario == "vision" and self.cfg.get("mute_vision_when_off", False)
                and getattr(self, "mode", None) and self.mode.get() == "off"):
            speak_this = False
        max_chars = int(self.cfg["tts"].get("max_chars_per_sentence", 60))
        fed_count = 0
        buf = ""
        t0 = time.time()
        logged_first = False
        logged_feed = False
        resp = self.open_stream(payload)
        self.turn_active = True
        try:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                try:
                    delta = chunk["choices"][0].get("delta") or {}
                except (KeyError, IndexError, TypeError):
                    continue
                piece = delta.get("content") or ""
                if not piece:
                    continue
                if not logged_first:
                    logged_first = True
                    first_latency = time.time() - t0
                    self.speech.text_ready_at = time.time()
                    log.info("首字 %.2fs（%s）", first_latency, scenario)
                    if scenario == "vision":
                        sp = self.speech
                        sp.vision_text_ewma = (
                            first_latency if sp.vision_text_ewma <= 0
                            else 0.7 * sp.vision_text_ewma + 0.3 * first_latency
                        )
                sink.append(piece)
                buf += piece
                # 攒到句末标点就送去合成：边生成边合成边播，不等整段。
                # take_ready_sentence 会把「唉……」这种碎片段并到下一句，
                # 免得先单独念一个「唉」、卡一下、再接着念。
                # 只有第一小句走流式（要它最快开口），后面的照常整句合成——
                # 流式那套分块出来的音质差一截，老大嫌不好听。
                while True:
                    # 前三段都按标点切（。！？；，、：… 都算），够长就切：
                    # 前几段要的是「快点开口」，切碎了先播出去，后面再慢慢合成。
                    # 但第一段要更长一点（first_min_chars，默认 24 字 ≈ 5.5 秒音频）：
                    # 第一段太短（8 字只有 1.6 秒）的话，等它念完第二句还没合成好，
                    # 中间就空一下——「后面几句卡」最早那一空就是这么来的。
                    early = int(self.cfg["tts"].get("early_sentences", 3) or 3)
                    early_min = int(self.cfg["tts"].get("early_min_chars", 8) or 8)
                    first_min = int(self.cfg["tts"].get("first_min_chars", 24) or 24)
                    if fed_count < early:
                        need = max(early_min, first_min) if fed_count == 0 else early_min
                        sentence, rest = take_first_sentence(buf, need)
                    else:
                        sentence, rest = take_ready_sentence(buf, 6)
                    if not sentence:
                        break
                    buf = rest
                    cleaned = clean_for_speech(sentence)
                    if cleaned and speak_this:
                        if not logged_feed:
                            logged_feed = True
                            log.info("首句入队 %.2fs（%d 字：%s）", time.time() - t0,
                                     len(cleaned), cleaned[:40])
                        self.speech.feed_sentences(split_sentences(cleaned, max_chars), scenario)
                        fed_count += 1
                yield piece
            tail = clean_for_speech(buf)
            if tail and speak_this:
                self.speech.feed_sentences(split_sentences(tail, max_chars), scenario)
        finally:
            self.turn_active = False
            resp.close()

    def _all_fixed_phrases(self) -> list[str]:
        """DSH 状态 + 语音模式三句 + 托盘反馈：全部固定句子。"""
        phrases: list[str] = []
        common_tools = ("pwsh", "read", "grep", "glob", "write", "edit", "web_search",
                        "web_fetch", "subagent", "todo_write", "list_agents", "job_output")
        try:
            for items in DshWatcher.TEMPLATES.values():
                for item in items:
                    if "{tool}" in item:
                        for tool in common_tools:
                            phrases.append(item.format(tool=tool))
                    elif "{name}" in item:
                        phrases.append(item.format(name="老大"))
                    else:
                        phrases.append(item)
        except Exception:  # noqa: BLE001
            pass
        phrases.extend(fixed_phrases.ALL)
        return phrases

    def warm_fixed_phrases(self, extra: list[str] | None = None,
                           force: bool = False, include_all: bool = True) -> int:
        total = list(extra or [])
        if include_all:
            total.extend(self._all_fixed_phrases())
        # 去重但保序
        seen: set[str] = set()
        uniq = [p for p in total if p and not (p in seen or seen.add(p))]
        self.speech.warm_phrases(uniq, force=force)
        return len(uniq)

    def _warm_dsh_phrases(self) -> None:
        """DSH 状态 + 固定气泡文案，全部预合成，之后开口零等待。"""
        try:
            self.warm_fixed_phrases()
        except Exception as exc:  # noqa: BLE001
            log.warning("短语预热失败：%s", exc)

    def _learn_fact(self, scenario: str, user_text: str, app: str = "", title: str = "") -> None:
        """从这一轮里挑「老大在纠正/教她」的话，记进共用事实（聊天/语音/识屏都读）。"""
        if getattr(self, "facts", None) is None:
            return
        try:
            text = str(user_text or "")
            if not (app or title):
                m = SCREEN_META_RE.search(text)
                if m:
                    app = (m.group(1) or "").strip()
                    title = (m.group(2) or "").strip()
            if scenario == "vision":
                text = clean_query(text)
            elif not (app or title):
                try:
                    from video_watch import foreground

                    app, title = foreground()
                except Exception:  # noqa: BLE001
                    pass
            self.facts.add(text, app, title, source=scenario)
        except Exception as exc:  # noqa: BLE001
            log.debug("事实没记成（不影响这轮）：%s", exc)

    def _facts_block(self, app: str = "", title: str = "") -> str:
        if getattr(self, "facts", None) is None:
            return ""
        try:
            return self.facts.block(app, title)
        except Exception as exc:  # noqa: BLE001
            log.debug("事实没取出来：%s", exc)
            return ""

    def finish_turn(self, scenario: str, user_text: str, reply: str, elapsed_ms: int) -> None:
        self.ledger.add(scenario, user_text, reply, {"ms": elapsed_ms})
        self._learn_fact(scenario, user_text)
        self.stats["requests"] += 1
        if scenario == "vision":
            self.stats["vision"] += 1
            self.last_vision_at = time.time()
        self.stats["spoken"] = self.speech.spoken_sentences
        if reply:
            log.info(
                "完成 scenario=%s %dms 回复%d字 排队%d句 已播%d句",
                scenario, elapsed_ms, len(reply), self.speech.pending_count(),
                self.speech.spoken_sentences,
            )
        if self.cfg.get("memory_inbox_url") and scenario in self.cfg.get("memory_inbox_scenarios", []):
            threading.Thread(target=self.push_memory, args=(scenario, user_text, reply), daemon=True).start()

    def push_memory(self, scenario: str, user_text: str, reply: str) -> None:
        body = json.dumps(
            {"scenario": scenario, "user": user_text, "assistant": reply, "ts": int(time.time())},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self.cfg["memory_inbox_url"], data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            log.info("记忆信箱已投递（%s）", scenario)
        except Exception as exc:
            log.warning("记忆信箱投递失败: %s", exc)

    # ---- 请求预处理
    def vision_dark_ratio(self) -> float:
        """最近几帧里最黑的那张有多黑（0~1）。取不到就 -1。

        为什么不用请求里那张图：桌宠传的图和我们的帧缓冲截的是同一块屏幕，
        而解码请求里的 base64 每轮要多花几十毫秒；帧缓冲这边顺手就算好了。
        """
        try:
            if not getattr(self, "frames", None):
                return -1.0
            with self.frames.lock:
                rows = list(self.frames.darks)[-3:]
            if not rows:
                return -1.0
            return max(r for _ts, r in rows)
        except Exception:  # noqa: BLE001
            return -1.0

    def vision_is_black(self) -> bool:
        """这一轮要不要因为「画面几乎全黑」直接跳过。"""
        if not self.cfg.get("skip_black_frames", True):
            return False
        ratio = float(self.cfg.get("vision_black_ratio", self.frames.black_ratio
                                   if getattr(self, "frames", None) else 0.92))
        n = max(1, int(self.cfg.get("vision_black_frames", 2)))
        if not getattr(self, "frames", None):
            return False
        return self.frames.is_black(n=n, ratio=ratio)

    def fix_black_request_image(self, messages: list) -> str:
        """桌宠传来的那张图如果是黑的，而我们自己截得没问题，就用我们的换掉。

        黑屏多半不是「屏幕上真是黑的」，是**截屏方式抓不到**：独占全屏的 DirectX 游戏、
        DRM 保护的视频、锁屏/息屏，GDI 截出来就是一片黑。桌宠用哪套我们改不了，
        但我们自己的帧缓冲是同一块屏幕，能截到就直接顶上去。
        返回 "ok"（没黑）/ "replaced"（换掉了）/ "black"（两边都黑，交给黑屏守卫跳过）。
        """
        if not self.cfg.get("fix_black_request_image", True):
            return "ok"
        try:
            import base64 as _b64
            import io as _io

            from PIL import Image

            last_user = None
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user = msg
                    break
            if last_user is None:
                return "ok"
            content = last_user.get("content")
            if not isinstance(content, list):
                return "ok"
            target = None
            # 注意取**最后一张**：我们自己的帧拼图是插在桌宠那张之前的，
            # 最后那张才是「此刻的屏幕」（第一张是我拼的过去几秒）。
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
                    url = (part.get("image_url") or {}).get("url") or part.get("url") or ""
                    if "base64," in url:
                        target = (part, url)
                        break
            if target is None:
                return "ok"
            part, url = target
            raw = _b64.b64decode(url.split("base64,", 1)[1][:6 * 1024 * 1024])
            img = Image.open(_io.BytesIO(raw)).convert("L").resize((64, 36))
            px = list(img.getdata())
            dark = sum(1 for v in px if v < 24) / max(1, len(px))
            if dark >= 0.6:
                log.info("识屏：请求里那张图有 %.0f%% 接近纯黑", dark * 100)
            if dark < 0.92:
                return "ok"
            # 请求里那张是黑的：看我们自己帧缓冲里有没有好帧
            frames = getattr(self, "frames", None)
            shot = None
            if frames is not None:
                snap = frames.snapshot(1)
                if snap:
                    ts, im = snap[-1]
                    if time.time() - ts <= 10:
                        shot = im
            if shot is None:
                return "black"
            # 我们自己截的也得不是黑的，否则换上去还是黑
            try:
                g64 = shot.convert("L").resize((64, 36))
                gpx = list(g64.getdata())
                if sum(1 for v in gpx if v < 24) / max(1, len(gpx)) >= 0.92:
                    return "black"
            except Exception:  # noqa: BLE001
                pass
            buf = _io.BytesIO()
            shot.save(buf, "JPEG", quality=82)
            b64 = _b64.b64encode(buf.getvalue()).decode()
            new_url = "data:image/jpeg;base64," + b64
            if part.get("type") == "image_url":
                part["image_url"] = dict(part.get("image_url") or {}, url=new_url)
            else:
                part["url"] = new_url
            return "replaced"
        except Exception as exc:  # noqa: BLE001
            log.info("换掉黑图失败（不换也照答）：%s", exc)
            return "ok"

    def prepare(self, payload: dict) -> tuple[dict, str, str]:
        messages = payload.get("messages") or []
        scenario = detect_scenario(messages, self.cfg.get("vision_markers", []))
        if self.cfg.get("wipe_owner_word", True):
            replace_owner_word(messages)
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_text = message_text(msg.get("content"))[:1200]
                break
        if scenario == "vision" and self.cfg.get("soften_self_recognition", True):
            for msg in messages:
                content = msg.get("content")
                if isinstance(content, str):
                    if "身份提示" in content:
                        msg["content"] = soften_identity_hint(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            item["text"] = soften_identity_hint(item["text"])
            # 刚才已经用过「那个是我」这招的话，这一轮按住
            conf = self.cfg.get("self_recognition") or {}
            rows = self.ledger.recent_vision(8, float(conf.get("lookback_minutes", 90)))
            if self_ref_recent(rows, float(conf.get("cooldown_minutes", 90))):
                hint = (
                    "【这一轮】你刚刚已经说过一次「那个是不是我」这种话了。这一轮别再提自己、"
                    "别说「角落里那个是我」，把注意力放在老大在干什么上。"
                )
                messages.insert(0, {"role": "system", "content": hint})
                log.info("识屏：压一下自我认领（刚说过）")

        if scenario == "vision" and getattr(self, "frames", None) is not None:
            attached = 0
            if self.frames.attach:
                # 游戏模式多给几帧（6 张、3 列）：精彩操作要连着看几秒才看得出来
                want, cols = (6, 3) if self.mode.get() == GAME_MODE else (0, 2)
                for msg in reversed(messages):
                    if msg.get("role") == "user":
                        attached = self.frames.attach_to(msg, count=want, cols=cols)
                        break
            if attached:
                try:
                    ages = [round(time.time() - ts, 1) for ts, _img in self.frames.snapshot(attached)]
                except Exception:
                    ages = []
                log.info("识屏附上 %d 帧拼图（分别是 %s 秒前）", attached, ages)

        # B站视频模式（方案 B）：有调试端口就读「现在放到哪」，按时间轴切出前 N 秒字幕。
        # 这条比转写/OCR 都准，所以有它就不再叠转写，省 token。
        player_block = ""
        if scenario == "vision" and getattr(self, "player", None) is not None:
            try:
                player_block = self.player.context_block()
            except Exception as exc:  # noqa: BLE001
                log.info("播放器状态读不出来（不影响识屏）：%s", exc)
            if player_block:
                messages.insert(0, {"role": "system", "content": player_block})
                log.info("播放器：带上进度和字幕（%s）", self.player.status().get("position") or "?")

        if scenario == "vision" and getattr(self, "video", None) is not None:
            # 通用视频模式：把刚才的对白/字幕条喂进去，她才知道片子在讲什么。
            # 已经有了「按时间轴的字幕」就不再叠一份转写（重复且费 token）。
            if not player_block or "B站字幕" not in player_block:
                block = self.video.context_block()
                if block:
                    messages.insert(0, {"role": "system", "content": block})
                    log.info("视频模式：带上 %d 条对白/字幕", self.video.transcript_count())
            hint = self.video.take_first_hint()
            if hint:
                messages.insert(0, {"role": "system", "content": hint})
                log.info("视频模式：第一次开口的引导已注入")

        if scenario == "vision":
            # 「先看清再说话」+ 作品小抄：认得出是哪款游戏/哪部番，也接得住话。
            m = SCREEN_META_RE.search(str(user_text or ""))
            app = (m.group(1) or "").strip() if m else ""
            title = (m.group(2) or "").strip() if m else ""
            note = title_hint(app, title)
            if note:
                messages.insert(0, {"role": "system", "content": (
                    "【这个作品你认识】%s\n（这是你自己的印象，顺着聊就行，别背简介、别逐字念。）" % note)})
                log.info("识屏：带上作品小抄（%s）", (title or app)[:30])
            elif title or app:
                # 记一笔，方便以后往 titles.json 里补
                log.info("识屏：小抄里没有这个作品（%s）", (title or app)[:40])
            # 「肚子里的资料」：只有画面/问话里出现相关线索才递一两段，用不上就一个字不提。
            try:
                lore_block = self.lore.block(app=app, title=title, query=user_text)
            except Exception as exc:  # noqa: BLE001
                lore_block = ""
                log.info("资料库检索没成（不影响识屏）：%s", exc)
            if lore_block:
                messages.insert(0, {"role": "system", "content": lore_block})
                log.info("识屏：带上资料（%s）", (title or app or "?")[:30])
            elif self.mode.get() == GAME_MODE:
                # 打游戏时没有问话也要能接上话：只给「一句话」，不给大段资料
                headline = self.lore.headline(app=app, title=title)
                if headline:
                    messages.insert(0, {"role": "system", "content": headline})
                    log.info("识屏：带上资料一句话（%s）", (title or app or "?")[:30])
            # 老大在聊天/语音里教过的事（「那是遐蝶，不是希尔」）→ 识屏也要知道
            facts_block = self._facts_block(app, title)
            if facts_block:
                messages.insert(0, {"role": "system", "content": facts_block})
                log.info("识屏：带上老大教过的事")
            if self.cfg.get("see_howto", True):
                messages.insert(0, {"role": "system", "content": build_see_howto()})

        if scenario == "vision" and self.cfg.get("inject_vision_context", True):
            # 识屏也要有前后文：把刚才几次「她看到过什么」带上，她才接得上
            app = ""
            m = APP_RE.search(str(user_text or ""))
            if m:
                app = m.group(1).strip()
            watching = self.mode.get() in FOCUS_MODES
            prev = self.ledger.recent_vision_for_app(
                app,
                max_minutes=float(self.cfg.get("vision_context_minutes", 30)),
                budget_chars=int(self.cfg.get("vision_context_budget_chars", 600)),
                item_chars=int(self.cfg.get("vision_context_item_chars", 90)),
                max_items=int(self.cfg.get("vision_context_entries", 6)),
            )
            block = build_vision_context(prev, hide_clock=watching)
            if block:
                messages.insert(0, {"role": "system", "content": block})
                log.info("识屏带上近况 %d 条%s", len(prev), "（看片中，不给钟点）" if watching else "")

        if scenario == "chat" and self.cfg.get("inject_vision_ledger", True):
            rows = self.ledger.recent_vision(
                int(self.cfg.get("inject_recent_entries", 3)),
                float(self.cfg.get("inject_recent_minutes", 45)),
            )
            digest = ""
            conf = self.cfg.get("ledger_digest") or {}
            if conf.get("enabled", True):
                digest = str(self.ledger.digest().get("text") or "")
            block = build_ledger_block(rows, digest)
            if block:
                messages.insert(0, {"role": "system", "content": block})
                log.info("注入识屏：近况 %d 条 + 摘要 %d 字", len(rows), len(digest))
            # 聊天里提到她学过的游戏/番剧，就把相关的一两段资料递过去（没提到就什么都不加）
            if getattr(self, "lore", None) is not None:
                try:
                    lore_block = self.lore.block(query=user_text, allow_queue=True)
                except Exception as exc:  # noqa: BLE001
                    lore_block = ""
                    log.info("聊天资料检索没成（不影响聊天）：%s", exc)
                if lore_block:
                    messages.insert(0, {"role": "system", "content": lore_block})
                    log.info("聊天：带上资料")
            # 聊天/语音里教过的事，别的通道也读同一份（反过来识屏也读）
            try:
                from video_watch import foreground

                fapp, ftitle = foreground()
            except Exception:  # noqa: BLE001
                fapp, ftitle = "", ""
            facts_block = self._facts_block(fapp, ftitle)
            if facts_block:
                messages.insert(0, {"role": "system", "content": facts_block})
                log.info("聊天：带上老大教过的事")
        # 自动/普通模式下的识屏：说清「这是你自己瞥了一眼」，别当成老大在跟她说话
        if scenario == "vision" and self.mode.get() in ("auto", "normal"):
            messages.insert(0, {"role": "system", "content":
                "【此刻】老大没叫你，是你自己瞥了一眼屏幕（自动模式）。别问「你叫我？」"
                "「有事吗」这种话，也别把自己当被点名；看到什么就直接说，一两句。"})

        # 看片/看番/看直播/网课/打游戏：这一轮只聊内容，不提时间，不念字幕（游戏不念界面文字）。
        # 放在最后 insert(0)，所以它是整段对话最前面那条 system（最有分量）。
        if scenario == "vision" and self.cfg.get("watch_focus", True):
            watching = self.mode.get()
            if watching in FOCUS_MODES:
                messages.insert(0, {"role": "system", "content": build_watch_focus(watching)})
                log.info("专注模式：注入「专心」规矩（%s）", watching)

        # 桌宠那张图是黑的、我们自己截得到 → 换成我们的；两边都黑就交给黑屏守卫整轮跳过
        if scenario == "vision":
            fixed = self.fix_black_request_image(messages)
            if fixed == "replaced":
                log.info("识屏：桌宠那张图是黑的，用我们自己截的换掉了")
            elif fixed == "black":
                log.info("识屏：两边都截成黑的（独占全屏/DRM/锁屏？）")

        payload["messages"] = messages
        payload["stream"] = True
        return payload, scenario, user_text


GATEWAY: Gateway | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8", "replace") or "{}")

    def do_GET(self):
        if self.path.startswith("/health"):
            g = GATEWAY
            self._send_json(
                {
                    "ok": bool(g and g.api_key),
                    "port": g.cfg["listen_port"] if g else None,
                    "speaker_pending": g.speech.pending_count() if g else 0,
                    "spoken": g.speech.spoken_sentences if g else 0,
                    "dsh_watch": g.dsh.dir if g else "",
                    "stats": g.stats if g else {},
                }
            )
            return
        if self.path.startswith("/status"):
            self._handle_status()
            return
        if self.path.startswith("/player"):
            # 浏览器扩展（方案 A）每秒钟推一小段进度；GET 是给它自检用的
            self._send_json({"ok": True, "player": getattr(GATEWAY, "player", None).status()
                             if getattr(GATEWAY, "player", None) else None})
            return
        if self.path.startswith("/titles"):
            # 现在小抄里有什么 / 存哪儿（改了文件按 mtime 自动重载）
            notes = title_notes(refresh=True)
            self._send_json({"ok": True, "count": len(notes), "items": notes,
                             "path": TITLES_PATH})
            return
        if self.path.startswith("/lore"):
            self._handle_lore()
            return
        if urllib.parse.urlparse(self.path).path == "/ext/paopao-player.user.js":
            # 让 Tampermonkey 能直接从这个地址装脚本（不用去文件夹里拖）
            path = os.path.join(APP_DIR, "browser_ext", "paopao-player.user.js")
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                self._send_json({"ok": False, "error": "用户脚本文件不在"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/doctor"):
            self._handle_doctor()
            return
        if self.path.startswith("/mode"):
            self._send_json(GATEWAY.mode.set.__self__ and {
                "mode": GATEWAY.mode.get(), "since": GATEWAY.mode.since, "source": GATEWAY.mode.source})
            return
        if self.path.startswith("/voice"):
            self._send_json({"ok": True, "voice": GATEWAY.voice.status(),
                             "bubble": GATEWAY.bubble.status()})
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/chat/completions"):
            self._handle_chat()
        elif path.endswith("/frames"):
            self._handle_frames()
        elif path.endswith("/digest"):
            self._handle_digest()
        elif path.endswith("/lookself"):
            self._handle_lookself()
        elif path.endswith("/look"):
            self._handle_look()
        elif path.endswith("/player"):
            self._handle_player()
        elif path.endswith("/divination"):
            self._handle_divination()
        elif path.endswith("/mode-lock"):
            self._handle_mode_lock()
        elif path.endswith("/mode"):
            self._handle_mode()
        elif path.endswith("/say"):
            self._handle_say()
        elif path.endswith("/bubble"):
            self._handle_bubble()
        elif path.endswith("/warm"):
            self._handle_warm()
        elif path.endswith("/titles"):
            self._handle_titles()
        elif path.endswith("/lore"):
            self._handle_lore()
        elif path.endswith("/voice/input"):
            self._handle_voice_input()
        elif path.endswith("/voice"):
            self._handle_voice()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_frames(self):
        try:
            b64, n = GATEWAY.frames.grid_jpeg()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if not b64:
            self._send_json({"ok": False, "frames": 0, "error": GATEWAY.frames.last_error or "缓冲里没有帧"})
            return
        raw = base64.b64decode(b64)
        path = os.path.join(APP_DIR, "logs", "last_grid.jpg")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        self._send_json({"ok": True, "frames": n, "bytes": len(raw), "saved": path})

    def _handle_digest(self):
        try:
            result = GATEWAY.digester.run_once(force=True)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        self._send_json(result)

    def _handle_lookself(self):
        try:
            reply = GATEWAY.own_look()
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        self._send_json({"ok": bool(reply), "reply": reply[:300]})

    def _handle_player(self):
        """浏览器扩展推上来的播放进度（方案 A）：{url,title,t,d,paused,fullscreen,visible}。"""
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        player = getattr(GATEWAY, "player", None)
        if player is None:
            self._send_json({"ok": False, "error": "播放器跟踪没启动"}, 503)
            return
        player.push_ext(req)
        self._send_json({"ok": True})

    def _handle_look(self):
        """主动让她看一眼：按一次「看看屏幕」，等她真的看（识屏计数涨）。"""
        before = GATEWAY.stats.get("vision", 0)
        sent = press_look(int((GATEWAY.cfg.get("look_guard") or {}).get("helper_port", 9893)))
        if not sent:
            self._send_json({"ok": False, "reason": "按键助手没响应（9893）"}, 503)
            return
        for _ in range(16):
            time.sleep(0.5)
            if GATEWAY.stats.get("vision", 0) > before:
                self._send_json({"ok": True, "vision_before": before,
                                 "vision_after": GATEWAY.stats.get("vision", 0), "via": "hotkey"})
                return
        # 热键没反应 → 先自己看（她可能被隐藏了，热键和菜单都未必管用）
        log.info("热键没反应，改用自己看一眼")
        try:
            reply = GATEWAY.own_look()
        except Exception as exc:  # noqa: BLE001
            reply = ""
            log.warning("自己看也失败：%s", exc)
        if reply:
            self._send_json({"ok": True, "via": "own", "reply": reply[:200]})
            return

        # 还不行 → 退回右键菜单那条（会短暂弹一下菜单）
        log.info("热键没反应，改走右键菜单")
        port = int((GATEWAY.cfg.get("look_guard") or {}).get("helper_port", 9893))
        if not press_look(port, "menulook"):
            self._send_json({"ok": False, "reason": "热键和菜单都没能触发"})
            return
        for _ in range(24):
            time.sleep(0.5)
            if GATEWAY.stats.get("vision", 0) > before:
                self._send_json({"ok": True, "vision_before": before,
                                 "vision_after": GATEWAY.stats.get("vision", 0), "via": "menu"})
                return
        self._send_json({"ok": False, "reason": "热键和菜单都试了，她都没看"})

    def _handle_divination(self):
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        text = str(req.get("text") or "").strip()
        if not text:
            self._send_json({"ok": False, "error": "text 为空"}, 400)
            return
        n = GATEWAY.speech.say(text, tag="divination")
        self._send_json({"ok": bool(n), "sentences": n})

    def _handle_mode(self):
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        try:
            source = str(req.get("source") or "api")
            data = GATEWAY.mode.set(str(req.get("mode") or ""), source=source)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        log.info("模式切换 → %s（%s）", data["mode"], data["source"])
        if not source.startswith("tray"):
            # API/插件切模式：不吃托盘残留的锁
            GATEWAY.mode_lock_until = 0.0
        if source.startswith("tray"):
            # 托盘所有模式（包括「通用模式」，内部值仍是 auto）都按手动锁走：
            # 默认锁 N 分钟；「一直」就锁到手动换掉为止。
            if getattr(GATEWAY.mode, "lock_forever", False):
                GATEWAY.mode_lock_until = float("inf")
                log.info("模式锁：%s 一直保持（手动锁=一直）", data["mode"])
            else:
                lock_min = max(1.0, float(GATEWAY.cfg.get("mode_lock_minutes", 5)))
                GATEWAY.mode_lock_until = time.time() + lock_min * 60
                log.info("模式锁：%s 锁 %.0f 分钟（到点只放开，不退出）", data["mode"], lock_min)
            # 托盘选的最后一步：把她那边的「主动识屏」也拨成一致
            # （关闭 = 识屏关；其它模式 = 识屏开）
            GATEWAY.set_proactive(data["mode"] != "off")
            if data["mode"] == "off" and not GATEWAY.cfg.get("mode_feedback", True):
                # 模式提示关掉时兜底：这句话不能没有（正常情况由 _announce_mode 说）
                GATEWAY.speech.say(fixed_phrases.PROACTIVE_OFF, tag="mode")
        if source.startswith("tray") and data["mode"] in ("video", "anime", "live", "course"):
            # 托盘里手动选了视频类模式：不等 2 分钟条件，直接进会话
            try:
                GATEWAY.video.force()
            except Exception as exc:  # noqa: BLE001
                log.warning("手动进视频类模式失败：%s", exc)
        self._send_json({"ok": True, **data})

    def _handle_mode_lock(self):
        """托盘「手动锁」：5 分钟（默认）/ 一直保持。"""
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        forever = bool(req.get("forever"))
        g = GATEWAY
        g.mode.set_lock_forever(forever)
        cur = g.mode.get()
        src = str(g.mode.source)
        if src.startswith("tray"):
            new_src = "tray-generic" if src.startswith("tray-generic") else "tray"
            if src != new_src:
                # 从 released 拉回锁住状态（模式不变，不播提示）
                g.mode.set(cur, source=new_src)
            if forever:
                g.mode_lock_until = float("inf")
            else:
                lock_min = max(1.0, float(g.cfg.get("mode_lock_minutes", 5)))
                g.mode_lock_until = time.time() + lock_min * 60
        else:
            g.mode_lock_until = 0.0
        log.info("手动锁 → %s（当前 %s / %s）",
                 "一直保持" if forever else "5 分钟", cur, g.mode.source)
        self._send_json({"ok": True, "mode": cur, "source": g.mode.source,
                         "forever": forever,
                         "lock_forever": bool(g.mode.lock_forever),
                         "mode_lock_until": 0 if not (g.mode_lock_until < float("inf"))
                         else g.mode_lock_until})

    def _handle_status(self):
        g = GATEWAY
        day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        rows = [r for r in g.ledger.rows() if r.get("epoch", 0) >= day_start]
        frames = len(g.frames.buf)
        # 前台是谁、小抄里有没有：想确认「她认得出现在这个吗」看这一项
        front = {"app": "", "title": "", "note": "", "hit": False}
        try:
            from video_watch import foreground

            app, title = foreground()
            front.update({"app": app, "title": title,
                          "note": title_hint(app, title), "hit": bool(title_hint(app, title))})
        except Exception:  # noqa: BLE001
            pass
        lock_until = float(getattr(g, "mode_lock_until", 0.0) or 0.0)
        lock_remaining = (0 if lock_until <= 0 else
                          (None if lock_until == float("inf")
                           else max(0, int(lock_until - time.time()))))
        self._send_json({
            "ok": True,
            "mode": g.mode.get(),
            "mode_since": g.mode.since,
            "mode_source": g.mode.source,
            "mode_lock_forever": bool(getattr(g.mode, "lock_forever", False)),
            "mode_lock_minutes": float(g.cfg.get("mode_lock_minutes", 5)),
            "mode_lock_remaining": lock_remaining,
            "front": front,
            "titles": len(title_notes()),
            "frames": frames,
            "precue": len(getattr(g.speech, "cue_paths", []) or []),
            "digest_chars": len(str(g.ledger.digest().get("text") or "")),
            "today_vision": len(rows),
            "spoken": g.speech.spoken_sentences,
            "first_byte_ms": round(float(getattr(g.speech, "first_byte_ewma", 0.0)) * 1000),
            "synth_ms": round(float(getattr(g.speech, "synth_ewma", 0.0)) * 1000),
            "api_key": bool(g.api_key),
            "video": getattr(g, "video", None).status() if getattr(g, "video", None) else None,
            "player": getattr(g, "player", None).status() if getattr(g, "player", None) else None,
            "bubble": getattr(g, "bubble", None).status() if getattr(g, "bubble", None) else None,
            "voice": getattr(g, "voice", None).status() if getattr(g, "voice", None) else None,
            "lore": (lambda lo: {
                "count": len(lo.store.entries),
                "names": [m.get("name") for m in lo.store.entries.values()],
                "keeper": lo.keeper.status() if lo.keeper is not None else None,
            })(g.lore) if getattr(g, "lore", None) is not None else None,
            "tts_guard": getattr(g, "tts_guard", None).status() if getattr(g, "tts_guard", None) else None,
            "facts": len(getattr(g, "facts", None).items) if getattr(g, "facts", None) else 0,
        })

    def _handle_doctor(self):
        import shutil

        def port_ok(port: int) -> bool:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                    return True
            except OSError:
                return False

        g = GATEWAY
        checks = {
            "api_key": {"ok": bool(g.api_key), "detail": "DeepSeek Key 已读取" if g.api_key else "缺 Key"},
            "tts_backend": {
                "ok": True,
                "detail": "%s · %s" % (
                    g.speech.tts_backend.describe(),
                    "流式" if g.speech.tts_backend.streams else "整句合成"),
            },
            "tts_9882": {"ok": port_ok(9882), "detail": "GPT-SoVITS 端口（说出口的流式）"},
            "tts_guard": (lambda st: {
                "ok": bool(st.get("enabled")) and bool(st.get("up")),
                "detail": "TTS 守护：%s（拉起 %d 次%s）" % (
                    "在线" if st.get("up") else "没在线",
                    int(st.get("launches") or 0),
                    "，" + st["error"] if st.get("error") else ""),
            })(getattr(g, "tts_guard", None).status() if getattr(g, "tts_guard", None) else {}),
            "tts_9883": {
                "ok": True if not getattr(g.speech, "split_tts", False) else port_ok(9883),
                "detail": ("整句合成那台（分家了）" if getattr(g.speech, "split_tts", False)
                           else "没分家，全走 9882"),
            },
            "asr_9881": {"ok": port_ok(9881), "detail": "本地转写服务"},
            "ocr_venv": {"ok": os.path.exists(r"D:\app1\tools\ocrvenv\Scripts\python.exe"),
                         "detail": "rapidocr 独立环境"},
            "ytdlp": {"ok": os.path.exists(r"D:\app1\tools\ytdlp\yt_dlp"),
                      "detail": "字幕抓取"},
            "ffmpeg": {"ok": bool(shutil.which("ffmpeg")), "detail": "音视频工具"},
            "frames": {"ok": len(g.frames.buf) > 0, "detail": "%d 帧在缓冲" % len(g.frames.buf)},
            "video_watch": (lambda st: {
                "ok": bool(st.get("enabled")),
                "detail": "通用视频：%s%s" % (
                    st.get("state"), "（%d 条对白）" % st.get("lines", 0)
                    if st.get("lines") else "（环回%s）" % ("有声" if st.get("audio") else "没声")),
            })(getattr(g, "video", None).status() if getattr(g, "video", None) else {}),
            "precue": (lambda cues: {
                "ok": (not cues) or bool(getattr(g.speech, "cue_paths", [])),
                "detail": ("已关闭（tts.pre_cue 为空）" if not cues
                           else "提示音缓存 %d 条" % len(getattr(g.speech, "cue_paths", []) or [])),
            })(g.cfg["tts"].get("pre_cue") or []),
            "bubble": (lambda st: {
                "ok": bool(st.get("enabled")),
                "detail": "气泡小工 %s（已弹 %d 次）" % (
                    "在跑" if st.get("alive") else "待命", st.get("shown", 0)),
            })(getattr(g, "bubble", None).status() if getattr(g, "bubble", None) else {}),
            "voice": (lambda st: {
                "ok": bool(st.get("enabled")) and st.get("mode") != "off",
                "detail": "语音 %s（%s，热键 %s）" % (
                    st.get("mode"), st.get("state"), st.get("hotkey")),
            })(getattr(g, "voice", None).status() if getattr(g, "voice", None) else {}),
            "ledger": {"ok": os.path.exists(g.ledger.path), "detail": g.ledger.path},
            "lore": {"ok": bool(getattr(g, "lore", None) and g.lore.enabled),
                     "detail": "资料库 %d 本手册（%s）" % (
                         len(g.lore.store.entries) if getattr(g, "lore", None) else 0,
                         g.lore.base if getattr(g, "lore", None) else "没起来")},
        }
        self._send_json({"ok": all(c["ok"] for c in checks.values()), "checks": checks})

    def _handle_voice(self):
        voice = GATEWAY.voice
        if self.command == "GET":
            self._send_json({"ok": True, "voice": voice.status()})
            return
        try:
            req = self._read_json()
        except ValueError:
            req = {}
        action = str(req.get("action") or "start").lower()
        if action == "start":
            started = voice.begin()
            self._send_json({"ok": True, "started": started, "voice": voice.status()})
        elif action == "stop":
            voice.cancel.set()
            self._send_json({"ok": True, "voice": voice.status()})
        elif action == "set_mode":
            mode = str(req.get("mode") or "").lower()
            if mode not in ("off", "ptt", "call"):
                self._send_json({"ok": False, "error": "mode 只能是 off/ptt/call"}, 400)
                return
            voice.mode = mode
            voice.enabled = mode != "off"
            log.info("语音模式 → %s", mode)
            self._send_json({"ok": True, "voice": voice.status()})
        else:
            self._send_json({"ok": False, "error": "action 只能是 start/stop/set_mode"}, 400)

    def _handle_voice_input(self):
        """键盘兜底：把一句话当成“听清了”直接走语音回答（测试和没麦时用）。"""
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        text = str(req.get("text") or "").strip()
        if not text:
            self._send_json({"ok": False, "error": "text 为空"}, 400)
            return
        reply = GATEWAY.voice_reply(text)
        self._send_json({"ok": bool(reply), "reply": reply[:300]})

    def _handle_titles(self):
        """往作品小抄里加/改条目：POST {"绝区零": "一句话说明"}，存盘即生效（热重载）。"""
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        items = req.get("items") if isinstance(req.get("items"), dict) else req
        if not isinstance(items, dict) or not items:
            self._send_json({"ok": False, "error": "给我 {作品名: 一句话说明}"}, 400)
            return
        added = [str(k).strip() for k in items if str(k).strip() and str(k) != "items"]
        try:
            total = save_title_notes(items)
        except OSError as exc:
            self._send_json({"ok": False, "error": "写不进去：%s" % exc}, 500)
            return
        log.info("作品小抄加了 %d 条：%s", len(added), "、".join(added[:6]))
        self._send_json({"ok": True, "added": added, "count": total})

    def _handle_lore(self):
        """资料库：GET 看状态；POST {action: learn|forget|retrieve|ensure|status}。"""
        g = GATEWAY
        lo = getattr(g, "lore", None)
        if lo is None:
            self._send_json({"ok": False, "error": "资料库没起来"}, 503)
            return
        if self.command == "GET":
            self._send_json(lo.status())
            return
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        action = str(req.get("action") or "status").lower()
        name = str(req.get("name") or "").strip()
        kind = str(req.get("kind") or "game").strip().lower()
        aliases = [str(a) for a in (req.get("aliases") or []) if str(a).strip()]
        if action == "learn":
            if not name:
                self._send_json({"ok": False, "error": "缺 name"}, 400)
                return
            res = lo.learn_now(name, kind, aliases=aliases)
            self._send_json(res, 200 if res.get("ok") else 500)
        elif action == "ensure":
            if not name:
                self._send_json({"ok": False, "error": "缺 name"}, 400)
                return
            lo.ensure(name, kind, aliases=aliases)
            self._send_json({"ok": True, "queued": name})
        elif action == "forget":
            if not name:
                self._send_json({"ok": False, "error": "缺 name"}, 400)
                return
            res = lo.forget(name)
            self._send_json(res, 200 if res.get("ok") else 404)
        elif action == "retrieve":
            query = str(req.get("query") or "")
            hits = lo.retrieve(query, game=str(req.get("game") or ""))
            self._send_json({"ok": True, "hits": [
                {"name": h["name"], "heading": h["heading"], "score": h["score"],
                 "text": h["text"]} for h in hits]})
        elif action == "status":
            self._send_json(lo.status())
        else:
            self._send_json({"ok": False, "error": "action 只能是 learn/ensure/forget/retrieve/status"}, 400)

    def _handle_warm(self):
        """重新预热固定句子（DSH 状态、气泡文案）；force=true 时先删缓存重合成。"""
        try:
            req = self._read_json()
        except ValueError:
            req = {}
        extra = [str(x) for x in (req.get("phrases") or []) if str(x).strip()]
        force = bool(req.get("force"))
        include_all = bool(req.get("all", True))
        threading.Thread(target=GATEWAY.warm_fixed_phrases,
                         args=(extra, force, include_all), daemon=True).start()
        self._send_json({"ok": True, "queued": len(extra), "force": force, "all": include_all})

    def _handle_bubble(self):
        """让她头顶出一个原生气泡（字是我们给的）。"""
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        text = str(req.get("text") or "").strip()
        if not text:
            self._send_json({"ok": False, "error": "text 为空"}, 400)
            return
        duration = req.get("duration_ms")
        ack = GATEWAY.bubble.show(text, int(duration) if duration else None)
        self._send_json({"ok": bool(ack.get("shown")), "shown": bool(ack.get("shown")),
                         "reason": ack.get("reason", ""), "text": text[:60],
                         "anchor": ack.get("anchor"), "screen": ack.get("screen"),
                         "status": GATEWAY.bubble.status()})

    def _handle_say(self):
        try:
            req = self._read_json()
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        n = GATEWAY.speech.say(str(req.get("text") or ""), tag="say")
        self._send_json({"ok": bool(n), "sentences": n})

    def _send_blank_completion(self, client_stream: bool, model: str) -> None:
        """回一个空的完成（一个字都不说），让桌宠那边安静地收场。"""
        try:
            if client_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                done = {
                    "id": "chatcmpl-gw", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                self.wfile.write(b"data: " + json.dumps(done, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self._send_json({
                    "id": "chatcmpl-gw", "object": "chat.completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""},
                                 "finish_reason": "stop"}],
                })
        except Exception as exc:  # noqa: BLE001
            log.info("回空完成失败（反正也不说）：%s", exc)

    def _handle_chat(self):
        try:
            payload = self._read_json()
        except ValueError:
            self._send_json({"error": {"message": "bad json"}}, 400)
            return
        client_stream = bool(payload.get("stream"))
        model = payload.get("model", "")
        t0 = time.time()
        try:
            payload, scenario, user_text = GATEWAY.prepare(payload)
        except Exception as exc:
            log.exception("prepare failed")
            self._send_json({"error": {"message": str(exc)}}, 500)
            return

        log.info("请求 scenario=%s stream=%s user=%r", scenario, client_stream, user_text[:60])
        # 黑屏守卫：独占全屏游戏 / 锁屏 / DRM 画面截出来一片黑，说出去就是「我什么也看不见」。
        # 连续 2 张都黑就整轮跳过（不叫模型、不说话、不冒泡），也不占账本。
        if scenario == "vision" and GATEWAY.vision_is_black():
            log.info("识屏：这一帧太黑（%.0f%% 接近纯黑），跳过这一轮", GATEWAY.vision_dark_ratio() * 100)
            self._send_blank_completion(client_stream, model)
            return
        # 新一轮开始：前一段还没念完就掐掉，直接改念这一轮
        GATEWAY.speech.interrupt()
        # 「让我看看」按最近的实测耗时推迟到首句开口前（见 arm_vision_cue）
        if scenario == "vision":
            GATEWAY.speech.arm_vision_cue()

        sink: list[str] = []
        gen = GATEWAY.stream_reply(payload, scenario, sink)

        try:
            if client_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                first = True
                for piece in gen:
                    frame = {
                        "id": "chatcmpl-gw",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": ({"role": "assistant"} if first else {}) | {"content": piece},
                            "finish_reason": None,
                        }],
                    }
                    first = False
                    self.wfile.write(b"data: " + json.dumps(frame, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                done = {
                    "id": "chatcmpl-gw", "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                self.wfile.write(b"data: " + json.dumps(done, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                for _ in gen:
                    pass
                reply = "".join(sink).strip()
                data = {
                    "id": "chatcmpl-gw", "object": "chat.completion",
                    "created": int(time.time()), "model": model,
                    "choices": [{
                        "index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": reply},
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                self._send_json(data)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            log.error("upstream HTTP %s: %s", exc.code, detail)
            if client_stream:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self._send_json({"error": {"message": detail, "code": exc.code}}, 502)
            return
        except Exception as exc:
            log.exception("gateway failed")
            if not client_stream:
                self._send_json({"error": {"message": str(exc)}}, 500)
            return

        GATEWAY.finish_turn(scenario, user_text, "".join(sink).strip(), int((time.time() - t0) * 1000))


class _GatewayServer(ThreadingHTTPServer):
    """不让第二个网关悄悄绑上同一个端口。

    Windows 上 SO_REUSEADDR 是「允许抢占」，不是「允许重用」：两个进程能同时
    绑 9888，各读各的 DSH 事件、各说各的——听起来就是「一句话念两遍」。
    """

    allow_reuse_address = False
    daemon_threads = True


def _single_instance_or_exit() -> bool:
    """同一时间只跑一个网关；已经有实例在跑就直接退出。

    端口抢绑是兜底，命名互斥体是主判据（绑端口失败要等到启动后期才报错）。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\paopao_pet_gateway_singleton")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            return False
        globals()["_SINGLETON_MUTEX"] = handle  # 进程活多久就持有多久
        return True
    except Exception:  # noqa: BLE001
        return True


def main() -> None:
    global GATEWAY
    cfg = load_config()
    setup_logging(cfg)
    if not _single_instance_or_exit():
        log.warning("已经有一个网关在跑了，这个实例直接退出（否则会一句话念两遍）")
        return
    GATEWAY = Gateway(cfg)
    server = _GatewayServer((cfg["listen_host"], int(cfg["listen_port"])), Handler)
    server.daemon_threads = True
    log.info(
        "泡泡桌宠网关 v2 启动：http://%s:%d → %s（音量 %.1f，合成线程 %d，DSH 事件 %s）",
        cfg["listen_host"], int(cfg["listen_port"]), GATEWAY.upstream_url(),
        float(cfg["tts"].get("volume", 1.0)), int(cfg["tts"].get("synth_workers", 2)),
        "开" if cfg["dsh"].get("enabled") else "关",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
