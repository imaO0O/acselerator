param([int]$EveryMinutes = 15, [switch]$Remove, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$name = 'KBAgent-trade'
if ($Remove) {
    if (-not $DryRun) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Output "Remove $name"
    exit 0
}
if ($EveryMinutes -lt 5 -or $EveryMinutes -gt 1440) { throw 'EveryMinutes must be 5..1440' }
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$config = Join-Path $root 'data\trade\sync.json'
$database = Join-Path $root 'data\trade\catalog.sqlite'
$script = Join-Path $root 'scripts\sync_trade.py'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install project environment first' }
if (-not (Test-Path -LiteralPath $config)) { throw 'Configure data/trade/sync.json first' }
if ($DryRun) { Write-Output "$name every $EveryMinutes minutes; $script; $config"; exit 0 }
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" --config `"$config`" --db `"$database`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $taskSettings -Description 'Local trade catalog prices and stock refresh' -Force | Out-Null
Write-Output "$name registered for the current signed-in user. Last report: data/trade/sync.last.json"
