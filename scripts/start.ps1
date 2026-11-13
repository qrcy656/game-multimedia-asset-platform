[CmdletBinding()]
param([switch]$WithoutGitLab)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$projectDirectory = (Get-Location).Path
if ($projectDirectory.Contains(',')) { throw 'Project directory cannot contain a comma.' }
docker run --rm --network none --read-only --user 0:0 --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --security-opt no-new-privileges --mount "type=bind,src=$projectDirectory,dst=/workspace" python:3.12-slim-bookworm python /workspace/scripts/initialize.py 1000 1000
if ($LASTEXITCODE -ne 0) { throw 'Initialization failed; inspect the message above.' }
$previousProfiles = $env:COMPOSE_PROFILES
try {
    if ($WithoutGitLab) { $env:COMPOSE_PROFILES = '' }
    if ($WithoutGitLab) {
        docker compose up -d --build app animation-worker effects postgres redis
    } else {
        docker compose up -d --build
    }
    if ($LASTEXITCODE -ne 0) { throw 'Docker startup failed; inspect the output above.' }
} finally {
    if ($null -eq $previousProfiles) { Remove-Item Env:COMPOSE_PROFILES -ErrorAction SilentlyContinue }
    else { $env:COMPOSE_PROFILES = $previousProfiles }
}
$originLine = Get-Content -LiteralPath '.env' | Where-Object { $_.StartsWith('APP_ORIGIN=') } | Select-Object -Last 1
$origin = if ($env:APP_ORIGIN) { $env:APP_ORIGIN } elseif ($originLine) { $originLine.Substring(11) } else { 'http://localhost:18082' }
Write-Host "Community is ready at $origin"
Write-Host 'Bundled GitLab initializes OAuth automatically; first startup can take several minutes.'
