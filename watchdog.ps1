<#
AI 编排层守护进程

为什么需要它：
Java 业务侧一旦把 AI 上游指向本服务，本服务掉线就等于客服失聪。
这个循环负责在 :8003 不可用时自动拉起，避免"业务还活着、AI 已经死了"。

停止方式：在本目录 logs\ 下创建 watchdog.stop 文件，最多 15 秒后退出。
#>
$ErrorActionPreference = 'Continue'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'
$logFile = Join-Path $logDir 'watchdog.log'
$stopFile = Join-Path $logDir 'watchdog.stop'
$pidFile = Join-Path $logDir 'watchdog.pid'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }
Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII
Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue

function Test-Port([int]$Port) {
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(1500)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        if ($client) { try { $client.Close() } catch {} }
    }
}

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

Write-Log ("守护启动 pid=" + $PID)
$restarts = 0

while (-not (Test-Path $stopFile)) {
    if (-not (Test-Port 8003)) {
        $restarts++
        Write-Log ("8003 不可用，第 " + $restarts + " 次拉起")
        try {
            Start-Process -FilePath $python -ArgumentList 'run.py' -WorkingDirectory $root -WindowStyle Hidden
        } catch {
            Write-Log ("拉起失败: " + $_.Exception.Message)
        }
        Start-Sleep -Seconds 8
    }
    Start-Sleep -Seconds 15
}

Write-Log "收到停止信号，守护退出"
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
