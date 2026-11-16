# -*- coding: utf-8 -*-
"""B站「现在放到哪」+ 按进度切字幕（老大 2026-11-17 定的方案 B：Edge 调试端口）。

为什么是这条路：不用装浏览器扩展，Edge 启动时加 `--remote-debugging-port=9222` 就行；
CDP（Chrome DevTools Protocol）能直接拿到 `location.href`（里面有 bvid）、`document.title`、
以及 `video` 元素的 `currentTime / duration / paused`。

字幕：走 `bili` CLI（本机已登录）——`bili video subtitle <bvid> --page N` 返回带时间轴的
`{from, to, content}`。按 bvid+page 缓存到 `audio/subtitles/`，同一集反复看不重复请求。

用法（网关内部）：
    self.player = PlayerWatch(cfg, self); self.player.start()
    block = self.player.context_block()      # 识屏时插进 system
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import threading
import time
import urllib.request

import logging

log = logging.getLogger("petgw")

APP_DIR = os.path.dirname(os.path.abspath(__file__))

BV_RE = re.compile(r"/(?:video|bangumi/play)/(BV[0-9A-Za-z]{8,12})")
PAGE_RE = re.compile(r"[?&]p=(\d+)")
# 只有这些页面算「在看 B 站视频」，别的网页不掺和
VIDEO_URL_HINTS = ("bilibili.com/video/", "bilibili.com/bangumi/play/",
                   "bilibili.com/list/", "youtube.com/watch")


def _fmt(sec: float) -> str:
    sec = max(0, int(sec or 0))
    return "%d:%02d" % (sec // 60, sec % 60)


class PlayerWatch(threading.Thread):
    """每秒读一次浏览器当前在看什么、放到第几秒；识屏时切出「前 N 秒台词」。"""

    def __init__(self, cfg: dict, gateway):
        super().__init__(name="player-watch", daemon=True)
        conf = cfg.get("player_watch") or {}
        self.cfg = cfg
        self.gateway = gateway
        self.enabled = bool(conf.get("enabled", True))
        self.port = int(conf.get("cdp_port", 9222))
        self.poll = max(0.5, float(conf.get("poll_seconds", 1.0)))
        self.fresh = float(conf.get("fresh_seconds", 8))
        self.window = float(conf.get("subtitle_window_seconds", 90))
        self.cache_dir = os.path.join(APP_DIR, "audio", "subtitles")
        self.cache_seconds = float(conf.get("subtitle_cache_hours", 24)) * 3600
        self.bili = str(conf.get("bili_cli") or "bili")
        # 兜底：CDP 不可用时（Edge 没带调试端口 / 端口被占），用 B站历史接口的进度顶上。
        # 历史里的 progress 是网页播放器定期上报的，通常晚十几秒，够用来切「前 90 秒字幕」。
        self.use_history = bool(conf.get("history_fallback", True))
        self.history_refresh = max(5.0, float(conf.get("history_refresh_seconds", 15)))
        self.history_max_age = float(conf.get("history_max_age_minutes", 10)) * 60
        self.cred_path = str(conf.get("bili_credential") or os.path.join(
            os.environ.get("APPDATA", ""), "bili-cli", "credential.json"))
        self._history_at = 0.0
        self._history_state: dict = {}
        os.makedirs(self.cache_dir, exist_ok=True)

        self.lock = threading.Lock()
        self.state: dict = {}
        self.last_error = ""
        self.ws = None
        self.ws_target = ""
        self._subs: dict[str, list] = {}

    # ---------------------------------------------------------------- CDP
    def _targets(self) -> list:
        url = "http://127.0.0.1:%d/json" % self.port
        with urllib.request.urlopen(url, timeout=0.8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _pick(self, targets: list) -> dict | None:
        pages = [t for t in targets if t.get("type") == "page"
                 and str(t.get("url") or "").startswith("http")]
        for t in pages:
            u = str(t.get("url") or "").lower()
            if any(h in u for h in VIDEO_URL_HINTS):
                return t
        return None

    def _connect(self, ws_url: str):
        from websocket import create_connection

        self.ws = create_connection(ws_url, timeout=2.0)
        self.ws_target = ws_url

    def _eval(self, expr: str, timeout: float = 2.5):
        """在页面里跑一段 JS，返回结果（字符串/数字）。断线就重连一次。"""
        for attempt in (1, 2):
            try:
                if self.ws is None:
                    raise RuntimeError("没连上")
                self.ws.settimeout(timeout)
                mid = int(time.time() * 1000) % 100000
                self.ws.send(json.dumps({
                    "id": mid, "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
                }))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    msg = json.loads(self.ws.recv())
                    if msg.get("id") == mid:
                        res = (msg.get("result") or {})
                        if res.get("exceptionDetails"):
                            raise RuntimeError(str(res["exceptionDetails"])[:120])
                        return ((res.get("result") or {}).get("value"))
                raise RuntimeError("等结果超时")
            except Exception:
                try:
                    if self.ws is not None:
                        self.ws.close()
                except Exception:  # noqa: BLE001
                    pass
                self.ws = None
                if attempt == 2:
                    raise
                # 重连一次
                try:
                    self._connect(self.ws_target)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError("重连失败：%s" % exc)

    JS_STATE = """
    (() => {
        const v = document.querySelector('video');
        return JSON.stringify({
            url: location.href,
            title: document.title,
            t: v ? v.currentTime : null,
            d: v ? v.duration : null,
            p: v ? v.paused : null,
            f: !!document.fullscreenElement
        });
    })()
    """

    def _poll_once(self) -> None:
        targets = self._targets()
        t = self._pick(targets)
        if not t:
            with self.lock:
                self.state = {}
            return
        ws_url = str(t.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            return
        if self.ws is None or self.ws_target != ws_url:
            self._connect(ws_url)
        raw = self._eval(self.JS_STATE)
        if not isinstance(raw, str):
            return
        data = json.loads(raw)
        url = str(data.get("url") or "")
        bv = BV_RE.search(url)
        page = PAGE_RE.search(url)
        with self.lock:
            self.state = {
                "url": url,
                "title": str(data.get("title") or "")[:100],
                "t": float(data.get("t") or 0),
                "d": float(data.get("d") or 0),
                "paused": bool(data.get("p")),
                "fullscreen": bool(data.get("f")),
                "bvid": bv.group(1) if bv else "",
                "page": int(page.group(1)) if page else 1,
                "at": time.time(),
            }
        self.last_error = ""

    def run(self) -> None:
        if not self.enabled:
            log.info("播放器跟踪：配置里关着")
            return
        warned = False
        while True:
            time.sleep(self.poll)
            try:
                self._poll_once()
                warned = False
            except Exception as exc:  # noqa: BLE001
                self.last_error = "调试端口 %d 没开（%s）" % (self.port, str(exc)[:40])
                if not warned:
                    warned = True
                    log.info("播放器跟踪：调试端口 %d 没开（用 start-edge-debug.ps1 开一个窗口；"
                             "没开时自动退到 B站历史进度）", self.port)
            # CDP 没数据（没开调试端口/没在看网页视频）→ 试试 B站历史里的进度
            if self.use_history and not self.snapshot():
                try:
                    if time.time() - self._history_at >= self.history_refresh:
                        self._history_at = time.time()
                        self._poll_history()
                except Exception as exc:  # noqa: BLE001
                    self.last_error = "history: %s" % exc

    # -------------------------------------------------------- 兜底：B站历史进度
    def _poll_history(self) -> None:
        try:
            with open(self.cred_path, "r", encoding="utf-8") as f:
                cred = json.load(f)
        except (OSError, ValueError):
            return
        names = {"sessdata": "SESSDATA", "bili_jct": "bili_jct", "buvid3": "buvid3",
                 "buvid4": "buvid4", "dedeuserid": "DedeUserID"}
        cookie = "; ".join("%s=%s" % (names[k], cred[k]) for k in names if cred.get(k))
        if not cookie:
            return
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/history/cursor?ps=3&max=0&view_at=0&business=",
            headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0",
                     "Referer": "https://www.bilibili.com/"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                obj = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            self.last_error = "history: %s" % exc
            return
        now = time.time()
        for it in ((obj.get("data") or {}).get("list") or []):
            his = it.get("history") or {}
            bvid = str(his.get("bvid") or "")
            if not bvid or str(it.get("business") or his.get("business") or "") == "live":
                continue
            if now - float(it.get("view_at") or 0) > self.history_max_age:
                break          # 列表按时间倒序，后面更旧，不用看了
            self._history_state = {
                "url": str(it.get("uri") or ""),
                "title": str(it.get("title") or "")[:100],
                "t": float(it.get("progress") or 0),
                "d": float(it.get("duration") or 0),
                "paused": None,
                "fullscreen": False,
                "bvid": bvid,
                "page": int(his.get("page") or 0) + 1,
                "source": "history",
                "at": now,
            }
            return
        self._history_state = {}

    # ------------------------------------------------------------ 字幕
    def _cache_path(self, bvid: str, page: int) -> str:
        return os.path.join(self.cache_dir, "%s_p%d.json" % (bvid, page))

    def _read_cache(self, bvid: str, page: int):
        path = self._cache_path(bvid, page)
        try:
            if time.time() - os.path.getmtime(path) > self.cache_seconds:
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("items")
        except (OSError, ValueError):
            return None

    def _fetch_subs(self, bvid: str, page: int):
        """跑 bili CLI 拿字幕（带时间轴）；拿不到返回 []。"""
        cmd = [self.bili, "video", "subtitle", bvid, "--page", str(page)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=25)
            obj = json.loads(r.stdout or "{}")
        except Exception as exc:  # noqa: BLE001
            log.info("字幕：bili 取不到（%s）：%s", bvid, exc)
            return []
        if not obj.get("ok"):
            log.info("字幕：%s 没有可用字幕（%s）", bvid,
                     str((obj.get("error") or {}).get("message") or "")[:40])
            return []
        data = obj.get("data") or {}
        items = [{"from": float(x.get("from") or 0),
                  "to": float(x.get("to") or 0),
                  "content": str(x.get("content") or "").strip()}
                 for x in (data.get("items") or []) if str(x.get("content") or "").strip()]
        try:
            with open(self._cache_path(bvid, page), "w", encoding="utf-8") as f:
                json.dump({"bvid": bvid, "page": page, "lang": data.get("lang"),
                           "items": items}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        log.info("字幕：%s p%d 拿到 %d 行", bvid, page, len(items))
        return items

    def subtitles(self, bvid: str, page: int = 1) -> list:
        key = "%s_p%d" % (bvid, page)
        if key in self._subs:
            return self._subs[key]
        items = self._read_cache(bvid, page)
        if items is None:
            items = self._fetch_subs(bvid, page)
        self._subs[key] = items
        return items

    # ------------------------------------------------------------ 对外
    def snapshot(self) -> dict:
        with self.lock:
            st = dict(self.state)
        if st and time.time() - st.get("at", 0) <= self.fresh:
            return st
        hs = dict(self._history_state)
        # 历史进度是定期上报的，容忍到「刷新间隔 + 一个 poll」
        if hs and time.time() - hs.get("at", 0) <= self.history_refresh + 5:
            return hs
        return {}

    BROWSERS = ("msedge", "chrome", "firefox", "brave", "opera", "vivaldi", "browser")

    def _foreground_is_browser(self) -> bool:
        """前台得是浏览器才算「在看」。不然人在打游戏、B站挂在后台，她会对着游戏说视频。"""
        try:
            from video_watch import foreground

            app = (foreground()[0] or "").lower().replace(".exe", "")
        except Exception:  # noqa: BLE001
            return True
        return (not app) or any(app.startswith(b) or b in app for b in self.BROWSERS)

    def context_block(self) -> str:
        """识屏时插进 system 的那段：现在在看什么、放到哪、前 N 秒台词。"""
        st = self.snapshot()
        if not st:
            return ""
        if not self._foreground_is_browser():
            return ""
        title = st.get("title") or ""
        pos = _fmt(st.get("t"))
        total = _fmt(st.get("d")) if st.get("d") else "?"
        by_history = st.get("source") == "history"
        head = "【老大此刻在看的】%s｜进度 %s / %s%s%s" % (
            title[:60], pos, total,
            "（暂停着）" if st.get("paused") else "",
            "（进度取自 B站历史，可能晚十几秒）" if by_history else "")
        bvid = st.get("bvid")
        if not bvid:
            return head
        subs = self.subtitles(bvid, int(st.get("page") or 1))
        if not subs:
            return head + "\n（这个视频没拿到字幕，只能靠画面和声音）"
        cur = st.get("t") or 0
        # 历史进度会滞后，窗口放宽一倍补上
        window = self.window * (2 if by_history else 1)
        lo = cur - window
        lines = []
        for it in subs:
            if it["from"] > cur:
                break
            if it["to"] < lo:
                continue
            lines.append("%s %s" % (_fmt(it["from"]), it["content"][:60]))
        if not lines:
            return head + "\n（刚开始，还没到有字幕的地方）"
        return "\n".join([
            head,
            "【B站字幕（按时间轴，最近 %d 秒，旧→新）】这些是给你看懂剧情的原文，" % int(window),
            *lines[-30:],
            "用它判断剧情走到哪了；**不要念字幕原文**，用自己的话讲你看到了什么、怎么想。",
        ])

    def status(self) -> dict:
        st = self.snapshot()
        return {
            "enabled": self.enabled,
            "port": self.port,
            "connected": bool(st),
            "error": self.last_error,
            "url": (st.get("url") or "")[:80],
            "title": (st.get("title") or "")[:40],
            "position": _fmt(st.get("t")) if st else "",
            "duration": _fmt(st.get("d")) if st and st.get("d") else "",
            "paused": st.get("paused"),
            "bvid": st.get("bvid", ""),
        }
