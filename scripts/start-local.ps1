[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$pgBin = Join-Path $taskRoot '.runtime\postgresql\pgsql\bin'
$pgData = Join-Path $taskRoot '.runtime\pgdata'
if (-not (Test-Path -LiteralPath (Join-Path $pgBin 'pg_ctl.exe'))) {
    throw 'Falta PostgreSQL portátil. Usa docker compose up --build -d o instala PostgreSQL 16 según README.md.'
}
if (-not (Test-Path -LiteralPath (Join-Path $pgData 'PG_VERSION'))) {
    & (Join-Path $pgBin 'initdb.exe') -D $pgData -U postgres -A trust --encoding=UTF8 --locale=C
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo inicializar PostgreSQL' }
}
& (Join-Path $pgBin 'pg_ctl.exe') -D $pgData status
if ($LASTEXITCODE -ne 0) {
    & (Join-Path $pgBin 'pg_ctl.exe') -D $pgData -l (Join-Path $taskRoot '.runtime\postgres.log') -o '-h 127.0.0.1 -p 5432' -w start
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo iniciar PostgreSQL' }
}
$exists = & (Join-Path $pgBin 'psql.exe') -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='barrio'"
if ($exists -ne '1') {
    & (Join-Path $pgBin 'createdb.exe') -h 127.0.0.1 -U postgres barrio
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear barrio' }
}
uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron instalar dependencias' }
uv run bot migrate
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron aplicar migraciones' }
uv run seed demo
if ($LASTEXITCODE -ne 0) { throw 'No se pudieron crear los datos de ejemplo' }
$python = Join-Path $taskRoot '.venv\Scripts\python.exe'
$processes = @{}
foreach ($mode in @('api','worker')) {
    $existing = Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -eq $python -and $_.CommandLine -like "*app.cli $mode*"
    } | Select-Object -First 1
    if ($existing) { $processes[$mode] = $existing.ProcessId; continue }
    $process = Start-Process -FilePath $python -ArgumentList '-m','app.cli',$mode -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRoot ".runtime\$mode.stdout.log") -RedirectStandardError (Join-Path $taskRoot ".runtime\$mode.stderr.log") -PassThru
    $processes[$mode] = $process.Id
}
$processes | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskRoot '.runtime\processes.json')
Write-Output 'Simulador: http://127.0.0.1:8000/dev/inbox'
