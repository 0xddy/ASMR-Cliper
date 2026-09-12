# Download all dependencies into a reusable artifact; this job does not compile the GUI.
[CmdletBinding()]
param(
    [string]$WorkDirectory = '',
    [string]$Proxy = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Run this builder with PowerShell 7 (pwsh).' }
if (-not $IsWindows -or -not [Environment]::Is64BitProcess) { throw 'Windows x64 is required.' }
$source = Split-Path $PSScriptRoot -Parent
if (-not $WorkDirectory) { $WorkDirectory = Join-Path $source ('build/win64-nv-' + [guid]::NewGuid().ToString('N')) }
$work = [IO.Path]::GetFullPath($WorkDirectory)
if ((Test-Path -LiteralPath $work) -and -not (Test-Path -LiteralPath (Join-Path $work '.asmrclip-ci.json'))) { throw 'Use a new work directory.' }
if (([IO.DriveInfo]::new([IO.Path]::GetPathRoot($work))).AvailableFreeSpace -lt 50GB) {
    throw 'The build volume needs at least 50 GiB free for CUDA, all models, temporary wheels and ZIP64 output.'
}
New-Item -ItemType Directory -Path $work -Force | Out-Null
$reports = Join-Path $work 'reports'
New-Item -ItemType Directory -Path $reports | Out-Null
Start-Transcript -Path (Join-Path $reports 'build.log') | Out-Null

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}

$savedEnv = @{}
foreach ($key in @('TEMP','TMP','PYTHONUTF8','PYTHONDONTWRITEBYTECODE','PIP_NO_CACHE_DIR','PIP_DISABLE_PIP_VERSION_CHECK','ASMRCLIP_BUILD_PROXY')) {
    $savedEnv[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}
try {
    $env:TEMP = Join-Path $work 'temp'
    $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP | Out-Null
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PIP_NO_CACHE_DIR = '1'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:ASMRCLIP_BUILD_PROXY = $Proxy

    $manifest = Get-Content -LiteralPath (Join-Path $source 'config/environment.json') -Raw | ConvertFrom-Json
    $bootstrapZip = Join-Path $work 'python-bootstrap.zip'
    $request = @{ Uri=$manifest.python.url; OutFile=$bootstrapZip; MaximumRetryCount=3; RetryIntervalSec=3 }
    if ($Proxy) { $request.Proxy = $Proxy } else { $request.NoProxy = $true }
    Invoke-WebRequest @request
    if ((Get-FileHash -LiteralPath $bootstrapZip -Algorithm SHA256).Hash -ne $manifest.python.sha256 -or
        (Get-Item -LiteralPath $bootstrapZip).Length -ne $manifest.python.size) { throw 'Embedded Python checksum/size mismatch.' }
    $bootstrap = Join-Path $work 'bootstrap'
    Expand-Archive -LiteralPath $bootstrapZip -DestinationPath $bootstrap
    $bootstrapPython = Join-Path $bootstrap 'python.exe'
    # Build and test with spaces and Unicode in the path; move before final validation.
    $payload = Join-Path $work 'payload/ASMR Cliper 构建'
    $builder = Join-Path $source 'scripts/package_win64_nv.py'
    Invoke-Checked $bootstrapPython @('-X','utf8',$builder,'prepare','--source',$source,'--root',$payload,'--python-archive',$bootstrapZip)
    $python = Join-Path $payload 'runtime/python/python.exe'
    $builder = Join-Path $payload 'scripts/package_win64_nv.py'

    # App-local Microsoft CRT DLLs: no global redistributable installer is needed.
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    $vs = & $vswhere -latest -products '*' -version '[17.0,18.0)' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $vs) { throw 'Visual Studio 2022 C++ Build Tools are required.' }
    $crt = Get-ChildItem -LiteralPath (Join-Path $vs 'VC/Redist/MSVC') -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName 'x64/Microsoft.VC143.CRT' } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $crt) { throw 'The x64 Microsoft.VC143.CRT redistributable files are missing.' }
    Invoke-Checked $python @('-X','utf8',$builder,'install','--root',$payload,'--crt',$crt)

    Invoke-Checked $bootstrapPython @('-X','utf8',$builder,'snapshot','--root',$payload,'--output',(Join-Path $work 'dependencies'))
} finally {
    foreach ($key in $savedEnv.Keys) { [Environment]::SetEnvironmentVariable($key, $savedEnv[$key], 'Process') }
    Stop-Transcript | Out-Null
}

