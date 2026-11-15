param([string]$Workspace = 'C:\Cursor_Github\OT_v1', [switch]$Apply, [switch]$SignedIn, [switch]$PromptForCredentials, [string]$SignedInUser)
$ErrorActionPreference = 'Stop'
$workspaceRoot = (Resolve-Path -LiteralPath $Workspace).Path
$taskPython = Join-Path $workspaceRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python interpreter is missing.' }
if ($SignedIn -and $PromptForCredentials) { throw 'Choose signed-in operation or stored credentials, not both.' }
$taskCredentials = $null
if ($Apply -and $PromptForCredentials) {
 $accountName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
 $taskCredentials = Get-Credential -UserName $accountName -Message 'Windows credentials for unattended Stockie tasks. Enter them only in this local prompt.'
 if ($null -eq $taskCredentials) { throw 'Credential prompt cancelled; no tasks changed.' }
}
$jobMap = @{
 'Stockie - US Index'='us-index'; 'Stockie - Kite Token'='kite-token';
 'Stockie - Option Instruments'='option-instruments'; 'Stockie - Macro Refresh'='macro-refresh';
 'Stockie - Asia Partial'='asia-partial'; 'Stockie - Option Snapshot Open'='option-snapshot';
 'Stockie - GIFT Open'='gift-capture'; 'Stockie - Open Gap'='open-gap';
 'Stockie - Prediction and Selection'='prediction'; 'Stockie - Paper Entry'='paper-entry';
 'Stockie - Paper Monitor'='paper-monitor'; 'Stockie - GIFT Close'='gift-close';
 'Stockie - Paper Report'='paper-report'; 'Stockie - Asia Final'='asia-final';
 'Stockie - Market Refresh'='market-refresh'; 'Stockie - Option OHLC Audit'='option-ohlc';
 'Stockie - Signal Summary'='signal-summary'
}
$backupPath = Join-Path $workspaceRoot ('output\scheduler\backups\' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
if ($Apply) { New-Item -ItemType Directory -Path $backupPath -Force | Out-Null }
foreach ($taskName in ($jobMap.Keys | Sort-Object)) {
 $registeredTask = Get-ScheduledTask -TaskName $taskName -TaskPath '\'
 $jobId = $jobMap[$taskName]
 $arguments = '-u -m technical_analysis_service.scheduled_jobs --workspace "' + $workspaceRoot + '" --job ' + $jobId
 if (-not $Apply) { [pscustomobject]@{Task=$taskName;Interpreter=$taskPython;Arguments=$arguments}; continue }
 Export-ScheduledTask -TaskName $taskName -TaskPath '\' | Set-Content -LiteralPath (Join-Path $backupPath ($jobId + '.xml')) -Encoding UTF8
 $action = New-ScheduledTaskAction -Execute $taskPython -Argument $arguments -WorkingDirectory $workspaceRoot
 $limit = if ($jobId -eq 'paper-monitor') { New-TimeSpan -Minutes 4 } else { New-TimeSpan -Minutes 25 }
 $retryCount = if ($jobId -eq 'kite-token') { 0 } else { 2 }
 $settingsOptions = @{WakeToRun=$true;StartWhenAvailable=$true;AllowStartIfOnBatteries=$true;DontStopIfGoingOnBatteries=$true;MultipleInstances='IgnoreNew';ExecutionTimeLimit=$limit}
 if ($retryCount -gt 0) { $settingsOptions.RestartCount=$retryCount; $settingsOptions.RestartInterval=New-TimeSpan -Minutes 1 }
 $settings = New-ScheduledTaskSettingsSet @settingsOptions
 $triggers = @($registeredTask.Triggers)
 if ($jobId -eq 'gift-capture') {
   $preopenExists = @($triggers | Where-Object { $_.StartBoundary -match 'T09:11:00' }).Count -gt 0
   if (-not $preopenExists) { $triggers += New-ScheduledTaskTrigger -Daily -At '09:11' }
 }
 # Keep the existing account and stored password; do not re-register credentials.
 if ($SignedIn) {
   $interactiveUser = if ($SignedInUser) { $SignedInUser } else { [System.Security.Principal.WindowsIdentity]::GetCurrent().Name }
   $principal = New-ScheduledTaskPrincipal -UserId $interactiveUser -LogonType Interactive -RunLevel Limited
   Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Settings $settings -Trigger $triggers -Principal $principal | Out-Null
 } elseif ($taskCredentials) {
   Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Settings $settings -Trigger $triggers -User $taskCredentials.UserName -Password $taskCredentials.GetNetworkCredential().Password | Out-Null
 } else {
   Set-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Settings $settings -Trigger $triggers | Out-Null
 }
 Write-Output ('Updated ' + $taskName)
}
if ($Apply) {
 & wevtutil.exe sl Microsoft-Windows-TaskScheduler/Operational /e:true
 if ($LASTEXITCODE -ne 0) { Write-Warning 'Task actions were repaired, but enabling scheduler history requires an elevated Windows account.' }
 Write-Output ('Original task XML backups: ' + $backupPath)
}
