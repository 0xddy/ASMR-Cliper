# Compile only. Models and Python environments are provided by the download job.
[CmdletBinding()]
param([string]$WorkDirectory = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$source = Split-Path $PSScriptRoot -Parent
if (-not $WorkDirectory) { $WorkDirectory = Join-Path $source ('build/native-' + [guid]::NewGuid().ToString('N')) }
$work = [IO.Path]::GetFullPath($WorkDirectory)
if ((Test-Path -LiteralPath $work) -and -not (Test-Path -LiteralPath (Join-Path $work '.asmrclip-ci.json'))) { throw 'Use a new work directory.' }
$reports = Join-Path $work 'reports'
New-Item -ItemType Directory -Path $reports -Force | Out-Null
Start-Transcript -Path (Join-Path $reports 'build.log') | Out-Null
function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE" }
}
try {
    $checkout = Join-Path $work 'source'
    New-Item -ItemType Directory -Path $checkout | Out-Null
    foreach ($folder in @('src','third_party','tests','engine','scripts')) {
        Copy-Item -LiteralPath (Join-Path $source $folder) -Destination $checkout -Recurse
    }
    foreach ($file in @('CMakeLists.txt','config/defaults.json','config/environment.json','docs/prompts/strict-v2.txt','docs/prompts/relaxed-v3.txt','docs/prompts/extract-v4.txt')) {
        $target = Join-Path $checkout $file
        New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $source $file) -Destination $target
    }
    $build = Join-Path $work 'cmake'
    Invoke-Checked 'cmake' @('-S',$checkout,'-B',$build,'-G','Visual Studio 17 2022','-A','x64')
    Invoke-Checked 'cmake' @('--build',$build,'--config','Release','--parallel','2')
    try {
        Invoke-Checked 'ctest' @('--test-dir',$build,'-C','Release','--output-on-failure','--output-junit',(Join-Path $reports 'native-ctest.xml'))
    } finally {
        Get-ChildItem -LiteralPath $build -Filter '*-regression.json' -File | Copy-Item -Destination $reports
        $log = Join-Path $build 'Testing/Temporary/LastTest.log'
        if (Test-Path -LiteralPath $log) { Copy-Item -LiteralPath $log -Destination $reports }
    }
    $native = Join-Path $work 'native'
    New-Item -ItemType Directory -Path $native | Out-Null
    Copy-Item -LiteralPath (Join-Path $checkout 'bin/asmrcliper.exe') -Destination $native
    Copy-Item -LiteralPath (Join-Path $build 'Release/process_tests.exe') -Destination $native
    $version = [regex]::Match((Get-Content -LiteralPath (Join-Path $checkout 'CMakeLists.txt') -Raw),'project\(ASMRCLIP VERSION (\d+\.\d+\.\d+)').Groups[1].Value
    @{commit=[string]$env:GITHUB_SHA; version=$version; exe_sha256=(Get-FileHash -LiteralPath (Join-Path $native 'asmrcliper.exe')).Hash} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $native 'build-source.json') -Encoding utf8
    Copy-Item -LiteralPath $reports -Destination (Join-Path $native 'build-reports') -Recurse
} finally {
    Stop-Transcript | Out-Null
}
