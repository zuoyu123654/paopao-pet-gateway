# 泡泡陪伴 · 给 AI 的配置手册

> 这份**只管这个插件自己**：装插件 → 填四个配置项 → 重载 → 按命令验收。
>
> 插件之外的东西——网关（9888）怎么起、GPT-SoVITS（9882）怎么起、转写（9881）、
> OCR venv、yt-dlp、模型补丁——全在
> **`C:\Users\zuo'yu\Documents\DSH配置\dsh3\桌宠语音与网关-SETUP-FOR-AI.md`**（尤其是第 13 节）。

用户会发一句：
**「请帮我配置好 AstrBot 里的泡泡陪伴插件，并验证是否可用。」**
你照着下面做，做到最后一步全绿，就算配置完成。

---

## 0. 先搞清楚它是什么（30 秒）

陪伴能力 = **一个本地常驻核心 + 一个 AstrBot 插件（遥控器）**。

| 部分 | 是什么 | 在哪 |
|---|---|---|
| 核心 | 常驻 HTTP 服务：截屏/帧拼图、字幕 OCR、音频转写、TTS、模式状态机 | 默认 `http://127.0.0.1:9888` |
| 插件 | 只调核心的 HTTP 接口，**不含采集、不含合成** | `<AstrBot>/data/plugins/astrbot_plugin_paopao_companion/` |

**核心不跑，插件只能显示「核心没响应」。** 所以第一步永远是确认核心——而核心属于
桌宠语音那条链路，插件手册不负责装它。

---

## 1. 前置检查（一条命令）

```powershell
(Invoke-WebRequest http://127.0.0.1:9888/health -UseBasicParsing -TimeoutSec 5).Content
```

- 返回 `{"ok": true, ...}` → 核心在，往下走。
- 连不上 → **停下来**，去桌宠语音文档（第 7 节「起停与自启」/ 第 13 节「依赖」）
  把核心拉起来。

顺手看一眼体检（核心自带，缺什么一目了然）：

```powershell
(Invoke-WebRequest http://127.0.0.1:9888/doctor -UseBasicParsing -TimeoutSec 15).Content
```

`ok:false` 的项按桌宠语音文档第 13 节补。

---

## 2. 装插件

目录：`<AstrBot>/data/plugins/astrbot_plugin_paopao_companion/`，必须有：

`main.py`、`metadata.yaml`、`_conf_schema.json`、`requirements.txt`、`README.md`、`logo.png`

版本以 `metadata.yaml` 的 `version` 为准（当前 0.2.2）。

---

## 3. 配置项（WebUI → 插件 → 泡泡陪伴 → 配置）

| 键 | 默认 | 说明 |
|---|---|---|
| `gateway_url` | `http://127.0.0.1:9888` | 核心地址；改端口时这里和核心一起改 |
| `start_script` | 空 | 核心启动脚本路径（可选）。Windows 填 `.ps1` |
| `auto_start` | `false` | 插件加载时自动拉起核心（要先配 `start_script`） |
| `timeout_seconds` | `12` | HTTP 超时 |

**没有必填项**。核心在，默认就能跑。这四项就是这个插件的全部配置。

---

## 4. 让插件生效

WebUI → 插件管理 → 泡泡陪伴 → 重载；或重启 AstrBot。

日志里应出现：`[陪伴] 核心地址 http://127.0.0.1:9888（自动启动=False）`

---

## 5. 验证（一条一条过，全绿才算好）

在聊天里依次发：

| 输入 | 期望 |
|---|---|
| `陪伴` | 模式 + 今日识屏次数 + 帧缓冲 + 摘要字数 + 直播弹幕状态 |
| `陪伴 医生` | 逐项 ✅/❌，最后一行「总体：全部就绪」 |
| `陪伴 模式 anime` | 「✅ 模式切到 anime（番剧）」 |
| `陪伴 模式 game` | 「✅ 模式切到 game（游戏）」 |
| `陪伴 模式 auto` | 切回自动 |
| `陪伴 说 我在看呢` | 「🔊 已送播 N 句」，并且真的响一声 |
| `陪伴 摘要` | 给一段摘要，或「这次没摘要：记录还不够多」——都算正常 |
| `陪伴 弹幕` | 直播弹幕 开/关、房间号、连没连上、队列几条 |
| `陪伴 弹幕 开` | 「✅ 直播弹幕开了（已写回 config.json）」 |
| `陪伴 弹幕出声 开` | 「✅ 回弹幕出声开了」；再发 `陪伴 弹幕出声 关` 应只冒泡 |
| `帮我看一下我的屏幕` | 「👀 看过了（识屏计数 a → b）」——随口说就能触发，不用记命令 |

再补一条**不经过聊天**的验证（证明核心自己活着）：

```powershell
(Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing -TimeoutSec 8).Content
```

应返回 `mode`、`frames`（>0 表示帧缓冲在跑）、`precue`（>0 表示提示音缓存好了）。

**全部通过 = 配置完成。**

> 说明：`陪伴 医生` 里有一项 `tts_9883`，没分家时显示「没分家，全走 9882」——
> 这是正常的（分家方案已撤，见桌宠语音文档 4.2e）。
> `ocr_venv` / `bili_cli` / `host` 三项是**按需**的：不装 OCR、不装 bili、不开弹幕时是 ❌，
> 不影响聊天和语音；要用番剧字幕/B站字幕/直播弹幕再按文档补。

---

## 6. 排障（只说插件这一侧）

**`陪伴` 回「核心没响应」**
插件没问题，是核心没跑。去桌宠语音文档第 7 节起核心（或配好 `start_script` 后发
`陪伴 启动`），等 10 秒再试。

**`陪伴 医生` 里 TTS / ASR / OCR 是 ❌**
都不是插件的事，按桌宠语音文档第 13 节补对应服务。

**`陪伴 说 ...` 说成功了但没声音**
检查系统默认输出设备、音量，以及 TTS 是否加载了目标音色（`-c tts_infer_dafeiyu.yaml`）。

**插件装好了但聊天里没反应**
1. AstrBot 日志搜 `astrbot_plugin_paopao_companion` 有没有加载错误；
2. 群里可能被「只回 @」规则挡住，私聊最稳。

**「看看屏幕」的各种说法不触发**
触发是靠 `LOOK_RE` 正则（看/瞧/瞅/瞄/盯 + 屏幕/桌面/画面/显示器，或「屏幕上有什么」），
改说法要改 `main.py` 里那一段；`帮我看看这个报错` 这种不会误触。

**`陪伴 弹幕 开` 成功但一直「未连」**
1. `陪伴 医生` 看 `host` 和 `bili_cli`：房间号 0 又没登录过 bili 账号，找不到房间；
   在网关 `config.json → host.room_id` 填死房间号最稳；
2. 房间没开播也能连上，只是没弹幕——先自己发一条测试弹幕，或
   `POST /host/test {"user":"测试","text":"在吗"}`；
3. 只在**桌面会话**里能连（网关别做成系统服务）。

---

## 7. 不要做的事

- 不要把这个插件当采集器改——采集在核心。
- 不要把核心塞进 AstrBot 进程里（重启会连带断采集）。
- 不要用系统服务托管核心（服务拿不到桌面会话，截屏和音频环回都会失败）。
- 不要在没确认 `/health` 的情况下先怀疑插件。

---

## 8. 语音后端怎么换 / 怎么接新的（2026-11-16 加）

网关的语音**不是写死 GPT-SoVITS 的**。`D:\app1\paopao_pet_gateway\tts_backends.py`
是一层后端，现在支持五家：

| `tts.backend` | 是什么 | 需要什么 |
|---|---|---|
| `gpt-sovits` | 本机 GPT-SoVITS（当前默认，**唯一支持真流式**） | 9882 端口 + 参考音频 |
| `pet` | **跟随桌宠设置**（推荐给"已经在桌宠里配好语音"的用户） | 桌宠里配好即可 |
| `edge` | edge-tts（免 Key，音色多） | `pip install edge-tts` |
| `sapi` | Windows 本地语音（离线、毫秒级） | `pip install comtypes` |
| `mimo` | 小米 MiMo（要 Key） | 钥匙串里有 `tts/mimo` |
| `doubao` | 豆包语音 2.0（要 Key） | 钥匙串里有 `tts/doubao_api_key` |

改法：`D:\app1\paopao_pet_gateway\config.json` → `tts.backend` 填上表任一值，
重启网关。日志会打一行 `语音后端：小米 MiMo（茉莉）`，`/doctor` 里也有 `tts_backend`。

**`backend: "pet"` 怎么"搬过来用"**：网关会读桌宠自己的
`%APPDATA%\dsh-pet-standalone-webm-chat\config.json` 里的 `voice_chime_*`
（`voice_chime_tts_backend` / `voice_chime_voice` / `voice_chime_mimo_voice` /
`voice_chime_sapi_rate` …），API Key 走**同一个系统钥匙串**
（keyring service `dsh-pet-standalone`，引用名 `tts/mimo`、`tts/doubao_api_key`）。
用户不用在网关里再填一遍 Key——网关装个 `keyring` 就能直接读。

### 8.1 没有流式播怎么办

`gpt-sovits` 以外都不流式。网关会**自动把"第一小句流式"关掉**（`streaming_first and
backend.streams`），所有句子走**第二句之后那条流程**：
`feed_sentences` → 合成线程合成 wav → 播放线程按顺序播。
代价：第一句要等整句合成完（1～3 秒，看句子长度），换来的是一点不卡。
想把开口再压快：把 `max_chars_per_sentence` 调小（20～24），首句更短、更快。

### 8.2 有流式播怎么办

- **现在这条**（GPT-SoVITS）：第一小句流式 + 第二段按标点切短 + 后面整句合成，
  设计和实测都在桌宠语音文档 **4.2e**。
- **想全流式**：把 `stream_reply` 里 `feed_sentences(..., force_stream=True)` 对每一句
  都开，就是整段逐句流式（连续性好、音质掉一截，老大试过，否了）。

### 8.3 加一家新的（四步）

1. 在 `tts_backends.py` 里写一个 `_xxx(text, out_path)`，产出任意音频到 `out_path`
   （mp3/ogg 都行，外面会自动过 ffmpeg 转成 32k 单声道 wav）；
2. 在 `render()` 的分发表里登记 `"xxx": self._xxx`；在 `KNOWN` 里加上 id；
3. 密钥用 `read_secret("tts/xxx")` 读钥匙串，**别写进 config.json**；
4. 在 `config.json → tts.backend` 填 `xxx`，重启验证：
   `POST /say {"text":"测一下"}` 能出声、日志有 `整句合成好（xxx）` 就算通。

---

## 9. 一句话总结

> 核心 = 发动机（默认 `127.0.0.1:9888`，采集全在这）；插件 = 遥控器（只管开关和体检）。
> 配置成功的判据只有一条：**`陪伴 医生` 全部 ✅，且 `陪伴 说` 能出声。**
> 发动机和外部依赖怎么装，看桌宠语音与网关那份文档。
