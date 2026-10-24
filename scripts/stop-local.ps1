[CmdletBinding()]
param([switch]$Database)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $taskRoot '.venv\Scripts\python.exe'
$all = @(Get-CimInstance Win32_Process)
$parents = @($all | Where-Object {
    $_.ExecutablePath -eq $python -and ($_.CommandLine -like '*app.cli api*' -or $_.CommandLine -like '*app.cli worker*')
})
foreach ($parent in $parents) {
    # The venv redirector starts a child interpreter on Windows. Only stop its matching CLI child.
    foreach ($child in $all | Where-Object { $_.ParentProcessId -eq $parent.ProcessId -and ($_.CommandLine -like '*app.cli api*' -or $_.CommandLine -like '*app.cli worker*') }) {
        Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $parent.ProcessId -ErrorAction SilentlyContinue
}
if ($Database) {
    $pgData = Join-Path $taskRoot '.runtime\pgdata'
    $pgCtl = Join-Path $taskRoot '.runtime\postgresql\pgsql\bin\pg_ctl.exe'
    & $pgCtl -D $pgData -m fast -w stop
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo detener PostgreSQL; revisa su estado' }
}
Write-Output 'Procesos de Barrio detenidos.'
