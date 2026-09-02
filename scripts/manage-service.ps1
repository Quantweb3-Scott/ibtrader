[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action,
    [string]$ServiceName = "IBTrader",
    [string]$NssmPath = "D:\tools\nssm\win64\nssm.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    throw "$ServiceName is not installed. Run scripts\install-service.ps1 as Administrator."
}

& $NssmPath $Action $ServiceName
if ($LASTEXITCODE -ne 0) {
    throw "NSSM $Action failed for $ServiceName"
}

Get-Service -Name $ServiceName | Select-Object Name, Status, StartType
