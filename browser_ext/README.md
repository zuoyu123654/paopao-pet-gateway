# 泡泡桌宠 · 播放进度上报（Tampermonkey 用户脚本）

> 2026-11-17：旧的 unpacked 扩展（`manifest.json` / `content.js` / `background.js`）已退役并删除。
> 原因是 Edge 更新/开发者模式策略会把它清掉；现在用商店扩展 **Tampermonkey** 当宿主，
> 跑 `paopao-player.user.js`，不会被“开发者模式扩展”那套清掉。

## 怎么装（不用开发者模式）

1. Edge 加载项商店搜 **Tampermonkey**（篡改猴；Violentmonkey / ScriptCat 也行），安装；
2. Edge 地址栏打开 **`http://127.0.0.1:9888/ext/paopao-player.user.js`**，
   Tampermonkey 会弹安装页，点「安装」；
3. **Edge 157 / Chromium 138+ 必须再开一个开关**：`edge://extensions` → Tampermonkey
   「详细信息」→ 打开 **「允许用户脚本 / Allow user scripts」**；不开脚本不会执行；
4. 第一次上报时 Tampermonkey 会问是否允许访问 `127.0.0.1:9888`，点允许；
5. **刷新 B站视频页**（脚本在页面加载时注入，旧页面不会自动跑）。

## 它做什么

每秒把当前视频的：

```text
url / title / currentTime / duration / paused / fullscreen / visible
```

POST 到 `http://127.0.0.1:9888/player`。只读进度，不碰页面内容，不发别处。

网关侧 `PlayerWatch` 的优先级：**扩展/用户脚本 → CDP 调试端口 → B站历史**。
所以扩展/脚本没跑时，看片窗口的 CDP 或 B站历史还能兜底。

> **B站隐藏 iframe 的坑**：B站页面里有 `message.bilibili.com/pages/nav/header_sync`
> 的跨域 iframe。用户脚本 1.0.1 只在**顶层页面**、且页面里**真有 `<video>`** 时才上报；
> 网关 `PlayerWatch.push_ext()` 也只认带有效 `currentTime` 的上报，没有时间戳的一律丢弃
> （不限 B站，YouTube/爱奇艺/腾讯/抖音的隐藏帧同样处理；直播 `duration` 为空但 `currentTime`
> 是数字，照收）。

## 验证

```powershell
(Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content |
  ConvertFrom-Json | Select-Object -ExpandProperty player
```

看到 `source: ext`、`position: 1:23`、`bvid: BV...` 就成了。

## 装不上/不通

- Tampermonkey 图标上没有脚本数字：脚本没装进 Tampermonkey，重开安装地址；
- 「允许用户脚本」没开：Edge 157 默认关，开了再刷新页面；
- 页面没有 `<video>`（首页/专栏/动态）不会上报，这是故意的；
- 只想用 CDP、不装脚本：跑 `start-edge-watch.ps1`（独立配置 + 调试端口），
  不依赖任何扩展。
