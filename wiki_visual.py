# -*- coding: utf-8 -*-
"""从 BWiki 角色页取官方立绘（原神/星铁/绝区零）。

异环没有 BWiki，走 bili_visual.py 的官方动态。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

WIKI = {
    "原神": "ys",
    "崩坏：星穹铁道": "sr",
    "绝区零": "zzz",
}
UA = {"User-Agent": "Mozilla/5.0"}


def _api(wiki: str, params: dict) -> dict:
    q = dict(params)
    q.setdefault("format", "json")
    url = "https://wiki.biligame.com/%s/api.php?%s" % (
        wiki, urllib.parse.urlencode(q))
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def find_portrait(title: str, name: str) -> str:
    """返回角色立绘 URL；找不到返回空。"""
    wiki = WIKI.get(title)
    if not wiki:
        return ""
    pages = _api(wiki, {"action": "query", "list": "search",
                        "srsearch": name, "srlimit": 5})
    page = ""
    for item in ((pages.get("query") or {}).get("search") or []):
        t = str(item.get("title") or "")
        if t == name or t.startswith(name + "/"):
            page = t
            break
    if not page:
        return ""
    data = _api(wiki, {"action": "parse", "page": page, "prop": "images"})
    images = [str(x) for x in ((data.get("parse") or {}).get("images") or [])]
    if not images:
        return ""
    # 优先「XX立绘」「角色立绘-XX」「代理人档案-XX」，再退含名字的立绘/图鉴图
    prefer = [
        name + "立绘.png", name + "立绘.jpg",
        "角色立绘-" + name + ".png", "角色立绘-" + name + ".jpg",
    ]
    chosen = ""
    for p in prefer:
        if p in images:
            chosen = p
            break
    if not chosen:
        for img in images:
            if name in img and ("立绘" in img or "角色图鉴" in img or "档案" in img):
                chosen = img
                break
    if not chosen:
        for img in images:
            if name in img and not img.endswith(".gif"):
                chosen = img
                break
    if not chosen:
        return ""
    info = _api(wiki, {"action": "query", "titles": "File:" + chosen,
                       "prop": "imageinfo", "iiprop": "url"})
    pages2 = (info.get("query") or {}).get("pages") or {}
    for pageinfo in pages2.values():
        for ii in pageinfo.get("imageinfo") or []:
            url = str(ii.get("url") or "")
            if url:
                return url
    return ""


def download_image(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0",
                      "Referer": "https://wiki.biligame.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
