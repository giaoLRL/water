param(
    [string]$Host_ = "127.0.0.1",
    [int]$Port = 8000
)

# 启动后端（隐藏窗口），日志写入 .runtime 目录
$ErrorActionPreference = "Stop"
$py = $env:PYTHON_CMD
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) {
    $py = "C:\Users\PC\AppData\Local\Programs\Python\Python312\python.exe"
}
if (-not (Test-Path $py)) {
    Write-Error "未找到 Python，请设置环境变量 PYTHON_CMD 或安装 Python 后重试。"
}
$backend = Join-Path $PSScriptRoot "..\backend"
$runtime = Join-Path $PSScriptRoot "..\.runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "端口 $Port 已被占用，跳过启动。"
    exit 0
}

$proc = Start-Process -FilePath $py `
    -ArgumentList @("-m", "uvicorn", "main:app", "--app-dir", $backend, "--host", $Host_, "--port", "$Port") `
    -WorkingDirectory $backend `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtime "uvicorn-out.log") `
    -RedirectStandardError (Join-Path $runtime "uvicorn-err.log") `
    -PassThru

Write-Host "后端启动中 (PID $($proc.Id))，http://$($Host_):$Port"
