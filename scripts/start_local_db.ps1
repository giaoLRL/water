param(
    [int]$Port = 3306,
    [string]$RootPassword = "root_local_2026"
)

# 本地开发用：启动项目目录内的 MariaDB 便携实例（MySQL 协议兼容）
$ErrorActionPreference = "Stop"
$base = Join-Path $PSScriptRoot "..\.runtime\mariadb-11.4.5-winx64"
$data = Join-Path $base "data"
$exe = Join-Path $base "bin\mariadbd.exe"

if (-not (Test-Path $exe)) {
    Write-Error "未找到 MariaDB：$exe。请先运行 scripts\setup_local_db.ps1 下载初始化。"
}

$listener = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue -InformationLevel Quiet
if ($listener) {
    Write-Host "端口 $Port 已存在监听，跳过启动。"
    exit 0
}

$proc = Start-Process -FilePath $exe `
    -ArgumentList @("--datadir=$data", "--port=$Port", "--bind-address=127.0.0.1", "--skip-name-resolve") `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $base "mysql-out.log") `
    -RedirectStandardError (Join-Path $base "mysql-err.log") `
    -PassThru

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -WarningAction SilentlyContinue -InformationLevel Quiet) {
        Write-Host "MariaDB 已启动 (PID $($proc.Id), 端口 $Port)。"
        exit 0
    }
}
Write-Error "MariaDB 启动超时，请检查 mysql-err.log。"
