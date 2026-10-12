#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$ResetUsers)

$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell.' }
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskServiceName = 'CODESYS Control Win V3 - x64'
$taskService = Get-CimInstance Win32_Service -Filter "Name='CODESYS Control Win V3 - x64'"
if (-not $taskService -or $taskService.PathName.Trim('"') -ne 'G:\codeesys\GatewayPLC\CODESYSControlService.exe' -or $taskService.StartName -ne 'LocalSystem') {
    throw 'Runtime service differs from the inspected local installation.'
}
$taskConfig = Get-Content -LiteralPath 'G:\codeesys\GatewayPLC\CODESYSControl.cfg' -Raw
if ($taskConfig -notmatch '(?m)^Windows\.WorkingDirectory=%APPDATA%\\CODESYS\\CODESYSControlWinV3x64\\635CD56B\s*$') {
    throw 'Runtime working-directory configuration changed; inspect it before resetting.'
}
$taskExpectedRoot = Join-Path ([Environment]::GetFolderPath('Windows')) 'System32\config\systemprofile\AppData\Roaming\CODESYS\CODESYSControlWinV3x64\635CD56B'
$taskRuntimeDirectory = (Resolve-Path -LiteralPath $taskExpectedRoot).ProviderPath
if (-not [string]::Equals([IO.Path]::GetFullPath($taskRuntimeDirectory), [IO.Path]::GetFullPath($taskExpectedRoot), [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resolved runtime directory differs from the expected local service directory.'
}
$taskDirectory = Get-Item -LiteralPath $taskRuntimeDirectory -Force
if (-not $taskDirectory.PSIsContainer -or ($taskDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw 'Runtime directory must be a regular directory.'
}
$taskNames = @('.UserDatabase.csv', '.UserDatabase.csv_', '.GroupDatabase.csv', '.GroupDatabase.csv_', '.UserMgmtRightsDB.csv', '.UserMgmtRightsDB.csv_')
$taskFiles = @()
foreach ($taskName in $taskNames) {
    $taskPath = [IO.Path]::GetFullPath((Join-Path $taskRuntimeDirectory $taskName))
    if (-not [string]::Equals((Split-Path -Parent $taskPath), $taskRuntimeDirectory, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Account file escaped the intended runtime directory.'
    }
    if (Test-Path -LiteralPath $taskPath) {
        $taskFile = Get-Item -LiteralPath $taskPath -Force
        if ($taskFile.PSIsContainer -or ($taskFile.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Account file must be a regular file.'
        }
        $taskFiles += $taskFile
    }
}
$taskStamp = [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
$taskBackupDirectory = [IO.Path]::GetFullPath((Join-Path $taskRoot "artifacts\codesys-user-reset-$taskStamp"))
if (Test-Path -LiteralPath $taskBackupDirectory) { throw 'Backup destination already exists.' }
New-Item -ItemType Directory -Path $taskBackupDirectory | Out-Null
$taskReport = [ordered]@{
    captured_at = [DateTimeOffset]::UtcNow.ToString('o')
    mode = $(if ($ResetUsers) { 'reset-users' } else { 'inspect-only' })
    status = 'inspected'
    runtime_directory = $taskRuntimeDirectory
    service_name = $taskServiceName
    service_state_before = $taskService.State
    backup_directory = $taskBackupDirectory
    account_files = @($taskFiles | ForEach-Object { [ordered]@{name=$_.Name;length=$_.Length} })
    relocated_files = @()
    rolled_back_files = @()
    plc_program_deleted_by_script = $false
    project_modified_by_script = $false
    device_account_recreated = $false
}
$taskStopped = $false
$taskCanRestart = $true
try {
    if ($ResetUsers) {
        if (-not ($taskFiles | Where-Object { $_.Name -eq '.UserDatabase.csv' })) {
            throw 'Expected active .UserDatabase.csv was not found. No service or account changes were made.'
        }
        $taskController = Get-Service -Name $taskServiceName
        if ($taskController.Status -notin @('Running', 'Stopped')) { throw 'Runtime service is in a transitional state.' }
        if ($taskController.Status -eq 'Running') {
            Stop-Service -Name $taskServiceName
            $taskController.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15))
            $taskStopped = $true
        }
        $taskCopyDirectory = Join-Path $taskBackupDirectory 'account-copy'
        $taskQuarantineDirectory = Join-Path $taskBackupDirectory 'original-account-files'
        New-Item -ItemType Directory -Path $taskCopyDirectory,$taskQuarantineDirectory | Out-Null
        # Back up and hash every selected file before relocating any of them.
        foreach ($taskFile in $taskFiles) {
            $taskCopy = Join-Path $taskCopyDirectory $taskFile.Name
            Copy-Item -LiteralPath $taskFile.FullName -Destination $taskCopy
            if ((Get-FileHash -LiteralPath $taskFile.FullName -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $taskCopy -Algorithm SHA256).Hash) {
                throw 'Account backup hash mismatch; no account relocation started.'
            }
        }
        foreach ($taskFile in $taskFiles) {
            $taskDestination = [IO.Path]::GetFullPath((Join-Path $taskQuarantineDirectory $taskFile.Name))
            if (-not [string]::Equals((Split-Path -Parent $taskDestination), $taskQuarantineDirectory, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Account backup destination escaped the intended directory.'
            }
            Move-Item -LiteralPath $taskFile.FullName -Destination $taskDestination
            $taskReport.relocated_files += $taskFile.Name
        }
        $taskReport.status = 'user-management-files-relocated'
    }
}
catch {
    $taskReport.status = 'error'
    $taskReport.error = $_.Exception.Message
    # If relocation was interrupted, restore the already moved originals while stopped.
    foreach ($taskMovedName in $taskReport.relocated_files) {
        try {
            if ($taskMovedName -notin $taskNames) { throw 'Unexpected rollback filename.' }
            $taskRestorePath = [IO.Path]::GetFullPath((Join-Path $taskRuntimeDirectory $taskMovedName))
            $taskMovedPath = [IO.Path]::GetFullPath((Join-Path $taskQuarantineDirectory $taskMovedName))
            if (-not [string]::Equals((Split-Path -Parent $taskRestorePath), $taskRuntimeDirectory, [StringComparison]::OrdinalIgnoreCase) -or
                -not [string]::Equals((Split-Path -Parent $taskMovedPath), $taskQuarantineDirectory, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Rollback path escaped an intended directory.'
            }
            if (Test-Path -LiteralPath $taskRestorePath) { throw 'Rollback refuses to overwrite an existing account file.' }
            Move-Item -LiteralPath $taskMovedPath -Destination $taskRestorePath
            $taskReport.rolled_back_files += $taskMovedName
        }
        catch {
            $taskCanRestart = $false
            if (-not $taskReport.Contains('rollback_errors')) { $taskReport.rollback_errors = @() }
            $taskReport.rollback_errors += $_.Exception.Message
        }
    }
}
finally {
    if ($taskStopped -and $taskCanRestart) {
        try {
            Start-Service -Name $taskServiceName
            (Get-Service -Name $taskServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
        }
        catch {
            $taskReport.status = 'error'
            $taskReport.restart_error = $_.Exception.Message
        }
    }
    $taskReport.service_state_after = (Get-Service -Name $taskServiceName).Status.ToString()
    $taskReport | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskBackupDirectory 'report.json') -Encoding UTF8
}
$taskReport | ConvertTo-Json -Depth 5
Write-Host "Report: $(Join-Path $taskBackupDirectory 'report.json')"
if ($taskReport.status -eq 'error') { exit 2 }
if ($ResetUsers) { Write-Host 'Connect in CODESYS and create a new device Administrator account. Account creation is not performed by this script.' }
