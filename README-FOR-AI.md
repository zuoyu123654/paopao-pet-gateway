# 泡泡陪伴网关 · 给 AI 的部署手册

> ⚠️ **先看称呼：本套默认把用户称为「老大」。**
> 如果你的人设里用户叫「主人 / 老板 / 先生 / 姐姐……」等别的称呼，请先把本套所有涉及
> 用户称呼的文本统一改成那个称呼，再往下做：`AGENT.md`、桌宠的 `default_system_prompt` / 人设、
> `phrases.py` 里的固定句、网关提示词和文档示例。**不要混用「老大」**，否则她会一会儿叫老大、
> 一会儿叫别的，或者把用户的称呼说错。下面所有写「老大」的地方都按这条替换理解。

> 这份是**安装包**的说明书。配套的施工细节（为什么这么调、踩过哪些坑）在同目录的
> `桌宠语音与网关-SETUP-FOR-AI.md`；插件那一侧看插件目录的 `README-FOR-AI.md`。

## 0. 它是干什么的

桌宠（dsh-pet-indesktop）自己是闭源的 PyInstaller，**加不了本地 TTS 后端**，但它的聊天
走 OpenAI 兼容 provider，`base_url` 可以改。这个网关就插在那个位置：

```
桌宠 ──(base_url=http://127.0.0.1:9888)──> 网关 ──> DeepSeek（或任意 OpenAI 兼容上游）
                                            ├─> TTS（GPT-SoVITS / 豆包 / edge / MiMo / SAPI）
                                            ├─> ledger.jsonl（识屏账本 + 摘要）
                                            ├─> DSH 事件（可选）→ 状态播报
                                            └─> 头顶气泡 / 语音模式 / 托盘菜单
```

拿到了模型交互的全文，音色、语速、音量、什么时候打断就全在自己手里了。

## 1. 前置（让用户自己下，AI 负责定位）

**不要让用户装我们编的东西**，这些都用官方/公共版本，AI 负责找到用户机器上的路径：

| 组件 | 从哪来 | AI 要确认 |
|---|---|---|
| 桌宠 | dsh-pet-indesktop 的发行版（PyInstaller onedir） | `%LOCALAPPDATA%\Programs\dsh-pet-standalone-webm-chat\` 存在，`%APPDATA%\dsh-pet-standalone-webm-chat\config.json` 存在 |
| Python 3.10+ | python.org 或系统自带 | `python --version` 跑得通；**别用带 `python3xx.zip` 的精简版**（venv 里会缺 `dis`） |
| TTS（三选一） | ① 本地 GPT-SoVITS（想要自定义音色）② 豆包/edge/MiMo 云 TTS ③ Windows SAPI | 见第 4 节；**没有也能先跑，网关会退回"只出文字"**；选 ① 必须打 `gsv_patches/` 两个补丁 |
| 转写 9881（可选） | 只想看视频/直播才要 | `http://127.0.0.1:9881` 活着；仓库 `tools/asr_server.py` 就是现成的 |
| OCR venv（可选） | `rapidocr_onnxruntime`，建议独立 venv | config 里 `video_watch.ocr_python` 指向它 |

## 2. 安装

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
# 如果系统里有多个 Python：-Python "C:\Python311\python.exe"
```

脚本会：建 `.venv` → 装 `requirements.txt` → 从 `config.example.json` 生成 `config.json`
→ 建 `logs/`、`audio/`、`lore/`。

> Python 用 **3.10–3.12 最省事**。3.13+ 也能跑：标准库删了 `audioop`，`requirements.txt`
> 里已经按版本自动装 `audioop-lts`。

**开机自启，二选一**（都是网关目录里的脚本，配一个启动文件夹 vbs 隐藏拉起）：

| 脚本 | 适合 | 行为 |
|---|---|---|
| `follow_pet.ps1` | 平时就在用桌宠 | 桌宠开→网关开，桌宠关→网关关；顺带每 60 秒查一次桌宠的主动识屏「当日熔断」，上游恢复了就清熔断 + 重启桌宠（日志 `logs\pet_guard.log`） |
| `watchdog.ps1` | 想让网关一直活着 | 每 5 分钟 `ensure_running.ps1`，缺谁补谁 |

`ensure_running.ps1` 起 TTS 认 `config.json → tts.guard.launch`（和网关自愈同一份），
旧写法 `mark` 也兼容。

## 3. 配置（`config.json`，**必须改的只有四类**）

| 键 | 说明 |
|---|---|
| `api_key` | DeepSeek Key。也能用 Windows 凭据（`api_key_credential_target`），写这儿最省事 |
| `vision.enabled` + `vision.profiles.qwen.api_key` | **识屏看图**：开 true 并填 Qwen（或任意 OpenAI 兼容多模态）Key；不开就走聊天模型，只有多模态模型才看得懂图 |
| `upstream_base_url` / `model` | 上游。默认 DeepSeek；换别家 OpenAI 兼容也行 |
| `tts.backend` | `gpt-sovits` / `doubao` / `edge` / `mimo` / `sapi`，见第 4 节 |
| `tts.ref_audio_path` + `tts.prompt_text` | 只有 GPT-SoVITS 要：参考音频和它说的那句原话 |
| `tts.output_device` | **留空**＝跟系统默认（换耳机自动跟着走）；填 `Realtek` 这种名字片段才钉死 |
| `video_watch.ocr_python` | 装了 OCR 才填 |
| `dsh.event_dir` | 留空＝默认 `%APPDATA%\dsh-pet-bridge`；没装 DSH 就把 `dsh.enabled` 设 false |

⚠ 这个仓库里的所有 JSON 都用 **UTF-8 无 BOM**。用 PowerShell 的 `Set-Content -Encoding utf8`
会给文件加 BOM，网关虽然读的是 `utf-8-sig`，但别的工具可能不认——尽量用 `[System.IO.File]::WriteAllText` 或 Python 写。

## 4. 语音后端（`tts.backend`）

### 4.1 GPT-SoVITS（想要自己的音色）

```jsonc
"tts": {
  "backend": "gpt-sovits",
  "api": "http://127.0.0.1:9882",
  "api_stream": "http://127.0.0.1:9882",
  "ref_audio_path": "C:\\path\\to\\ref.wav",
  "prompt_text": "参考音频里说的那句原话",
  "speed": 1.2, "speed_first": 1.0       // 第一小句正常，后面快一点
}
```

启动 GPT-SoVITS 时**必须带自己的推理配置**（`-c tts_infer_xxx.yaml`），否则会加载成默认音色。
随包的《泡泡音色包-dafeiyu》里有现成的权重 + 参考音频 + 配置 + 放置说明。

**装完 GPT-SoVITS 先打 `gsv_patches/` 里的两个补丁**（不然第一句卡、并发崩）：

```powershell
cd <GSV>
Copy-Item <网关目录>\gsv_patches\*.patch .
git -c core.autocrlf=false apply api_v2.py.patch
git -c core.autocrlf=false apply TTS.py.patch
```

细节、版本不匹配怎么办、回滚：`gsv_patches/README-FOR-AI.md`。

### 4.2 云 TTS / 系统 TTS

- `doubao`：豆包语音 2.0，读桌宠配置里的 `voice_chime_doubao_*`，Key 从 Windows 凭据 `tts/doubao_api_key`
- `edge`：`pip install edge-tts`，读 `voice_chime_voice` / `voice_chime_rate`
- `mimo`：小米 MiMo，读 `voice_chime_mimo_*`，Key 凭据 `tts/mimo`
- `sapi`：Windows 本地语音，`pip install comtypes`

这几家**没有流式**，网关会自动退成"整句合成、按顺序播"，功能一样。

## 5. 接桌宠

1. 桌宠 → 设置 → 聊天 → provider（openai 那栏）的 **base_url 改成 `http://127.0.0.1:9888`**，
   `chat_path` 保持 `/v1/chat/completions`，model 随便填（网关会用自己的 `model`）。
2. **关掉桌宠自己的聊天语音**（不然两套声音叠）：`chat_reply_voice_enabled=false`。
3. 启动：

```powershell
.\start.ps1
# 后台/开机自启：计划任务调用 .\ensure_running.ps1（登录时 + 每 5 分钟），
# 或再挂一个 watchdog.ps1 常驻
Invoke-WebRequest http://127.0.0.1:9888/doctor -UseBasicParsing
```

`/doctor` 里 `ok:false` 的就是缺的东西，一项项补。

## 6. 验证清单

```powershell
(Invoke-WebRequest http://127.0.0.1:9888/health -UseBasicParsing).Content    # 活着
(Invoke-WebRequest http://127.0.0.1:9888/doctor -UseBasicParsing).Content    # 全绿
# 让她说一句
Invoke-WebRequest http://127.0.0.1:9888/say -Method POST -Body '{"text":"测试一下"}' `
  -ContentType 'application/json; charset=utf-8'
# 让她头顶冒泡（需要 PySide6）
Invoke-WebRequest http://127.0.0.1:9888/bubble -Method POST -Body '{"text":"看得见我吗"}' `
  -ContentType 'application/json; charset=utf-8'
```

日志在 `logs\gateway.log`、`logs\bubble.log`。

## 7. 可选模块（不要一次全开）

| 模块 | 开了会怎样 | 需要 |
|---|---|---|
| 识屏账本 / 摘要 | 识屏带前情、按时间说"刚才/之前" | 无（默认开） |
| 看视频（通用） | 环回听声 + 字幕 OCR，自动进模式 | `soundcard`；OCR 可选 |
| 直播/网课转写 | 「主播说的 / 网课老师说的」从 9881 来 | 跑 `tools/asr_server.py`（见第 13 节） |
| 头顶气泡 | 我们生成的话用她那套气泡显示 | `PySide6-Essentials` |
| 语音模式 | 热键 `Ctrl+Alt+V` 说话、三句气泡、电话模式 | 麦克风；`sounddevice` |
| 托盘模式菜单 | 自动/B站视频/看番/通用/关闭 + 语音开关 | `PySide6`（跟着气泡小工） |
| 直播弹幕（叠加层） | 连 B站直播间，按弹幕回话；托盘勾「直播弹幕」「回弹幕出声」 | 直播间号或 `bili` 账号；OBS 采集见 §10 |
| DSH 状态播报 | 「开工/收工」这类状态念出来 | DSH + `dsh-pet-bridge` 事件 |
| QQ 语音 | AstrBot 里发语音 | AstrBot + GPT_SoVITS 插件（见第 19 节） |
| 作品手册（`lore.py`） | 自己查萌娘百科 + B站攻略并总结成本地手册；识屏/语音/聊到才检索一两段，不常驻 | 无（默认开；首次启动会在后台一本一本补，要联网） |

作品手册（2026-11-17 加）：
`lore/games/<作品>.md` 是自动生成的（玩法 / 操作 / 界面 / 术语 / 角色 / 时效），
不用手写。看有哪些、现在补到哪：

```powershell
& $py .\lore.py list        # 已有手册
& $py .\lore.py learn 绝区零  # 现在就去学一本
Invoke-WebRequest http://127.0.0.1:9888/lore -UseBasicParsing   # 队列/状态
```

手册是**按需检索**的：只有画面/问题里出现相关线索才递 3 段，还带「用不上就一个字别提」。
细节和踩坑见《桌宠语音与网关-SETUP-FOR-AI.md》4.2v。

## 8. 人设怎么同步

**不用抄我们的**。让 AI 读用户已有的那份人设（桌宠 `chat.default_system_prompt`、
QQ/AstrBot 人格、插件角色设定），把「称呼、自称、禁区、说话风格」对齐即可；
网关只负责把模型的话念出来，不掺人设。

## 9. 常见坑

- **BOM**：配置文件带 BOM 会让 JSON 解析失败（网关已用 `utf-8-sig` 兜底，其他工具不一定）。
- **端口占用**：9888 被占 → 网关起不来但没提示，先 `Test-NetConnection 127.0.0.1 -Port 9888`。
- **单实例**：网关自带互斥体，第二个实例会自己退出（日志 `已经有一个网关在跑了`）。
- **PySide6 没装**：气泡/托盘没有，语音不受影响。
- **麦克风**：语音模式启动会逐个试设备，`0 采样` 就是那个设备不出数据（插孔没麦/被占用）。
- **蓝牙耳机**：会被 Windows 抢默认输出；`output_device` 留空就跟默认走，想稳就钉名字片段。
- **GPT-SoVITS 首次加载** 60～90 秒，别以为挂了。
- **识屏第一句没声 / TTS 中途崩**：网关自带 `tts.guard`，每 15 秒探一次 9882，
  按 `tts.guard.launch` 里的命令把 GPT-SoVITS 拉起来（并写 `logs/.gsv-started`）；
  看 `GET /doctor` 的 `tts_guard` 一项。换 TTS 路径时记得改 `launch`。
- **`.ps1` 要带 UTF-8 BOM**：Windows PowerShell 5.1 会把无 BOM 的中文注释按 ANSI 读，
  行尾反引号会吞掉下一行，守护脚本就静默失效（`ensure_running.ps1` 踩过）。
- **跨通道认人**：`facts.jsonl` 存「老大亲口教的事」（那个人是X / 不是X是Y），
  聊天、语音、识屏共用一份，7 天过期；别把它扩成「所有聊天都记」。
- **手册生成在后台**：第一次启动/遇到新游戏时一本一本补（十几秒到一分钟一本），
  日志 `资料：给《…》做了本手册` 出现之前她确实“还不认识”。
- **改配置**不用换代码，但改完要重启网关（除非该键是每次现读的）。
- **第一句念到一半卡住 / 并发时 9882 崩**：GPT-SoVITS 没打补丁。打 `gsv_patches/` 两个，
  重启 9882（`gsv_patches/README-FOR-AI.md`）。
- **Python 3.13 报 `No module named audioop`**：新标准库删了它，`pip install audioop-lts`。
- **直播弹幕连上但没内容**：房间没开播 / `host.room_id` 还是 0 又没 `bili` 账号
  （`/doctor` 的 `host`、`bili_cli` 两项会说明）；弹幕只读不发，测试用 `POST /host/test`。
- **聊天/语音里她说「看不清」（让她解密、看报错、读代码）**：以前 chat/voice 带的是帧缓冲那张
  640 宽的图（q82），屏幕上的小字缩完就糊。现在这两条路改成**现抓一张高分辨率**：
  平时 `frame_buffer.shot_width`（默认 1280）/`shot_quality`（默认 90），
  老大这句话里有「解密/解码/看清/报错/代码/字太小」，或者只是**在请求她干活**
  （看一下、看一眼、看看、帮我、帮忙、猜猜、猜一猜、这是啥、咋回事、认一下…）时，
  用 `hi_res_width`（默认 1600）/`hi_res_quality`（默认 90）；现抓失败才退回 640 的缓冲帧。
  日志会写「聊天：补一张当前屏幕（…，1600×900 q90，334 KB）」或「语音：带上屏幕（1600宽 q90…）」。
  识屏（自动）那条路仍是 640 的拼图（看变化用的），要它读字得调 `frame_buffer.frame_width`。
- **回弹幕太短/太长**：改 `host.reply_max_chars`（默认 60，连叫的观众名一起算），重启网关生效。
  提示词里的字数和 `max_tokens` 都跟着它走；生成超了会断在句末，不会切半句。
- **聊天弹「模型请求失败」**：多半是模型把 token 全花在「思考」上、正文一个字没出来，
  桌宠那边等 60 秒就判失败（网关日志里连「首字」都没有，但请求是到了的）。
  实测：`thinking` 开着时，给它 4096 个 token 它也能全想光、正文照样 0 字；
  降到 `reasoning_effort: low` 就正常了（约 660 个思考块后开始说，首字 5～12 秒）。
  现在 config 的 `chat_thinking` 默认 `low`：`off` = 完全不思考（首字 1～2 秒，最快）、
  `low` / `medium` / `high`、`auto` = 不动、按桌宠传的来。识屏（vision）不受影响。
  验证：日志里聊天那轮会出现「首字 x.xxs（chat）」和「完成 scenario=chat」。
- **游戏模式还串台（后台挂着直播/视频）**：游戏模式下不掺后台内容——`video.context_block()`
  在 `mode=game` 时直接返回空（网页音轨/环回/OCR 转写都不进提示词），播放器进度字幕也不注入，
  回弹幕的上下文里也不带「正在看 / 主播刚说」。要她跟着直播走就切到 `live` / `video` 模式。
  验证看日志：游戏模式那几轮应该写「没有台词/字幕」，不该出现「视频模式：带上 N 条对白/字幕」。
- **“串台”的三道闸（2026-11-21 全模式过了一遍）**：
  1. 转写块 `video.context_block(mode)` 只认 `video / anime / live / course`；`game / auto / normal / off` 一律空。
  2. 播放器进度字幕只在她**正看着**那个页面在前台时才给（模式得是看片那几种，且前台窗口标题/应用
     与播放器页面对得上）；后台标签页哪怕 `visible=true` 也不算，日志会写「播放器：不在前台的页面…」。
  3. 网页音轨只收标题和「正在看的这一条」对得上的标签页（前 16 字比对），后台那个直播标签一直推音轨
     也会被丢掉，日志写「网页音轨：这个标签页不是正在看的那一个…」（每分钟最多一条）。
  另：回弹幕的上下文只在看片模式带「正在看 / 主播刚说」，游戏/auto/normal/off 下不带。
- **在 B站首页/搜索/消息中心/创作中心也会被拽进「B站视频」模式**：自动判模式以前只要标题里
  有「bilibili/哔哩哔哩」就当视频页。现在按 B站 自己的命名规律认：视频页 `document.title`
  一定带 `_哔哩哔哩_bilibili`（番剧/直播另按 URL：`/bangumi/play`、`live.bilibili`），
  只有站名（「哔哩哔哩弹幕视频网」）的页面一律**不自动进**，日志会写
  「视频类模式：这一页不是在看片（…），不进」。后台标签挂着视频也不影响：它上报的是
  `visible=false`，判模式时会被丢掉。
- **OBS 里气泡和桌宠对不齐**：用窗口采集抓 `泡泡气泡（全屏）`，别用显示器采集；
  换分辨率/缩放就调 `bubble.capture_offset_px`（默认 70，本机 2560×1440@125% 正好）。
- **`.ps1` 打不开/中文乱码**：仓库里的脚本都带 UTF-8 BOM，下载/解压时别让编辑器转码。

## 更新记录

### 2026-11-20 ～ 11-21（本机这一轮）

**直播回弹幕**
- 回复字数可配：`host.reply_max_chars`（默认 60，连叫的观众名一起算）。以前写死 30 字，
  念了名字就没空说话；生成超了会断在句末，不切半句。

**认作品（小抄 / facts）**
- `titles.json` 的说明现在由 `title_hint()` 前置《作品名》，她不用再从介绍文字里猜是哪款游戏
  （「原神被说成鸣潮」那次就是小抄里只有介绍、没给名字）。
- 补了「鸣潮 / wuthering」「蓝色星原 / 旅谣 / bluestar」三条；原神那条加了「别和这两个混」。
- facts（老大亲口纠正过的）改成**同窗口/标题优先**：只有当前 app/title 和记下来的对得上才算硬事实；
  换窗口的那几条根本不注入；没记窗口的老条目降级成「只是参考」。

**串台（后台挂着直播 / 视频）**
- 转写块 `video.context_block(mode)` 只认 `video/anime/live/course`；`game/auto/normal/off` 一律返回空。
- 播放器进度 / 字幕只在她**正看着**的那个页面在前台时才注入（后台标签即使 `visible=true` 也不算）。
- 网页音轨只收标题和「正在看的这一条」对得上的标签页。
- 回弹幕的上下文只在看片模式带「正在看 / 主播刚说」。
- 游戏模式的「【游戏参考】在玩 xxx」改为只认前台窗口，不再拿后台直播标题当游戏名。

**模式自动判定**
- B站非视频页（首页 / 搜索 / 消息中心 / 创作中心）不再被当成 B站视频：视频页认
  `_哔哩哔哩_bilibili` 后缀，番剧 / 直播认 URL；标题里带「直播」「番剧」的普通视频也不会误判。

**聊天 / 语音的截图**
- 从「帧缓冲里 640 宽的旧帧」改成**现抓高清**：平时 `shot_width` 1280 / `shot_quality` 90；
  话里带「解密 / 看清 / 报错 / 代码 / 字太小」，或「看一下 / 帮我 / 看看 / 猜猜 / 猜一猜 /
  这是啥 / 咋回事」这类请求时，用 `hi_res_width` 1600 / `hi_res_quality` 90；
  现抓失败才退回 640 缓冲帧。提示词里也加了「小字也照着读，别因为像缩略图就说看不清」。

**聊天的模型请求**
- 修「模型请求失败」：模型把 token 全花在「思考」上、正文 0 字，桌宠 60 秒就判失败。
  新增 `chat_thinking`（默认 `low`）＝聊天走 `reasoning_effort: low`，想一点但一定出正文；
  `off` 最快（1～2 秒）、`medium`/`high` 更认真但更慢（`high` 可能又想满预算）、`auto` 不动。
  实测：给再多 token 也没用——4096 它也能全想光。

**关闭模式（mode=off）**
- 关闭模式下**她自己的主动识屏一律不接**：桌宠那边照旧每两分钟发一次
  `（参考元数据…）`，网关直接回空、不叫模型、不冒泡、不记账，日志写
  「识屏：关闭模式，这一轮直接跳过（不回、不念、不记账）」。
  以前只 mute 了出声，气泡和账本照旧，所以看着像「关了还在看」。
- 切到关闭时会顺手把她那边的「主动识屏」也关掉（不再每两分钟截屏发送）；
  手动「看看屏幕」（文案是「看看这张截图…」）不受影响，照常能用。
- 重启网关**不会**再把「关闭」自动拨回 auto（以前开机看到主动识屏是开的就回 auto，
  手动关掉的关闭状态会被顶掉）。

### 2026-11-22（直播助手 + 欢迎进场）

- 托盘里的「直播弹幕」改名**直播助手**；「回弹幕出声」收进新的**直播设置**子菜单
  （里面有「回弹幕出声（Ctrl+Alt+B）」和「欢迎进场（新观众）」两项，都可单独勾）。
- 新增**欢迎进场**：有人进直播间（B站 `INTERACT_WORD` / 新版 `INTERACT_WORD_V2` 的
  `msg_type=1`；V2 的 `data.pb` 是 protobuf，代码里自己解 uname/uid/msg_type）就招呼一句。
  **怎么迎由她自己定**（不轮换、不替她选调子）：可以纯欢迎、可以顺口问一句、
  也可以介绍自己或我们此刻在干嘛，提示词只交代「自然点、别像客服」。
  配置：`host.welcome`（默认 true）、`host.welcome_min_gap`（默认 20 秒一次，
  同一个人 15 分钟内只迎一次）、`host.welcome_max_age`（默认 8 秒：只迎刚进门的，
  半分钟前的旧事件不补，没人进就安静）。出声跟「回弹幕出声」同一个开关，
  `speak=false` 只冒泡时不用等 TTS，迎得更快。
  看直播模式自己连的那条 WS（`LiveDanmaku`）也会收进场事件，但它连的是**别人**的直播间：
  只有「看的就是老大自己的直播间」才转发给欢迎，否则一律不转（日志写
  「看直播：这个房间不是老大自己的直播间（xxx），进场事件不转给欢迎」）。
  2026-11-22 的「欢迎一些不知道哪来的人」就是这条转发越界了——迎的其实是正在看的那位主播的观众。
  调试：`POST /host/test {"enter":"测试观众"}`（可选 `"style":"hi|ask|intro"` 强指定一种调子），
  日志会写「进直播间：…（排一条欢迎）」和「欢迎进场（名字｜自选/调子｜出声/只气泡）：那句话」。
- 状态接口：`GET /host` / `GET /status` 的 host 段里多了 `welcome`（开关）和 `welcome_queued`（排队数）。

**感谢关注 / 感谢加粉丝团（2026-11-22 加，和欢迎同一套）**
- 托盘「直播设置」里多了「感谢关注」「感谢加粉丝团」两项，各管各的。
- 关注：B站 `INTERACT_WORD` / `INTERACT_WORD_V2` 的 `msg_type=2`；
  粉丝团/上舰：`msg_type` 4/5/6、`GUARD_BUY`、`USER_TOAST_MSG` 都算，走同一条感谢。
- 怎么谢**由她自己决定**（提示词只说「一句谢谢，自然点，别像客服；不许许愿、不许承诺送东西/抽奖/加好友」）。
- 限频：`host.thanks_follow` / `host.thanks_fans`（默认都开）、`host.thanks_min_gap`（默认 10 秒）、
  `host.thanks_max_age`（默认 25 秒；出声时嗓子被占着，等一会儿再谢也不会丢）。
  同一个人同一类 1 小时内只谢一次。
- 调试：`POST /host/test {"thanks":"follow","user":"测试观众"}`（`"thanks":"fans"` 试粉丝团）。
  日志：「关注：名字（自己直播间，排一条感谢）」→「感谢关注（名字｜出声）：那句话」。
- 状态接口 host 段多 `thanks_follow` / `thanks_fans` / `thanks_queued`。
- 上舰**单列**（2026-11-22 老大要的，不混进粉丝团）：`GUARD_BUY`、`USER_TOAST_MSG`（`guard_level` 有值）
  → 托盘「感谢上舰（舰长/提督/总督）」，谢的时候会带上头衔（1=总督、2=提督、3=舰长）。
  `USER_TOAST_MSG` 里 `guard_level=0` 的才走「感谢粉丝团」。
- **感谢礼物**：`SEND_GIFT` → 托盘「感谢礼物」。两条规矩：
  * **价值门槛** `host.gift_min_value`（单位**电池**，默认 10；100 金瓜子 = 1 电池）：
    低于门槛的礼物不谢（日志写「礼物：xx 的 yy 价值 N 电池，低于门槛 10，不谢」），
    银瓜子免费礼物算 0 电池，一律不谢。
  * **合并**：同一个人 `host.gift_merge_seconds`（默认 15 秒）内连着送，合成一条谢
    （例如「辣条×3」，不是一句一个）；停手 15 秒才谢。
  * **上限** `host.gift_max_per_minute`（默认 6 条/分钟），超了就丢掉这批（日志写明）。
  实测：小心心 1 电池被门槛挡住；辣条 1 个 + 2 个合并成「辣条×3」一句谢。
  门槛在托盘里就能改：直播设置 →「礼物门槛（电池）」子菜单（不限 / 1 / 5 / 10 / 30 / 50 / 100，
  默认 10 电池≈1 元），选完立刻生效并写回 config；也可以 `POST /host {"gift_min_value":30}`。
  状态接口 `GET /host` 里能看到当前 `gift_min_value`、`gift_merge_seconds`、`gift_max_per_minute`。

**醒目留言（SC）/ 优先回 / 敏感词（2026-11-22 加）**
- 醒目留言单列：`SUPER_CHAT_MESSAGE` → 托盘「醒目留言（SC）」开关（`host.thanks_sc`）。
  谢的时候会**先把 SC 里说的话接一句**再道谢（`detail` 里带着「价格元｜内容」），
  别只谢不打内容。SC 在感谢队列里排最前（sc > 上舰 > 礼物 > 关注 > 粉丝团）。
- 带她名字 / 外号 / @ 她的弹幕**优先回**：评分 +40（以前只 +6），基本下一轮就回它。
  外号认 CALL_NAMES：泡泡、鲸鱼、鲸鱼娘、鲸鲸、肥鱼、大肥鱼、鱼娘（观众不一定喊「泡泡」）。
  日志会写「直播弹幕收到（点名·鲸鲸）：…」。队列满了也会把分数最低的挤掉先回它。
- 敏感词一直在（没有开关）：默认挡广告引流（加微/微信/QQ群/代练/刷粉/扫码/外链…）
  和粗口（傻逼/操你/尼玛…），`host.block_words` 可以追加自己的词；命中就整条不念不回，
  日志写「直播弹幕：xx 那条有过滤词（微信），跳过」（30 秒最多记一条）。
- 托盘**单实例**：`bubble_host.py` 启动时看 `logs/bubble_host.pid`，已有活着的就自己退出，
  不会再出现两个托盘图标（手动启动和自启脚本撞车时也不会）。
