# -*- coding: utf-8 -*-
"""读取本机已安装游戏的版本号（只认写进手册的几款）。

优先读 Windows「应用和功能」的卸载注册表：DisplayName + DisplayVersion。
米哈游启动器安装的原神/星铁/绝区零，以及异环，都会在这里留一条。
读不到就返回空，不猜、不编。
"""
from __future__ import annotations

import re
import winreg

# 标题键 -> 注册表 DisplayName 里能认出的关键词
GAME_MATCHERS = {
    "原神": ("原神", "genshin impact"),
    "崩坏：星穹铁道": ("崩坏：星穹铁道", "崩坏:星穹铁道", "star rail"),
    "绝区零": ("绝区零", "zenless zone zero"),
    "异环": ("异环", "neverness to everness"),
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


if __name__ == "__main__":
    for k, v in installed_versions().items():
        print("%s\t%s" % (k, v))
