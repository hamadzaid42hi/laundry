$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = 'C:\Users\hamad\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not $env:BACKUP_ENCRYPTION_KEY) { throw 'Set BACKUP_ENCRYPTION_KEY before installing the scheduled backup.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}\backup.py"' -f $project) -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName 'Al Rams Laundry Encrypted Backup' -Action $action -Trigger $trigger -Settings $settings -Description 'Daily encrypted SQLite backup for Al Rams Laundry' -Force
