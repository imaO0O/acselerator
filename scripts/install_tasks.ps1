# Регистрирует фоновые задачи агента в Планировщике заданий Windows (имена KBAgent-…):
#   резервная копия — каждый день (по умолчанию в 22:00; если компьютер был выключен — при включении),
#   почта отдела — каждые 5 минут, папка документов — каждые 10 минут, цены из 1С — каждый час.
# Почта, папка и 1С регистрируются, только если настроены (data\settings.env). Итог каждого запуска — на
# странице «Администрирование» → «Система»; там же видно, если задача отстала или завершилась с ошибкой.
#   scripts\install_tasks.bat                      — зарегистрировать (повторный запуск обновляет задачи)
#   scripts\install_tasks.bat -Jobs backup,mail    — только эти задачи (даже если не настроены)
#   scripts\install_tasks.bat -BackupAt 23:30      — время резервной копии
#   scripts\install_tasks.bat -DryRun              — показать, что будет зарегистрировано, ничего не меняя
#   scripts\install_tasks.bat -Remove              — удалить задачи агента
# Задачи выполняются от текущего пользователя, пока он вошёл в Windows (как и чат). Чтобы они шли без входа,
# откройте задачу в Планировщике и включите «Выполнять для всех пользователей» — Windows спросит пароль.
param([string[]]$Jobs = @(), [string]$BackupAt = "22:00", [switch]$DryRun, [switch]$Remove)

$root = (Resolve-Path "$PSScriptRoot\..").Path
$prefix = "KBAgent-"

if ($Remove) {
    $tasks = @(Get-ScheduledTask -TaskName "$prefix*" -ErrorAction SilentlyContinue)
    foreach ($t in $tasks) {
        if ($DryRun) { Write-Host "  будет удалена: $($t.TaskName)" }
        else { Unregister-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -Confirm:$false }
    }
    Write-Host "Задач агента удалено: $(if ($DryRun) { 0 } else { $tasks.Count })"
    exit 0
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { Write-Host "Не найден Python: сначала install.bat или python scripts\setup.py" -ForegroundColor Red; exit 1 }
    $python = $cmd.Source
}
# pythonw — без окна консоли, которое иначе мелькало бы каждые 5 минут
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$runJob = Join-Path $root "scripts\run_job.py"

$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$plan = & $python $runJob --plan
if ($LASTEXITCODE -ne 0) { Write-Host "Не удалось получить список задач (scripts\run_job.py --plan)" -ForegroundColor Red; exit 1 }
$Jobs = @($Jobs | ForEach-Object { $_ -split "," } | Where-Object { $_ })

$user = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 40) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$done = 0
foreach ($line in $plan) {
    $key, $every, $ready, $title = $line -split "\|", 4
    if ($Jobs.Count -and $Jobs -notcontains $key) { continue }
    if (-not $Jobs.Count -and $ready -ne "1") { Write-Host "  $title — не настроено, пропущено"; continue }
    if ($key -eq "backup") {
        $trigger = New-ScheduledTaskTrigger -Daily -At $BackupAt
        $when = "каждый день в $BackupAt"
    } else {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes ([int]$every))
        $when = "каждые $every мин"
    }
    if ($DryRun) { Write-Host "  будет: $prefix$key — $title, $when"; continue }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$runJob`" $key" -WorkingDirectory $root
    Register-ScheduledTask -TaskName "$prefix$key" -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "AI-агент базы знаний: $title" -Force | Out-Null
    Write-Host "  $prefix$key — $title, $when"
    $done++
}
if (-not $DryRun) {
    Write-Host "Зарегистрировано задач: $done. Итоги запусков — «Администрирование» → «Система»." -ForegroundColor Green
}
