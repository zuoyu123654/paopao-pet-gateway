# -*- coding: utf-8 -*-
"""固定文案：语音模式的三句气泡 + 模式切换的提示。

放一处是为了**预合成**：这些都是固定句子，启动时合成好，播的时候直接用缓存，
不再当场等 TTS（第一句流式再快也要 300ms 起步）。

2026-11-17 改：每个模式**备两句**，切换时随机挑一句说（老大要的），
而且句子里**不出现「XX 模式」**这种机器人腔。
"""
from __future__ import annotations

# 语音模式（气泡是提示，不出声；所以它在这里有名字，但不在 ALL 里，不预合成）
LISTENING = "我在听"
THINKING = "唔……我想想"
HEARD_NOTHING = "没听清，再说一遍？"
STUCK = "我这会儿有点卡壳…"
VOICE_OFF_HINT = "语音关着呢"

# 托盘里语音那几项的反馈（这几个不走网关，气泡小工直接冒）
TRAY_FEEDBACK = {
    "voice_off": "语音关掉了。",
    "ptt": "按键说话，Ctrl+Alt+V。",
    "call": "语音电话开着呢。",
    "lock_5m": "手动锁改回 5 分钟。",
    "lock_forever": "手动锁一直保持。",
    "host_on": "直播弹幕开了，我帮你看弹幕。",
    "host_off": "直播弹幕关了。",
    "host_speak_on": "回弹幕出声开了。",
    "host_speak_off": "回弹幕只冒泡，不出声。",
}

# 语音模式切换时**出声**的提示（气泡那边托盘本来就会冒，这里补「听得见」的那份）
VOICE_MODE_PHRASES: dict[str, list[str]] = {
    "call": [
        "语音电话开着呢，随时说。",
        "行，电话通了，我听着呢。",
    ],
    "ptt": [
        "改按键说话了，按住 Ctrl Alt V。",
        "行，按住键说话，我等着。",
    ],
    "off": [
        "语音关掉了。",
        "好，那我先不听啦。",
    ],
}

MODE_TIMEOUT = "回自动了"

# 每个模式两句，随机挑一句（都要预合成）
MODE_PHRASES: dict[str, list[str]] = {
    "auto": [
        "好，我自己看着来。",
        "行，那我继续自己瞅。",
    ],
    "normal": [
        "好，我自己看着来。",
        "行，那我继续自己瞅。",
    ],
    "off": [
        "行，不看了。",
        "好，那我先不看了。",
    ],
    "video": [
        "哦，刷B站视频啊。",
        "哦，B站视频啊，我跟你一块儿看。",
    ],
    # 通用视频（托盘「通用视频」/自动判定的播放器）：跟 B站 那条分开，
    # 别让老大在看本地播放器/别的网站时听到「刷B站视频」
    "video_any": [
        "哦，看视频呢，我陪你一块儿看。",
        "行，那我跟着你看会儿。",
    ],
    "anime": [
        "哦，看番去啦。",
        "哦，看番啊，那我跟着看。",
    ],
    "live": [
        "哦，看直播啊。我陪你一起看，顺便听你唠两句。",
        "哦，直播开了。我在这儿陪你，别一个人傻乐。",
    ],
    "course": [
        "哦，上课时间。我跟着听，不吵你。",
        "哦，上网课啊。我安静陪着，有重点我记着。",
    ],
    "game": [
        "哦，打上了呀——那我蹲旁边陪你，边打边聊。",
        "哦，开局了？我在这儿陪你，顺便点评两句。",
    ],
}

# 自动进游戏时的那两句（本来想单独一版，老大说只留一句口径：随机就行）
GAME_ON = MODE_PHRASES["game"]
GAME_OFF = ["不打啦？那我先眯一会儿。", "哦，收工了？那我先歇会儿。"]
# 跟着桌宠「主动识屏」开关走的两句：
# 开 → 桌宠那边已经有气泡了，我们只出声（用她那句原文）
# 关 → 桌宠那边没气泡，我们既出声也冒泡
PROACTIVE_ON = "主动识屏已开启，我会偶尔看看你正在用的软件。"
PROACTIVE_OFF = "行，不看了"


def _flat() -> list[str]:
    out = [THINKING, HEARD_NOTHING, STUCK, VOICE_OFF_HINT, MODE_TIMEOUT,
           PROACTIVE_ON, PROACTIVE_OFF]
    out += list(TRAY_FEEDBACK.values())
    for phrases in MODE_PHRASES.values():
        out += phrases
    out += GAME_OFF
    for phrases in VOICE_MODE_PHRASES.values():
        out += phrases
    return out


# 全部要预合成的固定句子
ALL = tuple(dict.fromkeys(_flat()))     # 去重但保序
