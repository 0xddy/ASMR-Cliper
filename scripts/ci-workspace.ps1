param([ValidateRange(1,500)][int]$MinimumFreeGiB = 50)
$ErrorActionPreference = 'Stop'
if (-not $env:GITHUB_ACTIONS -or -not $env:GITHUB_OUTPUT) { throw 'This helper runs inside GitHub Actions.' }
$candidates = @($env:RUNNER_TEMP)
if ($env:RUNNER_ENVIRONMENT -eq 'github-hosted') {
    $candidates += [IO.DriveInfo]::GetDrives() |
        Where-Object { $_.IsReady -and $_.DriveType -eq 'Fixed' } |
        ForEach-Object { $_.RootDirectory.FullName }
}
$parent = $candidates | Sort-Object -Unique |
    Sort-Object { ([IO.DriveInfo]::new([IO.Path]::GetPathRoot($_))).AvailableFreeSpace } -Descending |
    Select-Object -First 1
$free = ([IO.DriveInfo]::new([IO.Path]::GetPathRoot($parent))).AvailableFreeSpace
if ($free -lt $MinimumFreeGiB * 1GB) {
    throw ('This job needs {0} GiB free; available: {1:N1} GiB. Select a larger Windows runner.' -f $MinimumFreeGiB,($free / 1GB))
}
$work = Join-Path $parent ('asmrclip-win64-nv-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
@{root=[IO.Path]::GetFullPath($work); run=$env:GITHUB_RUN_ID; job=$env:GITHUB_JOB} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work '.asmrclip-ci.json') -Encoding utf8
"work=$work" >> $env:GITHUB_OUTPUT
"Build disk free: $([math]::Round($free / 1GB, 1)) GiB"
