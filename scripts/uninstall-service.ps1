[CmdletBinding()]
param(
    [string]$ServiceName = "IBTrader",
    [string]$NssmPath = "D:\tools\nssm\win64\nssm.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $NssmPath)) {
    throw "NSSM does not exist: $NssmPath"
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    Write-Output "$ServiceName is not installed"
    exit 0
}

if ($service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}

& $NssmPath remove $ServiceName confirm
if ($LASTEXITCODE -ne 0) {
    throw "Failed to remove $ServiceName"
}
Write-Output "$ServiceName removed"
