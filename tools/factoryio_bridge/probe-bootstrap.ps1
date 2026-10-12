param(
    [Parameter(Mandatory = $true)][string]$ManagedCopy,
    [Parameter(Mandatory = $true)][string]$BridgeDll,
    [Parameter(Mandatory = $true)][string]$Report,
    [Parameter(Mandatory = $true)][string]$ProbeCfg
)
$ErrorActionPreference = 'Stop'
# Run under Windows PowerShell/.NET Framework, not PowerShell Core. This creates
# a separate interpreter with the actual SceneProxy type allocated without
# invoking its constructor. It never invokes Install or any Unity method.
$taskManagedPath = (Resolve-Path -LiteralPath $ManagedCopy).Path
$taskBridgePath = (Resolve-Path -LiteralPath $BridgeDll).Path
[void][System.Reflection.Assembly]::LoadFrom((Join-Path $taskManagedPath 'Microsoft.Scripting.dll'))
[void][System.Reflection.Assembly]::LoadFrom((Join-Path $taskManagedPath 'Microsoft.Dynamic.dll'))
[void][System.Reflection.Assembly]::LoadFrom((Join-Path $taskManagedPath 'IronPython.dll'))
$taskEngine = [IronPython.Hosting.Python]::CreateEngine()
$taskBuiltins = [IronPython.Hosting.Python]::GetBuiltinModule($taskEngine)
[void]$taskBuiltins.RemoveVariable('__import__')
$taskScope = $taskEngine.CreateScope()
$taskAppAssembly = [System.Reflection.Assembly]::LoadFrom((Join-Path $taskManagedPath 'Assembly-CSharp.dll'))
$taskProxyType = $taskAppAssembly.GetType('FactoryIO.Scripting.SceneProxy', $true)
$taskDummyProxy = [System.Runtime.Serialization.FormatterServices]::GetUninitializedObject($taskProxyType)
$taskScope.SetVariable('scene', $taskDummyProxy)
$taskProbeLines = @(
    '_agent_load_type = scene.GetType().GetType().Assembly.GetType("System.Reflection.Assembly")',
    '_agent_load_method = [m for m in _agent_load_type.GetMethods() if m.Name == "LoadFrom" and len(m.GetParameters()) == 1][0]',
    ('_agent_bridge_assembly = _agent_load_method.Invoke(None, (r"{0}",))' -f $taskBridgePath),
    '_agent_protocol = _agent_bridge_assembly.GetType("FactoryIOAgentBridge.Bootstrap").GetMethod("Protocol").Invoke(None, None)'
)
$taskProbeCfgPath = [System.IO.Path]::GetFullPath($ProbeCfg)
$taskProbeLines | Set-Content -LiteralPath $taskProbeCfgPath -Encoding UTF8
$taskProbeSource = $taskEngine.CreateScriptSourceFromString(('execfile(r"{0}")' -f $taskProbeCfgPath), [Microsoft.Scripting.SourceCodeKind]::SingleStatement)
[void]$taskProbeSource.Execute($taskScope)
$taskResult = [pscustomobject]@{
    protocol = $taskScope.GetVariable('_agent_protocol')
    import_available = $taskBuiltins.ContainsVariable('__import__')
    execfile_available = $taskBuiltins.ContainsVariable('execfile')
    unity_install_invoked = $false
    scope = 'isolated IronPython interpreter and uninitialized SceneProxy; no Unity methods invoked'
}
if ($taskResult.protocol -ne 'factoryio-agent-file-bridge/1') { throw 'Protocol probe failed' }
$taskResult | ConvertTo-Json | Set-Content -LiteralPath $Report -Encoding UTF8
$taskResult | ConvertTo-Json
$taskEngine.Runtime.Shutdown()
