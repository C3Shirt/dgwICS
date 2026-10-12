#Requires -RunAsAdministrator
[CmdletBinding()]
param([string]$OutputPath)

# Run from an elevated PowerShell. Does not download/start an IEC application,
# change service startup types, credentials, user accounts or security policy.
$ErrorActionPreference = 'Stop'
$taskProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputPath) {
    $taskStamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
    $OutputPath = Join-Path $taskProjectRoot "artifacts\codesys-official-api-001\runtime-start-elevated-$taskStamp.json"
}
$taskReportPath = [IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $taskReportPath) {
    throw 'Report exists; provide a fresh OutputPath before starting services.'
}
$taskRuntimeName = 'CODESYS Control Win V3 - x64'
$taskRuntime = Get-CimInstance Win32_Service -Filter "Name='CODESYS Control Win V3 - x64'"
if (-not $taskRuntime -or $taskRuntime.PathName.Trim('"') -ne 'G:\codeesys\GatewayPLC\CODESYSControlService.exe') {
    throw 'Runtime service does not match the inspected local installation.'
}
$taskBefore = $taskRuntime.State
$taskReport = [ordered]@{
    schema_version = 1
    captured_at = [DateTimeOffset]::UtcNow.ToString('o')
    service_name = $taskRuntimeName
    service_state_before = $taskBefore
    service_running = $false
    plc_program_started_by_script = $false
    plc_application_running_verified = $false
    download_requested = $false
    script_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
try {
    $taskGateway = Get-Service -Name 'CODESYS Gateway V3'
    if ($taskGateway.Status -ne 'Running') {
        Start-Service -Name 'CODESYS Gateway V3'
        $taskGateway.WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
    }
    $taskController = Get-Service -Name $taskRuntimeName
    if ($taskController.Status -ne 'Running') {
        Start-Service -Name $taskRuntimeName
        $taskController.WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
    }
    $taskRuntime = Get-CimInstance Win32_Service -Filter "Name='CODESYS Control Win V3 - x64'"
    $taskReport.service_running = $taskRuntime.State -eq 'Running'
    $taskReport.service_state_after = $taskRuntime.State
    $taskReport.process_id = $taskRuntime.ProcessId
    $taskReport.status = 'ok'
}
catch {
    $taskReport.status = 'error'
    $taskReport.error = $_.Exception.Message
}
$taskReportDirectory = Split-Path -Parent $taskReportPath
New-Item -ItemType Directory -Path $taskReportDirectory -Force | Out-Null
$taskReport | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $taskReportPath -Encoding UTF8
$taskReport | ConvertTo-Json -Depth 4
Write-Host "Report: $taskReportPath"
if (-not $taskReport.service_running) { exit 2 }
