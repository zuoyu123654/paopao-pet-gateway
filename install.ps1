# 泡泡陪伴网关 · 一键安装（给 AI 用）
# 用法：powershell -ExecutionPolicy Bypass -File .\install.ps1
#      可选 -Python "C:\Python311\python.exe"
#      可选 -EdgeShortcut  → 顺便给 Edge 桌面快捷方式加上「播放进度扩展」（方案 A）

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
New-Item -ItemType Directory -Force -Path "$here\logs", "$here\audio" | Out-Null

Write-Host "== 5/5 Edge 桌面快捷方式（B站视频模式要读播放进度）" -ForegroundColor Cyan
if (Test-Path "$here\browser_ext\manifest.json") {
    if ($EdgeShortcut) {
        & "$here\make-edge-shortcut.ps1"
    } else {
        Write-Host "  没加（默认不动你的快捷方式）。想加就跑："
        Write-Host "    powershell -ExecutionPolicy Bypass -File .\make-edge-shortcut.ps1"
        Write-Host "  加完下次从桌面图标启动 Edge 就自动带上扩展；不想动快捷方式也可以在"
        Write-Host "  edge://extensions 里手动「加载解压缩的扩展」→ 选 browser_ext 文件夹。"
    }
} else {
    Write-Host "  这个包里没有 browser_ext，跳过"
}

Write-Host ""
Write-Host "装好了。下一步：" -ForegroundColor Green
Write-Host "  1) 编辑 config.json：api_key、tts 段（后端和参考音频）、output_device 留空即可"
Write-Host "  2) 桌宠 → 设置 → 聊天 → provider 的 base_url 改成 http://127.0.0.1:9888"
Write-Host "  3) 启动：.\start.ps1（或后台：powershell -File .\ensure_running.ps1）"
Write-Host "  4) 验：Invoke-WebRequest http://127.0.0.1:9888/doctor -UseBasicParsing"
Write-Host "  5) 看视频时的进度/字幕：Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing"
