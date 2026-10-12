[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskUsername = Read-Host 'CODESYS PLC device username (not the Windows account)'
if (-not $taskUsername) { throw 'PLC device username is required.' }
$taskPassword = Read-Host 'CODESYS PLC device password' -AsSecureString
$taskUsernameBefore = [Environment]::GetEnvironmentVariable('FACTORYIO_CODESYS_USERNAME', 'Process')
$taskPasswordBefore = [Environment]::GetEnvironmentVariable('FACTORYIO_CODESYS_PASSWORD', 'Process')
$taskCredential = New-Object System.Management.Automation.PSCredential($taskUsername, $taskPassword)
try {
    [Environment]::SetEnvironmentVariable('FACTORYIO_CODESYS_USERNAME', $taskUsername, 'Process')
    [Environment]::SetEnvironmentVariable('FACTORYIO_CODESYS_PASSWORD', $taskCredential.GetNetworkCredential().Password, 'Process')
    & (Join-Path $taskRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'probe_codesys_online.py') --credential-env
    $taskExitCode = $LASTEXITCODE
}
finally {
    [Environment]::SetEnvironmentVariable('FACTORYIO_CODESYS_USERNAME', $taskUsernameBefore, 'Process')
    [Environment]::SetEnvironmentVariable('FACTORYIO_CODESYS_PASSWORD', $taskPasswordBefore, 'Process')
    $taskCredential = $null
    $taskPassword = $null
}
exit $taskExitCode
