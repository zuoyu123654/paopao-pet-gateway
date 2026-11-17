# -*- coding: utf-8 -*-
"""读取本机已安装游戏的版本号（只认写进手册的几款）。

优先读 Windows「应用和功能」的卸载注册表：DisplayName + DisplayVersion。
米哈游启动器安装的原神/星铁/绝区零，以及异环，都会在这里留一条。
读不到就返回空，不猜、不编。
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import winreg

# 标题键 -> 注册表 DisplayName 里能认出的关键词
GAME_MATCHERS = {
    "原神": ("原神", "genshin impact"),
    "崩坏：星穹铁道": ("崩坏：星穹铁道", "崩坏:星穹铁道", "star rail"),
    "绝区零": ("绝区零", "zenless zone zero"),
    "异环": ("异环", "neverness to everness"),
}

# 官方首页/新闻列表：注册表版本可能是安装器旧版本，官网更接近当前版本
OFFICIAL_PAGES = {
    "原神": "https://ys.mihoyo.com/",
    "崩坏：星穹铁道": "https://sr.mihoyo.com/",
    "绝区零": "https://zzz.mihoyo.com/",
    "异环": "https://yh.wanmei.com/m/news/gamebroad/",
}

UNINSTALL_KEYS = (
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
)


def _trim_version(raw: str) -> str:
    m = re.search(r"(\d+\.\d+)(?:\.\d+)*", str(raw or ""))
    return m.group(1) if m else ""


def installed_versions() -> dict[str, str]:
    """返回 {标题键: '7.2'}；只包含注册表里真的有版本的几款。"""
    found: dict[str, str] = {}
    for hive, path in UNINSTALL_KEYS:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        try:
            n = winreg.QueryInfoKey(root)[0]
            for i in range(n):
                try:
                    name = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, name) as sub:
                        try:
                            display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                        except OSError:
                            continue
                        low = display.lower()
                        for title, needles in GAME_MATCHERS.items():
                            if not any(x.lower() in low for x in needles):
                                continue
                            try:
                                ver = _trim_version(str(winreg.QueryValueEx(sub, "DisplayVersion")[0]))
                            except OSError:
                                ver = ""
                            if ver and title not in found:
                                found[title] = ver
                except OSError:
                    continue
        finally:
            root.Close()
    return found


def _fetch(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:3]) or (0,)


def official_versions() -> dict[str, str]:
    """从官网首页/新闻列表读当前版本；读不到就空着。"""
    out: dict[str, str] = {}
    for title, url in OFFICIAL_PAGES.items():
        try:
            text = _fetch(url)
            # 优先看 <title>/og:title/description，别被静态资源的 3.22.15 带偏
            heads = []
            for pat in (r"<title[^>]*>(.*?)</title>",
                        r'<meta[^>]+(?:property|name)="(?:og:title|description)"[^>]+content="([^"]+)"'):
                for m in re.finditer(pat, text, re.S | re.I):
                    heads.append(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))))
            head_text = " ".join(heads)
            versions = re.findall(r"(?<!\d)(\d+\.\d+)(?!\d)", head_text)
            if not versions:
                versions = re.findall(r"(\d+\.\d+)版本", text)
            if versions:
                out[title] = versions[0]
        except Exception:  # noqa: BLE001
            continue
    return out


_VERSION_CACHE: dict[str, str] = {}
_VERSION_CACHE_AT = 0.0


def current_versions(force: bool = False) -> dict[str, str]:
    """注册表 + 官网，取较高版本；只返回四款有手册的游戏。结果缓存 30 分钟。"""
    global _VERSION_CACHE, _VERSION_CACHE_AT
    import time
    if not force and _VERSION_CACHE and time.time() - _VERSION_CACHE_AT < 1800:
        return dict(_VERSION_CACHE)
    reg = installed_versions()
    off = official_versions()
    out: dict[str, str] = {}
    for title in GAME_MATCHERS:
        vals = [v for v in (reg.get(title), off.get(title)) if v]
        if vals:
            out[title] = max(vals, key=_ver_tuple)
    _VERSION_CACHE = dict(out)
    _VERSION_CACHE_AT = time.time()
    return out


def _bwiki_search(wiki: str, query: str) -> list[str]:
    url = ("https://wiki.biligame.com/%s/api.php?action=query&list=search"
           "&srsearch=%s&format=json&srlimit=8" % (wiki, urllib.parse.quote(query)))
    data = json.loads(_fetch(url))
    return [str(x.get("title") or "") for x in
            ((data.get("query") or {}).get("search") or []) if x.get("title")]


def _bwiki_text(wiki: str, page: str) -> str:
    url = ("https://wiki.biligame.com/%s/api.php?action=parse&page=%s"
           "&prop=wikitext&format=json" % (wiki, urllib.parse.quote(page)))
    data = json.loads(_fetch(url))
    raw = str(((data.get("parse") or {}).get("wikitext") or {}).get("*") or "")
    raw = re.sub(r"\{\{[^{}]*\}\}", " ", raw)
    raw = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", raw)
    return raw


def announcement_text(title: str, version: str) -> str:
    """尽力拿这个版本的新内容原文；没有就返回空，不编。"""
    version = str(version or "").strip()
    if not version:
        return ""
    if title == "异环":
        try:
            raw = _fetch(OFFICIAL_PAGES[title])
            for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S | re.I):
                text = html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
                if ("%s版本" % version) in text:
                    href = m.group(1)
                    if href.startswith("/"):
                        href = "https://yh.wanmei.com" + href
                    page = _fetch(href)
                    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page,
                                  flags=re.S | re.I)
                    page = html.unescape(re.sub(r"<[^>]+>", " ", page))
                    return re.sub(r"\s+", " ", page)[:14000]
        except Exception:  # noqa: BLE001
            return ""
        return ""
    wiki = {"原神": "ys", "崩坏：星穹铁道": "sr", "绝区零": "zzz"}.get(title, "")
    if not wiki:
        return ""
    try:
        pages = _bwiki_search(wiki, "%s版本" % version)
        for page in pages[:3]:
            text = _bwiki_text(wiki, page)
            if version in text:
                return text[:14000]
    except Exception:  # noqa: BLE001
        pass
    return ""


if __name__ == "__main__":
    for k, v in installed_versions().items():
        print("%s\t%s" % (k, v))
