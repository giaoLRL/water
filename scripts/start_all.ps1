<#
一键启动全部服务：
  1. MySQL 数据库（系统服务 MySQL80，3306）
  2. YOLO 推理服务 server.py（Flask，5000）—— 根目录
  3. 后端 FastAPI main.py（8000）—— backend 目录

用法: powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
已监听的端口会自动跳过，可重复执行。
#>
$ErrorActionPreference = "Stop"

$repo   = Split-Path $PSScriptRoot -Parent          # 仓库根（shuixunhuan）
$root   = Split-Path $repo -Parent                  # 项目根（server.py 所在，YOLO 服务）
$backend = Join-Path $repo "backend"
$runtime = Join-Path $repo ".runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

# Python 解释器：优先环境变量 PYTHON_CMD，其次本项目实际使用的 3.12
$py = $env:PYTHON_CMD
if (-not $py) { $py = "D:\python\python.exe" }
if (-not (Test-Path $py)) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py -or -not (Test-Path $py)) { Write-Error "未找到 Python，请设置 PYTHON_CMD 环境变量。" }

function Test-Port([int]$port) {
    Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue -InformationLevel Quiet
}

Write-Host "== 1/3  MySQL 数据库 (3306) =="
if (Test-Port 3306) {
    Write-Host "   已运行"
} else {
    $svc = Get-Service -Name "MySQL80" -ErrorAction SilentlyContinue
    if ($svc) { Start-Service -Name "MySQL80"; Write-Host "   已启动系统服务 MySQL80" }
    else { Write-Host "   未监听且未找到 MySQL80 服务，请手动启动数据库" }
}

Write-Host "== 2/3  YOLO 推理服务 server.py (5000) =="
if (Test-Port 5000) {
    Write-Host "   已运行"
} else {
    Start-Process -FilePath $py -ArgumentList "server.py" -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtime "yolo-out.log") `
        -RedirectStandardError  (Join-Path $runtime "yolo-err.log") | Out-Null
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port 5000) { $ok = $true; break }
    }
    Write-Host $(if ($ok) { "   启动成功" } else { "   启动超时，请查看 .runtime\yolo-err.log" })
}

Write-Host "== 3/3  后端 FastAPI main.py (8000) =="
if (Test-Port 8000) {
    Write-Host "   已运行"
} else {
    Start-Process -FilePath $py -ArgumentList "main.py" -WorkingDirectory $backend `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtime "uvicorn-out.log") `
        -RedirectStandardError  (Join-Path $runtime "uvicorn-err.log") | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port 8000) { break }
    }
    Write-Host "   已启动（可通过 http://127.0.0.1:8000/docs 验证）"
}

Write-Host ""
Write-Host "================================================="
Write-Host "  系统访问:   http://127.0.0.1:8000        "
Write-Host "  接口文档:   http://127.0.0.1:8000/docs    "
Write-Host "  局域网访问: http://192.168.31.234:8000    "
Write-Host "================================================="