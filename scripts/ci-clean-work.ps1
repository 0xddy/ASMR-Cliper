param([Parameter(Mandatory)][string]$WorkDirectory)
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true') { throw 'This cleanup is only for GitHub Actions work directories.' }
$work = [IO.Path]::GetFullPath($WorkDirectory)
if (-not (Test-Path -LiteralPath $work)) { return }
$item = Get-Item -LiteralPath $work -Force
if ($item.LinkType -or $item.Name -notmatch '^asmrclip-win64-nv-[0-9a-f]{32}$') { throw 'Unrecognized work directory.' }
$marker = Get-Content -LiteralPath (Join-Path $work '.asmrclip-ci.json') -Raw | ConvertFrom-Json
if ($marker.root -ne $work -or $marker.run -ne $env:GITHUB_RUN_ID -or $marker.job -ne $env:GITHUB_JOB) {
    throw 'Work directory ownership does not match this job.'
}
# Absolute target and ownership are checked above; never remove the runner temp root or a tool cache.
Remove-Item -LiteralPath $work -Recurse -Force
Write-Output 'Removed this job''s temporary files.'
