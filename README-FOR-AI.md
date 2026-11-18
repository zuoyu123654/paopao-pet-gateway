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
| TTS（三选一） | ① 本地 GPT-SoVITS（想要自定义音色）② 豆包/edge/MiMo 云 TTS ③ Windows SAPI | 见第 4 节；**没有也能先跑，网关会退回"只出文字"** |
| 转写 9881（可选） | 只想看视频/直播才要 | `http://127.0.0.1:9881` 活着 |
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
| `follow_pet.ps1` | 平时就在用桌宠 | 桌宠开→网关开，桌宠关→网关关 |
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
- **Python 3.13 报 `No module named audioop`**：新标准库删了它，`pip install audioop-lts`。
- **直播弹幕连上但没内容**：房间没开播 / `host.room_id` 还是 0 又没 `bili` 账号
  （`/doctor` 的 `host`、`bili_cli` 两项会说明）；弹幕只读不发，测试用 `POST /host/test`。
- **OBS 里气泡和桌宠对不齐**：用窗口采集抓 `泡泡气泡（全屏）`，别用显示器采集；
  换分辨率/缩放后要微调 `bubble_host.py` 的捕获偏移（本机 2560×1440@125% 是 70px）。
- **`.ps1` 打不开/中文乱码**：仓库里的脚本都带 UTF-8 BOM，下载/解压时别让编辑器转码。
