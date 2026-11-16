# -*- coding: utf-8 -*-
"""固定文案：语音模式的三句气泡 + 托盘切换的反馈。

放一处是为了**预合成**：这些都是固定句子，启动时合成好，播的时候直接用缓存，
不再当场等 TTS（第一句流式再快也要 300ms 起步）。
"""
from __future__ import annotations

# 语音模式（气泡是提示，不出声；所以它在这里有名字，但不在 ALL 里，不预合成）
LISTENING = "我在听"
THINKING = "唔……我想想"
HEARD_NOTHING = "没听清，再说一遍？"
STUCK = "我这会儿有点卡壳…"
VOICE_OFF_HINT = "语音关着呢"

# 托盘菜单切换后的反馈（默认 3.2 秒气泡）
# 别写「XX模式」这种词——听起来像机器在报告状态（老大 2026-11-17 说的）。
TRAY_FEEDBACK = {
    "auto": "好，我自己看着来。",
    "video": "哦，刷B站视频啊。",
    "anime": "哦，看番去啦。",
    "game": "哦，打上了呀——那我蹲旁边陪你打，不吵你。",
    "generic": "哦，看视频啊。",
    "voice_off": "语音关掉了。",
    "ptt": "按键说话，Ctrl+Alt+V。",
    "call": "语音电话开着呢。",
    "watch_off": "行，不看了。",
}
MODE_TIMEOUT = "回自动了"
# 每次模式切换都播一句（老大 2026-11-17 要的：切换必须有提示音 + 气泡）
MODE_BACK = "好，我自己看着来。"
LIVE_ON = "哦，看直播啊。"
COURSE_ON = "哦，上课时间。"
# 进游戏模式只有这一句（手动点、自动进都用它，不再分两版）
GAME_ON = "哦，打上了呀——那我蹲旁边陪你打，不吵你。"
GAME_OFF = "不打啦？那我先眯一会儿。"
MODE_PHRASES = {
    "auto": MODE_BACK,
    "normal": MODE_BACK,
    "off": "行，不看了。",
    "video": TRAY_FEEDBACK["video"],
    "anime": TRAY_FEEDBACK["anime"],
    "live": LIVE_ON,
    "course": COURSE_ON,
    "game": GAME_ON,
}
# 跟着桌宠「主动识屏」开关走的两句：
# 开 → 桌宠那边已经有气泡了，我们只出声（用她那句原文）
# 关 → 桌宠那边没气泡，我们既出声也冒泡
PROACTIVE_ON = "主动识屏已开启，我会偶尔看看你正在用的软件。"
PROACTIVE_OFF = "行，不看了"

# 全部要预合成的固定句子
ALL = (
    [THINKING, HEARD_NOTHING, STUCK, VOICE_OFF_HINT, MODE_TIMEOUT, PROACTIVE_ON, PROACTIVE_OFF,
     GAME_ON, GAME_OFF, LIVE_ON, COURSE_ON, MODE_BACK]
    + list(TRAY_FEEDBACK.values())
    + list(MODE_PHRASES.values())
)
