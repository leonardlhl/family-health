param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectDirectory = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$dataDirectory = Join-Path $projectDirectory 'private'
$serverScript = Join-Path $projectDirectory 'server.py'
$baseUrl = 'http://127.0.0.1:8767'

function Get-ArchiveStatus {
    try {
        $result = Invoke-RestMethod -Uri "$baseUrl/api/bootstrap" -Method Get -TimeoutSec 2
        return @{ reachable = $true; body = $result }
    } catch {
        if ($_.Exception.Response) { return @{ reachable = $true; body = $null } }
        return @{ reachable = $false; body = $null }
    }
}

function Assert-OurArchive($status) {
    if ($status.body.app -ne 'family-health' -or $status.body.mode -ne 'local') {
        throw 'Port 8767 is occupied by another application. Close that application, then open START.cmd again.'
    }
    $expectedInbox = [System.IO.Path]::GetFullPath((Join-Path $dataDirectory 'inbox')).TrimEnd('\')
    $actualInbox = [System.IO.Path]::GetFullPath([string]$status.body.paths.inbox).TrimEnd('\')
    if ($expectedInbox -ne $actualInbox) {
        throw 'Another Family Health archive is already open on port 8767. Close it before opening this archive.'
    }
}

try {
    $initial = Get-ArchiveStatus
    if ($initial.reachable) {
        Assert-OurArchive $initial
        if (-not $NoBrowser) { Start-Process $baseUrl }
        Write-Host "Family Health is already running: $baseUrl"
        exit 0
    }

    $pythonCandidates = @()
    $resolvedPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($resolvedPython -and $resolvedPython.Source -notlike '*\WindowsApps\*') {
        $pythonCandidates += $resolvedPython.Source
    }
    $pythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
    $pythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    $pythonCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
    $pythonCandidates += 'C:\Program Files\Python313\python.exe'
    $pythonExecutable = $null
    foreach ($candidate in ($pythonCandidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            & $candidate -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonExecutable = $candidate; break }
        }
    }
    if (-not $pythonExecutable) {
        throw 'Python 3.10 or newer was not found. Install Python from python.org, then open START.cmd again.'
    }
    & $pythonExecutable -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('PIL') else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'The image library is missing. Run this command in PowerShell, then open START.cmd again:'
        Write-Host ('& "' + $pythonExecutable + '" -m pip install -r "' + (Join-Path $projectDirectory 'requirements.txt') + '"')
        exit 1
    }
    foreach ($subdirectory in @('inbox\grandpa', 'inbox\grandma', 'archive', 'backups', 'staging')) {
        New-Item -ItemType Directory -Path (Join-Path $dataDirectory $subdirectory) -Force | Out-Null
    }
    $stdoutPath = Join-Path $dataDirectory 'server.stdout.log'
    $stderrPath = Join-Path $dataDirectory 'server.stderr.log'
    $arguments = @('-X', 'utf8', ('"' + $serverScript + '"'), '--data-dir', ('"' + $dataDirectory + '"'), '--port', '8767')
    $serverProcess = Start-Process -FilePath $pythonExecutable -ArgumentList $arguments -WorkingDirectory $projectDirectory -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
    $deadline = (Get-Date).AddSeconds(20)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        $status = Get-ArchiveStatus
        if ($status.reachable) {
            Assert-OurArchive $status
            $healthy = $true
            break
        }
        $serverProcess.Refresh()
        if ($serverProcess.HasExited) {
            throw "The server could not start. See $stderrPath"
        }
        Start-Sleep -Milliseconds 350
    }
    if (-not $healthy) { throw "The server did not become ready. See $stderrPath" }
    if (-not $NoBrowser) { Start-Process $baseUrl }
    Write-Host "Family Health is ready: $baseUrl"
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
