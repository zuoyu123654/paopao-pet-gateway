# 泡泡陪伴网关 · 一键安装（给 AI 用）
# 用法：powershell -ExecutionPolicy Bypass -File .\install.ps1
#      可选 -Python "C:\Python311\python.exe"
#      可选 -EdgeShortcut  → 清理 Edge 快捷方式（桌面/任务栏/开始菜单）里旧的 --load-extension 参数（旧扩展已退役）

param(
    [string]$Python = "python",
    [switch]$EdgeShortcut
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "== 1/5 找 Python" -ForegroundColor Cyan
try {
    $ver = & $Python --version 2>&1
} catch {
    Write-Host "找不到 Python（$Python）。先装 Python 3.10+ 并勾选 Add to PATH，或者用 -Python 指定完整路径。" -ForegroundColor Red
    exit 1
}
Write-Host "  $ver"

Write-Host "== 2/5 建虚拟环境 .venv" -ForegroundColor Cyan
if (-not (Test-Path "$here\.venv")) {
    & $Python -m venv "$here\.venv"
}
$venvPy = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "虚拟环境没建出来，检查一下 Python 是不是精简版（不要用带 python3xx.zip 的那种）。" -ForegroundColor Red
    exit 1
}

Write-Host "== 3/5 装依赖（几分钟，PySide6 比较大）" -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -r "$here\requirements.txt"

Write-Host "== 4/5 生成配置" -ForegroundColor Cyan
if (-not (Test-Path "$here\config.json")) {
    Copy-Item "$here\config.example.json" "$here\config.json"
    Write-Host "  已生成 config.json（这是模板，必须改：api_key / 语音后端 / 参考音频）"
} else {
    Write-Host "  config.json 已存在，没动它"
}
New-Item -ItemType Directory -Force -Path "$here\logs", "$here\audio", "$here\lore" | Out-Null

Write-Host "== 5/5 浏览器播放进度（可选：Tampermonkey 用户脚本）" -ForegroundColor Cyan
Write-Host "  旧 unpacked 扩展已退役，不再需要开发者模式。"
Write-Host "  装 Tampermonkey（Edge 加载项商店搜索），然后打开："
Write-Host "    http://127.0.0.1:9888/ext/paopao-player.user.js"
Write-Host "  再到 edge://extensions → Tampermonkey「详细信息」→ 打开「允许用户脚本」。"
if ($EdgeShortcut) {
    # 兼容旧参数：只清理 Edge 快捷方式里旧的 --load-extension（桌面/任务栏/开始菜单）
    & "$here\make-edge-shortcut.ps1"
}

Write-Host ""
Write-Host "装好了。下一步：" -ForegroundColor Green
Write-Host "  1) 编辑 config.json：api_key、tts 段（后端和参考音频）、output_device 留空即可"
Write-Host "  2) 桌宠 → 设置 → 聊天 → provider 的 base_url 改成 http://127.0.0.1:9888"
Write-Host "  3) 启动：.\start.ps1（或后台：powershell -File .\ensure_running.ps1）"
Write-Host "  4) 验：Invoke-WebRequest http://127.0.0.1:9888/doctor -UseBasicParsing"
Write-Host "  5) 看视频时的进度/字幕：Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing"
