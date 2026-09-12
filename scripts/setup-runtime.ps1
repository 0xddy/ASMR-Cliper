param(
    [string]$Proxy = 'http://127.0.0.1:10886',
    [switch]$Direct,
    [ValidateSet('all','python','dependencies','whisper','ast','review','ffmpeg','qwen','aligner','clap','neural')][string]$Component = 'all'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$config = Get-Content -LiteralPath (Join-Path $root 'config/defaults.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$config.proxy_enabled = -not $Direct
$config.proxy_url = $Proxy
$jobs = Join-Path $root 'runtime/jobs'
[IO.Directory]::CreateDirectory($jobs) | Out-Null
$path = Join-Path $jobs ('setup-' + [Guid]::NewGuid().ToString('N') + '.json')
[IO.File]::WriteAllText($path,($config | ConvertTo-Json -Depth 10),(New-Object Text.UTF8Encoding($false)))
& (Join-Path $PSScriptRoot 'environment.ps1') -Action install -Config $path -Component $Component
exit $LASTEXITCODE
