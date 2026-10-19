<#
SelfDesk Agent 一键启动
    .\start.ps1              启动依赖与主服务，并打开浏览器
    .\start.ps1 -NoBrowser   只启动，不打开浏览器
    .\start.ps1 stop         停止主服务
#>
param(
    [switch]$NoBrowser,
    [string]$Action = "start"
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null


function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", $port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Wait-Port([int]$port, [int]$seconds, [string]$name) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $port) { return $true }
        Start-Sleep -Milliseconds 800
    }
    Write-Host ("  ! " + $name + " 在 " + $seconds + " 秒内未就绪") -ForegroundColor Yellow
    return $false
}

if ($Action -eq "stop") {
    $pidFile = Join-Path $root "logs\server.pid"
    if (Test-Path $pidFile) {
        $serverPid = [int](Get-Content $pidFile -Raw).Trim()
        $proc = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
        if ($proc) { $proc | Stop-Process -Force; Write-Host ("已停止 SelfDesk Agent (PID " + $serverPid + ")") }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "没有记录到运行中的服务"
    }
    return
}

Write-Host "启动 SelfDesk Agent..." -ForegroundColor Cyan

# 0) PostgreSQL + pgvector —— 唯一数据源（业务表、知识库、工作流 checkpoint）
# WSL 重启后 PG 不会自动起。以前会静默降级到 SQLite，现在数据库是硬依赖：
# 起不来就直接报错，不允许服务带着「另一套空库」上线。
$pgReady = $false
try {
    wsl.exe -d Ubuntu -u root -- bash -lc "pg_isready -q" 2>&1 | Out-Null
    $pgReady = ($LASTEXITCODE -eq 0)
} catch { }
if (-not $pgReady) {
    Write-Host "  · 启动 PostgreSQL (WSL)..."
    wsl.exe -d Ubuntu -u root -- bash -lc "pg_ctlcluster 16 main start" 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    try {
        wsl.exe -d Ubuntu -u root -- bash -lc "pg_isready -q" 2>&1 | Out-Null
        $pgReady = ($LASTEXITCODE -eq 0)
    } catch { }
}
if (-not $pgReady) {
    Write-Host "  x PostgreSQL 未就绪，服务无法启动" -ForegroundColor Red
    Write-Host "    手动排查：wsl -d Ubuntu -u root -- bash -lc 'pg_ctlcluster 16 main start'" -ForegroundColor Yellow
    Write-Host "    连接串：`$env:SELFDESK_PG_DSN（默认 127.0.0.1:5432/selfdesk）" -ForegroundColor Yellow
    exit 1
}
Write-Host "  + PostgreSQL 就绪 (:5432, pgvector)"

# 1) 向量服务（RAG 依赖它）
if (Test-Port 8000) {
    Write-Host "  = Embedding 已在运行 (:8000)"
} else {
    Write-Host "  · 启动 Embedding (:8000)..."
    Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "embedding_server:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $root -WindowStyle Hidden | Out-Null
    if (Wait-Port 8000 120 "Embedding") { Write-Host "  + Embedding 就绪 (:8000)" }
    else {
        Write-Host "  ! Embedding 未起来，检索会不可用（回答将全部转人工）" -ForegroundColor Yellow
        Write-Host "    手动启动：pip install fastembed && python -m uvicorn embedding_server:app --port 8000" -ForegroundColor Yellow
    }
}

# 2) 主服务
if (Test-Port 8003) {
    Write-Host "  = Agent 服务已在运行 (:8003)"
} else {
    Write-Host "  · 启动 Agent 服务 (:8003)..."
    $out = Join-Path $logDir "server.log"
    $proc = Start-Process -FilePath $python `
        -ArgumentList "run.py" `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $logDir "server.pid") -Value $proc.Id -Encoding ASCII
    if (Wait-Port 8003 40 "Agent 服务") { Write-Host ("  + Agent 就绪 (:8003, PID " + $proc.Id + ")") }
    else { Write-Host ("  ! 启动失败，见 " + $out) -ForegroundColor Red; return }
}

Write-Host ""
Write-Host "  访客端   http://127.0.0.1:8003/" -ForegroundColor Green
Write-Host "  客服台   http://127.0.0.1:8003/console" -ForegroundColor Green
Write-Host "  坐席账号 agent@selfdesk.local / SelfDesk#2026"
Write-Host ""

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8003/"
    Start-Process "http://127.0.0.1:8003/console"
}
