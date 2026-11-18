# -*- coding: utf-8 -*-
"""气泡桥：网关 → 气泡小工（bubble_host.py）。

为什么单独一个进程：气泡要 Qt 事件循环，网关是纯 http 线程池；Qt 崩了不能
把语音一起带走。小工按需拉起，网关退出时它读到 stdin EOF 自己退。

协议：stdin 一行一个 JSON 命令（带自增 id），stdout 一行一个 ack（带回同一个
id）。日志走小工的 stderr，落到 logs\\bubble.log，不会和 ack 混。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger("petgw")

APP_DIR = os.path.dirname(os.path.abspath(__file__))


class BubbleBridge:
    def __init__(self, cfg: dict):
        conf = cfg.get("bubble") or {}
        self.enabled = bool(conf.get("enabled", True))
        self.script = os.path.join(APP_DIR, "bubble_host.py")
        self.duration_ms = int(conf.get("duration_ms", 3200) or 3200)
        self.max_chars = int(conf.get("max_chars", 60) or 60)
        self.capture_window = bool(conf.get("capture_window", False))
        self.ack_timeout = float(conf.get("ack_timeout", 3.0) or 3.0)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.ipc: socket.socket | None = None
        self.ipc_file = None
        self.acks: "queue.Queue[dict]" = queue.Queue()
        self._seq = 0
        self.last_error = ""
        self.shown = 0
        self.last_reason = ""

    # ------------------------------------------------------------ 进程
    def _kill_stale(self) -> None:
        """网关重启后，上一任气泡小工可能还活着（stdin 没断干净），先收掉。"""
        path = os.path.join(APP_DIR, "logs", "bubble_host.pid")
        try:
            with open(path, "r", encoding="utf-8") as f:
                pid = int((f.read() or "0").strip())
        except (OSError, ValueError):
            return
        if pid <= 0 or pid == os.getpid():
            return
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5)
            log.info("收掉旧气泡小工（pid %s）", pid)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(path)
        except OSError:
            pass

    def _spawn(self) -> None:
        self._kill_stale()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if self.capture_window:
            env["PAOPAO_BUBBLE_CAPTURE"] = "1"
        log_path = os.path.join(APP_DIR, "logs", "bubble.log")
        try:
            stderr = open(log_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            stderr = subprocess.DEVNULL
        self.proc = subprocess.Popen(
            [sys.executable, self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            text=True, encoding="utf-8", errors="replace", env=env,
            cwd=APP_DIR, creationflags=flags,
        )
        threading.Thread(target=self._read_acks, args=(self.proc,), daemon=True).start()
        log.info("气泡小工已拉起（pid %s）", self.proc.pid)

    def _read_acks(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.acks.put(json.loads(line))
            except ValueError:
                continue

    def _connect_ipc(self) -> bool:
        """先连已经活着的气泡小工；这样网关重启不会换窗口句柄。"""
        if self.ipc is not None:
            return True
        try:
            s = socket.create_connection(("127.0.0.1", 9894), timeout=0.4)
            s.settimeout(max(1.0, self.ack_timeout))
            self.ipc = s
            self.ipc_file = s.makefile("rwb")
            return True
        except OSError:
            self.ipc = None
            self.ipc_file = None
            return False

    def _ensure(self) -> bool:
        if self._connect_ipc():
            return True
        if self.proc is not None and self.proc.poll() is None:
            return self._connect_ipc()
        try:
            self._spawn()
            for _ in range(20):
                if self._connect_ipc():
                    return True
                time.sleep(0.1)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("气泡小工拉不起来：%s", exc)
            return False

    def warm(self) -> None:
        """启动时就把气泡窗口拉起来，OBS 一开始就能选到“泡泡气泡”。"""
        if self.enabled:
            self._ensure()

    def _request(self, payload: dict) -> dict | None:
        """发一条命令，等它自己那个 id 的 ack（超时返回 None）。"""
        if not self.enabled:
            return None
        with self.lock:
            if not self._ensure():
                return None
            self._seq += 1
            payload = {**payload, "id": self._seq}
            if self.ipc_file is not None:
                try:
                    self.ipc_file.write(
                        (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.ipc_file.flush()
                    line = self.ipc_file.readline()
                    if line:
                        return json.loads(line.decode("utf-8", "replace"))
                except Exception as exc:  # noqa: BLE001
                    self.last_error = str(exc)
                    log.warning("气泡 IPC 断了（%s），下一句重连", exc)
                    try:
                        self.ipc_file.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self.ipc_file = None
                    self.ipc = None
                return None
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("气泡指令发不出去（%s），下一句重拉", exc)
                self.proc = None
                return None
            want = self._seq
        deadline = self.ack_timeout
        while deadline > 0:
            try:
                ack = self.acks.get(timeout=min(0.5, deadline))
            except queue.Empty:
                deadline -= 0.5
                continue
            if ack.get("id") == want:
                return ack
            deadline -= 0.1
        return None

    # ------------------------------------------------------------ 对外
    def start(self) -> None:
        """开机就把小工拉起来：托盘菜单住在它里面，不能等到第一次弹气泡。"""
        if not self.enabled:
            return
        threading.Thread(target=self._ensure, name="bubble-spawn", daemon=True).start()

    def show(self, text: str, duration_ms: int | None = None,
             scale: float | None = None, offset_y: int = 0) -> dict:
        text = str(text or "").strip()
        if not text:
            return {"shown": False, "reason": "empty"}
        if len(text) > self.max_chars:
            text = text[: self.max_chars]
        req = {"cmd": "show", "text": text,
               "duration_ms": int(duration_ms or self.duration_ms)}
        if scale is not None:
            req["scale"] = max(0.5, min(3.0, float(scale)))
        if offset_y:
            req["offset_y"] = int(offset_y)
        ack = self._request(req)
        if ack is None:
            self.last_reason = "no-ack"
            return {"shown": False, "reason": "no-ack"}
        shown = bool(ack.get("shown"))
        self.last_reason = str(ack.get("reason") or "")
        if shown:
            self.shown += 1
        return ack

    def hide(self) -> bool:
        ack = self._request({"cmd": "hide"})
        return bool(ack)

    def status(self) -> dict:
        alive = bool(self.ipc is not None
                     or (self.proc is not None and self.proc.poll() is None))
        return {"enabled": self.enabled, "alive": alive, "shown": self.shown,
                "error": self.last_error, "last_reason": self.last_reason}
