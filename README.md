# 泡泡陪伴网关

桌宠 / 桌娘的本地陪伴网关：**OpenAI 兼容代理 + TTS + 识屏账本 + 头顶气泡 + 语音模式**。

```
桌宠 ──(base_url=http://127.0.0.1:9888)──> 网关 ──> DeepSeek（或任意 OpenAI 兼容上游）
                                            ├─> TTS（GPT-SoVITS / 豆包 / edge / MiMo / SAPI）
                                            ├─> ledger.jsonl（识屏账本 + 摘要）
                                            ├─> DSH 事件（可选）→ 状态播报
                                            └─> 头顶气泡 / 托盘模式菜单 / 语音模式
```

桌宠本身是闭源打包的，加不了本地 TTS；但它的聊天走 OpenAI 兼容 provider，`base_url` 可改。
把地址指到这里，就拿到了每一次模型交互的全文——音色、语速、音量、什么时候打断全在自己手里。

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

## 更新

```powershell
powershell -ExecutionPolicy Bypass -File .\update.ps1
```

它做三件事：`git pull` → `pip install -r requirements.txt`（只补缺的）→ 提示重启网关。
`config.json` 和 `logs/`、`audio/`、`ledger.jsonl` 都在 `.gitignore` 里，**不会被覆盖**。

## 目录

| 路径 | 说明 |
|---|---|
| `gateway.py` | 主程序（HTTP 服务 + 转发 + 朗读流水线 + 识屏账本） |
| `tts_backends.py` | 语音后端：GPT-SoVITS / 豆包 / edge / MiMo / SAPI |
| `video_watch.py` | 通用视频模式（前台播放器 + 环回有声 + 键鼠低活跃） |
| `voice_mode.py` | 语音模式（热键开录 + VAD 断句 + 转写） |
| `bubble_host.py` / `bubble_bridge.py` | 头顶气泡小工（独立 Qt 进程）+ 网关侧桥 |
| `bubble/` | 从 [dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) 搬来的气泡组件（MIT，见 `bubble/README.md`） |
| `phrases.py` | 固定文案（预合成用） |
| `ocr_worker.py` | 字幕 OCR 小工（RapidOCR） |
| `config.example.json` | 配置模板 |
| `docs/` | 施工单（含为什么这么调、踩过哪些坑） |

详细部署手册：[`README-FOR-AI.md`](README-FOR-AI.md)（给 AI 看的，一步步带）。

## 配套

- 桌宠：[dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)（MIT）
- AstrBot 控制台插件：[astrbot_plugin_paopao_companion](https://github.com/zuoyu123654/astrbot_plugin_paopao_companion)
- 音色：自己练的 GPT-SoVITS v2Pro 权重（不随仓库分发）

## LICENSE

MIT（`bubble/` 内的上游代码同样 MIT，版权归 MerZlin）。
