[CmdletBinding()]
param(
    [string]$OutputPath
)

# Read-only inspection. The only optional write is the requested JSON report.
$ErrorActionPreference = 'Stop'
$taskIssues = [System.Collections.Generic.List[string]]::new()

$taskRegistryPaths = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$taskInstallations = @(
    Get-ItemProperty -Path $taskRegistryPaths -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'Factory I/O|CODESYS' } |
        Select-Object DisplayName, DisplayVersion, InstallLocation |
        Sort-Object DisplayName, DisplayVersion, InstallLocation -Unique
)

$taskProcesses = @(
    Get-Process |
        Where-Object { $_.ProcessName -match 'Factory|CODESYS|Gateway' } |
        Select-Object ProcessName, Id, Path
)

$taskServices = @()
try {
    $taskServices = @(
        Get-CimInstance Win32_Service -Filter "Name LIKE '%CODESYS%'" |
            Select-Object Name, DisplayName, State, PathName
    )
}
catch {
    $taskIssues.Add('Service inspection failed: ' + $_.Exception.Message)
}

$taskPorts = @(502, 1217, 4840, 11740, 7410)
$taskListeners = @()
$taskListenerInspectionSucceeded = $false
try {
    $taskListeners = @(
        Get-NetTCPConnection -State Listen |
            Where-Object { $_.LocalPort -in $taskPorts } |
            ForEach-Object {
                $taskOwner = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
                [pscustomobject]@{
                    address = $_.LocalAddress
                    port = $_.LocalPort
                    processId = $_.OwningProcess
                    processName = $taskOwner.ProcessName
                }
            }
    )
    $taskListenerInspectionSucceeded = $true
}
catch {
    $taskIssues.Add('Listener inspection failed: ' + $_.Exception.Message)
}

$taskFactoryInstallation = $taskInstallations |
    Where-Object { $_.DisplayName -eq 'Factory I/O' } |
    Select-Object -First 1
$taskCodesysInstallation = $taskInstallations |
    Where-Object { $_.DisplayName -match '^CODESYS (64 )?3\.' } |
    Select-Object -First 1

$taskFactoryScenes = @()
$taskCodesysProfiles = @()
$taskScriptingPluginPresent = $false
if ($taskFactoryInstallation.InstallLocation) {
    $taskSceneDirectory = Join-Path $taskFactoryInstallation.InstallLocation 'Scenes'
    $taskFactoryScenes = @(
        Get-ChildItem -LiteralPath $taskSceneDirectory -File -Filter '*.factoryio' -ErrorAction SilentlyContinue |
            Select-Object Name, FullName, Length
    )
}
if ($taskCodesysInstallation.InstallLocation) {
    $taskCodesysDirectory = Join-Path $taskCodesysInstallation.InstallLocation 'CODESYS'
    $taskProfileDirectory = Join-Path $taskCodesysDirectory 'Profiles'
    $taskCodesysProfiles = @(
        Get-ChildItem -LiteralPath $taskProfileDirectory -File -Filter '*.profile.xml' -ErrorAction SilentlyContinue |
            Select-Object Name, FullName
    )
    $taskPluginDirectory = Join-Path $taskCodesysDirectory 'PlugIns'
    $taskScriptingPluginPresent = @(
        Get-ChildItem -LiteralPath $taskPluginDirectory -Recurse -File -Filter 'ScriptEngine.plugin.dll' -ErrorAction SilentlyContinue
    ).Count -gt 0
}

$taskShanghaiTime = [TimeZoneInfo]::ConvertTime(
    [DateTimeOffset]::UtcNow,
    [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
)
$taskSnapshot = [pscustomobject]@{
    capturedAt = $taskShanghaiTime.ToString('o')
    timezone = 'Asia/Shanghai'
    installations = $taskInstallations
    processes = $taskProcesses
    services = $taskServices
    inspectedPorts = $taskPorts
    listenerInspectionSucceeded = $taskListenerInspectionSucceeded
    listeners = $taskListeners
    factoryScenes = $taskFactoryScenes
    codesysProfiles = $taskCodesysProfiles
    scriptingPluginFilesPresent = $taskScriptingPluginPresent
    inspectionIssues = @($taskIssues.ToArray())
    notVerified = @(
        'Factory I/O license edition and enabled drivers',
        'Factory I/O Web API and SDK availability',
        'CODESYS script execution and project compilation',
        'PLC application state and Modbus I/O exchange',
        'Factory I/O and CODESYS closed-loop simulation'
    )
}
$taskJson = $taskSnapshot | ConvertTo-Json -Depth 6

if ($OutputPath) {
    $taskReportPath = [System.IO.Path]::GetFullPath($OutputPath)
    $taskReportDirectory = [System.IO.Path]::GetDirectoryName($taskReportPath)
    [System.IO.Directory]::CreateDirectory($taskReportDirectory) | Out-Null
    [System.IO.File]::WriteAllText(
        $taskReportPath,
        $taskJson,
        [System.Text.UTF8Encoding]::new($false)
    )
}

Write-Output $taskJson
