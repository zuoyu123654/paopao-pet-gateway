# 泡泡桌宠：装浏览器扩展（方案 A）的小助手
# 打开扩展管理页 + 把扩展文件夹弹出来，剩下点三下就好。
$ErrorActionPreference = 'SilentlyContinue'
$ext = 'D:\app1\paopao_pet_gateway\browser_ext'
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path -LiteralPath $edge)) { $edge = 'C:\Program Files\Microsoft\Edge\Application\msedge.exe' }

Start-Process -FilePath explorer.exe -ArgumentList $ext
if (Test-Path -LiteralPath $edge) {
    Start-Process -FilePath $edge -ArgumentList 'edge://extensions'
}
Write-Host ''
Write-Host '接下来三步：'
Write-Host '  1) 扩展页右上角打开「开发人员模式」'
Write-Host '  2) 点「加载解压缩的扩展」'
Write-Host "  3) 选这个文件夹：$ext"
Write-Host ''
Write-Host '装完播个 B站视频，然后看：'
Write-Host '  (Invoke-WebRequest http://127.0.0.1:9888/status -UseBasicParsing).Content | ConvertFrom-Json | % player'
