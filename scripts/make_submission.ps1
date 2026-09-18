<#
技能赛提交包生成脚本（一键打包到 U 盘）

按《技能赛竞赛细则》要求，在目标位置（默认 U 盘根目录）建立独立文件夹「技能赛提交文件」，
内含：全部源码与文档、数据库数据导出（CSV）、提交说明。

用法：
    powershell -ExecutionPolicy Bypass -File scripts\make_submission.ps1
    powershell -ExecutionPolicy Bypass -File scripts\make_submission.ps1 -Target E:\
    powershell -ExecutionPolicy Bypass -File scripts\make_submission.ps1 -NoData      # 不导数据

说明：
  - 只创建/覆盖文件，绝不删除目标目录里的任何内容；
  - 自动排除 .venv / .runtime / __pycache__ / .git / *.log / *.pyc（体积小、可现场重建）；
  - 后端在运行时会登录并导出 传感数据/告警/操作日志 三个 CSV（Excel 直接打开不乱码）。
#>
param(
    [string]$Target = "",
    [string]$Base = "http://127.0.0.1:8000",
    [string]$User = "admin",
    [string]$Password = "admin123",
    [switch]$NoData
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$folderName = "技能赛提交文件"

function Write-Step([string]$text) { Write-Host "== $text" }

Write-Step "技能赛提交包生成"
Write-Host "   项目目录: $repo"

# ---------- 1. 解析目标位置（默认：唯一可移动磁盘，否则桌面） ----------
if (-not $Target) {
    $removable = @()
    try {
        $removable = Get-Volume -ErrorAction SilentlyContinue |
            Where-Object { $_.DriveType -eq 'Removable' -and $_.DriveLetter } |
            Select-Object -ExpandProperty DriveLetter
    } catch { $removable = @() }
    if ($removable.Count -eq 1) {
        $Target = $removable[0] + ':\'
    } else {
        $Target = Join-Path ([Environment]::GetFolderPath("Desktop")) ""
        if (-not $Target) { $Target = $env:USERPROFILE }
        Write-Host "   （未检测到唯一 U 盘，改用桌面：$Target）"
    }
}
$dest = Join-Path $Target $folderName
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Write-Host "   提交目录: $dest"

# ---------- 2. 复制源码与文档 ----------
Write-Step "复制源码与文档（排除运行环境与缓存）"
robocopy $repo $dest /E /XD ".venv" ".runtime" "__pycache__" ".git" ".idea" ".vscode" `
    /XF "*.log" "*.pyc" /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Error "复制失败（robocopy 退出码 $LASTEXITCODE）" }
$copied = (Get-ChildItem $dest -Recurse -File | Measure-Object).Count
Write-Host "   已复制文件数: $copied"

# ---------- 3. 导出数据（后端在运行时） ----------
$dataDir = Join-Path $dest "数据导出"
$exported = @()
if (-not $NoData) {
    Write-Step "导出数据（CSV）"
    try {
        $login = Invoke-RestMethod "$Base/api/auth/login" -Method Post -ContentType "application/json" `
            -Body (@{ username = $User; password = $Password } | ConvertTo-Json) -TimeoutSec 5
        if ($login.code -ne 0) { throw "登录失败: $($login.msg)" }
        $headers = @{ Authorization = "Bearer " + $login.data.token }
        New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
        foreach ($kind in @("sensors", "alarms", "logs")) {
            $file = Join-Path $dataDir "$kind.csv"
            Invoke-WebRequest "$Base/api/water/export.csv?kind=$kind" -Headers $headers -OutFile $file -TimeoutSec 60
            $exported += $kind
        }
        Write-Host ("   已导出: " + ($exported -join ", "))
    } catch {
        Write-Host "   后端未运行或导出失败，跳过数据导出：$($_.Exception.Message)" -ForegroundColor Yellow
    }
} else {
    Write-Host "   （-NoData：跳过数据导出）"
}

# ---------- 4. 生成提交说明 ----------
Write-Step "生成提交说明"
$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$codeCount = (Get-ChildItem $dest -Recurse -File -Include *.py, *.js, *.ps1, *.html, *.css |
        Where-Object { $_.FullName -notlike "*\js\lib\*" } | Measure-Object).Count
$noteTemplate = @'
# 智能水循环监测与温控物联网系统 — 提交说明

- 赛队编号：【请填写】
- 生成时间：__NOW__
- 源文件数（不含本地库）：__CODE__

## 一、系统构成

| 层 | 实现 |
| --- | --- |
| 感知层 | ESP32 采集端（流量/双温度/压力/加热/超声波液位/光照），HTTP + JSON |
| 网络层 | 私有局域网 HTTP，后端 1Hz 轮询；支持自定义接口卡接入任意 HTTP 传感器 |
| 平台层 | Python 3.11+ FastAPI + Uvicorn，MySQL 8（PyMySQL 连接池），1Hz 入库 |
| 应用层 | Vue 3 + ECharts + gridstack 可编辑仪表盘（本地库、无外网依赖） |

## 二、启动方式

1. 启动数据库（MySQL 8，库 iot_system，账号 iot_user / iot_pass_2026）
2. 启动后端：

```powershell
cd backend
python main.py            # 或：..\.venv\Scripts\python.exe main.py
```

3. 浏览器访问 http://127.0.0.1:8000 ，默认账号 admin / admin123
4. 完整部署与故障排查见同目录 `部署指南.md`

## 三、文件说明

- `backend/`：FastAPI 后端（采集/入库/告警/联动/PID/判定服务对接/接口）
- `frontend/`：前端页面（可编辑仪表盘、历史/统计/告警/日志、系统配置）
- `tests/`：前端端到端测试（真实浏览器 + CDP）
- `scripts/`：启动脚本、本地数据库脚本、本提交包生成脚本
- `数据导出/`：传感数据、告警记录、操作日志（CSV，UTF-8 BOM，Excel 可直接打开）
- `README.md` / `AGENTS.md` / `改造记录.md` / `部署指南.md`：说明与过程文档

## 四、主要功能

1. 实时采集与展示：流量、累计水量、双水温、水压、光照、加热槽水位（储水槽无传感器标注「无传感器」）
2. 远程控制：水泵、加热、定量浇水、累计清零（二次确认 + 权限控制 + 操作留痕）
3. 历史与统计：任意时间范围曲线、区间均值/极值/用水量/采样点数、CSV 导出
4. 异常告警与联动：阈值告警入库；联动规则支持多条件（且）+ 持续 N 秒判定，动作为卡片（本机执行器或自定义指令）
5. 恒温闭环 PID、判定服务对接、多终端自适应、账号与角色权限矩阵

## 五、说明

- 本系统为**纯真实模式**：无数据一律显示 0 或 --（并标注原因），不做任何模拟数据；
- 所有控制指令均写入操作日志（含操作人/来源），可审计；
- 局域网内离线运行，不依赖任何外部 CDN 或云服务。
'@
$note = $noteTemplate.Replace("__NOW__", $now).Replace("__CODE__", "$codeCount")
Set-Content -LiteralPath (Join-Path $dest "提交说明.md") -Value $note -Encoding UTF8

# ---------- 5. 汇总 ----------
Write-Host ""
Write-Host "================ 提交包已生成 ================"
Write-Host "  目录: $dest"
Write-Host "  文件: $((Get-ChildItem $dest -Recurse -File | Measure-Object).Count) 个，$([math]::Round((Get-ChildItem $dest -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 2)) MB"
if ($exported.Count -gt 0) { Write-Host "  数据: 数据导出\ 下 $($exported.Count) 个 CSV（$($exported -join ', ')）" }
Write-Host "  提示: 提交前请把「提交说明.md」里的赛队编号填上，并核对 U 盘根目录名为「技能赛提交文件」"
Write-Host "=============================================="
