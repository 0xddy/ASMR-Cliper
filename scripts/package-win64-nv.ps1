# Assemble artifacts from successful download/build jobs. This phase never installs dependencies.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$WorkDirectory, [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$work = [IO.Path]::GetFullPath($WorkDirectory)
$payload = Join-Path $work 'payload/ASMR Cliper 构建'
$native = Join-Path $work 'native'
$reports = Join-Path $work 'reports'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $work 'artifacts' }
$out = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $reports -Force | Out-Null
Start-Transcript -Path (Join-Path $reports 'package.log') | Out-Null
function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
try {
    $info = Get-Content -LiteralPath (Join-Path $payload 'build-info.json') -Raw | ConvertFrom-Json
    $compiled = Get-Content -LiteralPath (Join-Path $native 'build-source.json') -Raw | ConvertFrom-Json
    if ($compiled.version -ne $info.version -or $compiled.commit -ne $info.commit -or
        ($env:GITHUB_SHA -and $compiled.commit -ne $env:GITHUB_SHA)) { throw 'Download and native artifacts must come from the same source revision.' }
    if ((Get-FileHash -LiteralPath (Join-Path $native 'asmrcliper.exe')).Hash -ne $compiled.exe_sha256) { throw 'Executable SHA-256 mismatch.' }
    Copy-Item -LiteralPath (Join-Path $native 'asmrcliper.exe') -Destination $payload
    if (Test-Path -LiteralPath (Join-Path $native 'build-reports')) {
        Copy-Item -LiteralPath (Join-Path $native 'build-reports') -Destination $reports -Recurse
    }
    $python = Join-Path $payload 'runtime/python/python.exe'
    Invoke-Checked $python @('-X','utf8','-m','unittest','discover','-s',(Join-Path $payload 'tests'),'-p','test_*.py','-v')
    Invoke-Checked (Join-Path $native 'process_tests.exe') @($python)

    $relocated = Join-Path $work 'payload/ASMR-Cliper'
    foreach ($path in @($payload,$relocated)) {
        $resolved = [IO.Path]::GetFullPath($path)
        if (-not $resolved.StartsWith($work + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Move outside the package work directory.' }
    }
    if (Test-Path -LiteralPath $relocated) { throw 'Relocated target already exists.' }
    Move-Item -LiteralPath $payload -Destination $relocated
    $python = Join-Path $relocated 'runtime/python/python.exe'
    $builder = Join-Path $relocated 'scripts/package_win64_nv.py'
    Invoke-Checked $python @('-X','utf8',$builder,'verify','--root',$relocated,'--report',(Join-Path $reports 'portable-check.json'))
    Invoke-Checked $python @('-X','utf8',$builder,'archive','--root',$relocated,'--output',$out)
} finally {
    Stop-Transcript | Out-Null
}
