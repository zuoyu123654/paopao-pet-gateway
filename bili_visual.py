# -*- coding: utf-8 -*-
"""从 B站官方动态里找角色立绘（异环：「海特洛特别资讯 | 角色名」）。

只负责找图和下载；外观描述由网关的视觉模型完成。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request

BILI_UID = {"异环": "3546636978489848"}


def _bili() -> str:
    return shutil.which("bili") or "bili"


def find_character_pics(mid: str, names: list[str], max_pages: int = 10) -> dict[str, str]:
    """返回 {角色名: 图片URL}；只匹配「海特洛特别资讯 | 名字」。"""
    want = {str(x).strip() for x in names if str(x).strip()}
    if not want:
        return {}
    found: dict[str, str] = {}
    offset = ""
    for _ in range(max_pages):
        cmd = [_bili(), "--raw", "dynamic", "list", "--mid", str(mid), "--compact"]
        if offset:
            cmd += ["--offset", offset]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
            data = json.loads(proc.stdout).get("data") or {}
        except Exception:
            break
        for item in data.get("items") or []:
            opus = (((item.get("modules") or {}).get("module_dynamic") or {})
                    .get("major") or {}).get("opus") or {}
            m = re.search(r"海特洛特别资讯\s*[|｜]\s*(.+)$",
                          str(opus.get("title") or ""))
            if not m:
                continue
            name = m.group(1).strip()
            if name not in want or name in found:
                continue
            pics = opus.get("pics") or []
            if pics:
                found[name] = str(pics[0].get("url") or "")
        if len(found) >= len(want):
            break
        offset = str(data.get("offset") or "")
        if not offset:
            break
    return found


def download_image(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
