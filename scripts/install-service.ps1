[CmdletBinding()]
param(
    [string]$ServiceName = "IBTrader",
    [string]$NssmPath = "D:\tools\nssm\win64\nssm.exe",
    [string]$UvPath = "H:\ProgramData\anaconda3\Scripts\uv.exe",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot "config.yaml"
$logDirectory = Join-Path $projectRoot "logs"
$stdoutPath = Join-Path $logDirectory "service.stdout.log"
$stderrPath = Join-Path $logDirectory "service.stderr.log"

foreach ($requiredPath in @($NssmPath, $UvPath, $configPath, (Join-Path $projectRoot "uv.lock"))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required path does not exist: $requiredPath"
    }
}
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $NssmPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "NSSM command failed ($LASTEXITCODE): $($Arguments -join ' ')"
    }
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}

if (-not $service) {
    Invoke-Nssm install $ServiceName $UvPath
}

Invoke-Nssm set $ServiceName Application $UvPath
Invoke-Nssm set $ServiceName AppDirectory $projectRoot
Invoke-Nssm set $ServiceName AppParameters "run --directory `"$projectRoot`" --frozen ibtrader"
Invoke-Nssm set $ServiceName AppEnvironmentExtra "IBTRADER_CONFIG=$configPath" "PYTHONUNBUFFERED=1"
Invoke-Nssm set $ServiceName DisplayName "IBTrader (IB Gateway overnight trader)"
Invoke-Nssm set $ServiceName Description "IBTrader FastAPI dashboard and IB Gateway trading engine on port 8089"
Invoke-Nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START
Invoke-Nssm set $ServiceName AppExit Default Restart
Invoke-Nssm set $ServiceName AppRestartDelay 5000
Invoke-Nssm set $ServiceName AppThrottle 5000
Invoke-Nssm set $ServiceName AppNoConsole 1
Invoke-Nssm set $ServiceName AppStdout $stdoutPath
Invoke-Nssm set $ServiceName AppStderr $stderrPath
Invoke-Nssm set $ServiceName AppRotateFiles 1
Invoke-Nssm set $ServiceName AppRotateOnline 1
Invoke-Nssm set $ServiceName AppRotateSeconds 86400
Invoke-Nssm set $ServiceName AppRotateBytes 10485760

if (-not $NoStart) {
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
}

Get-Service -Name $ServiceName | Select-Object Name, DisplayName, Status, StartType
