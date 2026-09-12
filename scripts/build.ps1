param([ValidateSet('Release','Debug')][string]$Configuration='Release')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
cmake -S $projectRoot -B (Join-Path $projectRoot 'build') -G 'Visual Studio 17 2022' -A x64
if ($LASTEXITCODE -ne 0) { throw 'CMake configuration failed.' }
cmake --build (Join-Path $projectRoot 'build') --config $Configuration --parallel
if ($LASTEXITCODE -ne 0) { throw 'C++ build failed.' }
ctest --test-dir (Join-Path $projectRoot 'build') -C $Configuration --output-on-failure
if ($LASTEXITCODE -ne 0) { throw 'C++ tests failed.' }
Write-Output "Built: $projectRoot\bin\asmrcliper.exe"
