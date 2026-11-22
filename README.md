# 泡泡陪伴网关

> 她和老大之间的那条通道。
> 给闭源的桌面桌宠「泡泡」装上自己的声音、眼睛和记性：本地跑，`127.0.0.1:9888`，OpenAI 兼容。

```
桌宠 ──(base_url=http://127.0.0.1:9888)──> 泡泡陪伴网关 ──> DeepSeek（或任意 OpenAI 兼容上游）
                                            ├─> TTS（GPT-SoVITS / 豆包 / edge / MiMo / SAPI）
                                            ├─> ledger.jsonl（识屏账本 + 摘要）
                                            ├─> 作品手册 lore/（自己查资料、自己总结）
                                            ├─> 看片：网页音轨 / 环回 / B站字幕 / OCR
                                            ├─> DSH 事件（可选）→ 状态播报
                                            ├─> 直播助手 → 弹幕回复 + 欢迎进场（气泡 / 可选出声）
                                            └─> 头顶气泡 / 托盘模式菜单 / 语音模式
```

桌宠本身是闭源打包的，加不了本地 TTS；但它的聊天走 OpenAI 兼容 provider，`base_url` 可改。
把地址指到这里，就拿到了每一次模型交互的全文——音色、语速、音量、什么时候开口全在自己手里。

## 能做什么

- **语音**：聊天、识屏、语音问答都用自己练的音色念；**先来后到**——上一段念完才念下一段，不打断、不插队。
  首句走流式（开口 1.0～1.5 秒），后面的句子并发合成，句间空档 0～0.8 秒。
- **识屏**：帧缓冲 + 拼图 + 前情账本，接得住「刚才那个」「这个角色是谁」。
  聊天/语音带的那张是**现抓的高清图**（默认 1280 宽 q90；说「解密/看清/报错」或
  「看一下/帮我/看看/猜猜/猜一猜」这种请求时 1600 宽 q90），不再用 640 宽的缓冲帧，
  屏幕上的小字也读得出来。
- **五档 + 游戏**：`auto` / `normal` / `video` / `anime` / `live` / `course` / `game` / `off`，
  切换有气泡有声，托盘也能点。
- **看视频/番剧/直播/网课**：字幕来源按 **B站 CC → 网页音轨 → 环回转写 → 字幕 OCR** 排，
  按来源标注「剧情内容参考」「主播说的」「网课老师说的」。网页音轨是从播放器里直接接出来的，
  **蓝牙耳机没有环回也能用**；日语番剧、英语片都能转（不丢），快进/换集会自动清旧台词。
- **直播助手**：连 B站直播间，问句/@/SC 优先，按简称回；托盘「直播设置」里勾「回弹幕出声」才念，
  热键 `Ctrl+Alt+B`。一条回多少字在 `config.json` 的 `host.reply_max_chars`
  （默认 60，连叫的观众名一起算；超了会断在句末）。
- **欢迎进场**：有人进直播间就招呼一句——纯欢迎、顺口问一句、或者介绍一下自己/我们正在干嘛，
  三种轮换。托盘「直播设置 → 欢迎进场（新观众）」勾选，`host.welcome`（默认开）、
  `host.welcome_min_gap`（默认 45 秒一次，同一个人 15 分钟内只迎一次）。
- **头顶气泡**：用桌宠那套气泡显示（默认挂 10 秒）；开 `bubble.capture_window` 会多一个全屏透明采集窗
  **`泡泡气泡（全屏）`**，给 OBS/直播姬做窗口采集。
- **作品手册**：遇到新游戏/番剧自己查萌娘百科 + B站攻略，总结成 `lore/` 手册，按需检索一两段。
- **语音模式**：`Ctrl+Alt+V` 按住说话 / 电话模式；`bubble/` 是搬来的气泡组件（MIT）。

## 快速开始

```powershell
git clone https://github.com/zuoyu123654/paopao-pet-gateway.git
cd paopao-pet-gateway
powershell -ExecutionPolicy Bypass -File .\install.ps1     # 建 .venv、装依赖、生成 config.json
# 改 config.json：api_key、tts 段（后端 + 参考音频）
.\start.ps1
Invoke-WebRequest http://127.0.0.1:9888/doctor -UseBasicParsing
```

然后把桌宠的 provider `base_url` 改成 `http://127.0.0.1:9888`，并关掉桌宠自带的聊天语音。

用本地 GPT-SoVITS 的话，**先打 `gsv_patches/` 里两个补丁**（不打会第一句卡、并发崩），
步骤见 [`gsv_patches/README-FOR-AI.md`](gsv_patches/README-FOR-AI.md)。

**音色（她那把嗓子）**：训练好的 GPT-SoVITS v2Pro 权重不放在仓库里（单个文件 155MB / 135MB，
超过 GitHub 仓库 100MB 的单文件上限），挂在 Release：

```text
https://github.com/zuoyu123654/paopao-pet-gateway/releases/tag/voice-v1
```

下载 5 个附件 → 按里面的 `VOICE-README-FOR-AI.md` 放进 GPT-SoVITS 目录 → 改
`config.json → tts.ref_audio_path / prompt_text`（参考文本逐字是
「今天2月11日，路过了一整条街的黑屏幕」）→ 启动时**必须带 `-c ...tts_infer_dafeiyu.yaml`**。
自己练了新音色也一样：权重进 Release，仓库里只留说明。

**浏览器那边**（看片字幕要用）：装 Tampermonkey，然后打开
`http://127.0.0.1:9888/ext/paopao-player.user.js` 安装。
脚本头里有 `@updateURL`，从这条地址装过一次之后会自动跟着仓库版本更新。
不装也能用——只是看片时拿不到网页音轨，退回环回/OCR。

**开机自启，二选一**（配个启动文件夹 vbs 隐藏拉起）：

| 脚本 | 行为 |
|---|---|
| `follow_pet.ps1` | 桌宠开→网关开，桌宠关→网关关 |
| `watchdog.ps1` | 每 5 分钟 `ensure_running.ps1`，缺谁补谁 |

## 更新

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1
```

它做三件事：`git pull` → `pip install -r requirements.txt`（只补缺的）→ 提示重启网关。
`config.json` 和 `logs/`、`audio/`、`ledger.jsonl`、`lore/` 都在 `.gitignore` 里，**不会被覆盖**。

## 最近更新（2026-11-19）

- **网页音轨**：用户脚本 1.2.2 把 `<video>` 的音轨 8 秒一段推到 `/tabaudio`，蓝牙耳机也能转写字幕；
  有 B站 CC 时自动停掉（省钱省 CPU），静音段不送。
- **日语/英语不再被丢**：判据从「像不像中文」改成「像不像话」（汉字+假名+拉丁），
  并加了 whisper 幻觉黑名单；番剧模式按 `auto` 转，不会被硬认成中文。
- **语音首句**：撤回 10 字硬切，改成流式 + 按逗号切到最长 22 字；首句流式期间整句合成额度为 0
  （这是「第一句不卡」的关键，别改回去）。
- **气泡**：默认 3.2 秒 → **10 秒**。
- **弹幕当参考**：看直播时拿当前直播间的弹幕、看番/视频时按进度拿弹幕，各取 **8 条**塞进识屏，
  只当氛围、不逐条念也不回（和 `host` 那套「回弹幕」是两回事）。

## 目录

| 路径 | 说明 |
|---|---|
| `gateway.py` | 主程序（HTTP 服务 + 转发 + 朗读流水线 + 识屏账本 + 模式机） |
| `tts_backends.py` | 语音后端：GPT-SoVITS / 豆包 / edge / MiMo / SAPI |
| `host_mode.py` | 直播弹幕：B站 WebSocket + 队列筛选 + 回复 |
| `player_watch.py` | 播放进度 / B站 CC 字幕 / `<video>` 矩形 |
| `video_watch.py` | 看片模式：网页音轨、环回转写、字幕 OCR、模式进出 |
| `voice_mode.py` | 语音模式（热键开录 + VAD 断句 + 转写） |
| `lore.py` | 作品手册（自动检索 + 总结 + 按需注入） |
| `bubble_host.py` / `bubble_bridge.py` | 头顶气泡小工（独立 Qt 进程）+ 网关侧桥 |
| `bubble/` | 从 [dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) 搬来的气泡组件（MIT，见 `bubble/README.md`） |
| `browser_ext/` | Tampermonkey 用户脚本（播放进度 + 网页音轨） |
| `phrases.py` | 固定文案（预合成用） |
| `ocr_worker.py` | 字幕 OCR 小工（RapidOCR） |
| `install.ps1` / `start.ps1` / `ensure_running.ps1` / `watchdog.ps1` / `follow_pet.ps1` | 安装、启动、自启 |
| `gsv_patches/` | GPT-SoVITS 必须打的两个补丁（第一句卡、并发崩） |
| `tools/asr_server.py` | 9881 本地转写服务（OpenAI 兼容，faster-whisper / funasr 二选一） |
| `config.example.json` | 配置模板 |
| `docs/` | 施工单（含为什么这么调、踩过哪些坑） |

详细部署手册：[`README-FOR-AI.md`](README-FOR-AI.md)（给 AI 看的，一步步带）。
想看「这一轮到底改了什么、怎么复现」，直接翻
[`docs/桌宠语音与网关-SETUP-FOR-AI.md`](docs/桌宠语音与网关-SETUP-FOR-AI.md) 的 **§0.1 一页复现清单**。

## 配套

- 桌宠：[dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（MIT）
- AstrBot 控制台插件：[astrbot_plugin_paopao_companion](https://github.com/zuoyu123654/astrbot_plugin_paopao_companion)
- 音色：自己练的 GPT-SoVITS v2Pro 权重（不随仓库分发）

## LICENSE

MIT（`bubble/` 内的上游代码同样 MIT，版权归 MerZlin）。
