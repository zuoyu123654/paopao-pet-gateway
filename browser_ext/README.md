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

## 如果这个扩展老被 Edge 清掉：用 Tampermonkey（免开发者模式）

unpacked 扩展（上面三步装的那个）是「开发人员模式」扩展，Edge 更新/策略/配置重建可能把它清掉。
不想每次重装，就改用**商店里的脚本管理器**：

1. Edge 加载项商店搜 **Tampermonkey**（或 Violentmonkey / ScriptCat），安装；
2. 最省事：Edge 地址栏打开 **`http://127.0.0.1:9888/ext/paopao-player.user.js`**，
   Tampermonkey 会弹安装页，点「安装」；
   （也可以 Tampermonkey 面板 → 新建脚本 → 把文件内容粘进去）
3. **Edge 157 / Chromium 138+ 还要开一个开关**：`edge://extensions` → Tampermonkey
   「详细信息」→ 打开「允许用户脚本 / Allow user scripts」。不开这个开关，脚本不会执行；
4. 第一次上报时 Tampermonkey 会问是否允许访问 `127.0.0.1:9888`，点允许；
5. 播个 B站视频，看 `/status` 的 `player.source` 是不是 `ext`。

它和上面的扩展做的是同一件事：每秒把 `url/title/currentTime/duration/paused/fullscreen` 发给
`http://127.0.0.1:9888/player`，只读进度、不碰页面内容。区别只是宿主是商店扩展，
不会被「开发者模式扩展」那套清掉；脚本本身存在 Tampermonkey 里，也可以导出备份。

> **B站隐藏 iframe 的坑（2026-11-17 修）**：B站页面里有一个 `message.bilibili.com/pages/nav/header_sync`
> 的跨域 iframe，旧脚本会在里面也跑，每秒把「header sync / 0:00」覆盖到真实视频状态上。
> 现在两层都堵了：用户脚本只在**顶层页面**、且页面里**真的有 `<video>`** 时才上报；
> 网关 `PlayerWatch.push_ext()` 只认带有效 `currentTime` 的上报，没有时间戳的一律丢弃
> （不限 B站，YouTube/爱奇艺/腾讯/抖音的隐藏帧同样处理）。

## 排障

- `/status` 里 `player.connected: false`：说明一秒内没收到任何上报 → 检查扩展是否启用、页面是不是在视频站、
  有没有播起来（`currentTime` 一直是 0 也算）。
- 网关没开：扩展的 `fetch` 会静默失败，不弹错、不拖慢页面。
- 想停用：`edge://extensions` 里关掉开关，或者从扩展列表移除；网关会自动退到「调试端口 → B站历史」。
