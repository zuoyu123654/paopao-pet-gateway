# 泡泡桌宠 · 浏览器扩展（播放进度上报）

一个**只读进度**的小扩展：每秒把当前网页视频的 `地址 / 标题 / 当前秒 / 总长 / 暂停 / 全屏` POST 给本机网关
`http://127.0.0.1:9888/player`。桌宠拿到之后就能按时间轴切出「刚才那 90 秒的字幕」，知道你在看哪一段。

- 不发页面内容、不注入脚本到页面 DOM、不改视频行为；
- 只在视频站生效（bilibili / YouTube / 爱奇艺 / 优酷 / 芒果 / 腾讯视频 / 抖音）；
- 目标只有 `127.0.0.1:9888`，不联网到别处。

## 怎么装（三下，不用重启浏览器）

1. 地址栏打开 `edge://extensions`（Chrome 是 `chrome://extensions`）
2. 右上角打开「开发人员模式」
3. 点「加载解压缩的扩展」，选这个文件夹：`D:\app1\paopao_pet_gateway\browser_ext`

装完会在扩展列表里看到「泡泡桌宠 · 播放进度上报」。打开任意 B站视频播几秒，然后看：

```powershell
(Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content |
  ConvertFrom-Json | Select-Object -ExpandProperty player
```

看到 `source: ext`、`position: 1:23` 就成了。

## 或者：靠快捷方式自动加载

`--load-extension=D:\app1\paopao_pet_gateway\browser_ext` 加进 Edge 快捷方式的「目标」里，
下次启动 Edge 就自动带上（本机任务栏那个 Edge 快捷方式已经加好了）。
缺点是每次启动可能顶一条「正在使用开发者模式扩展」的提示。

## 排障

- `/status` 里 `player.connected: false`：说明一秒内没收到任何上报 → 检查扩展是否启用、页面是不是在视频站、
  有没有播起来（`currentTime` 一直是 0 也算）。
- 网关没开：扩展的 `fetch` 会静默失败，不弹错、不拖慢页面。
- 想停用：`edge://extensions` 里关掉开关，或者从扩展列表移除；网关会自动退到「调试端口 → B站历史」。
