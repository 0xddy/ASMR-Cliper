# Join Release parts into the original ZIP. Requires only Windows PowerShell 5.1+.
[CmdletBinding()]
param([string]$Manifest = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}
if (-not $Manifest) {
    $manifests = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.zip.parts.json' -File)
    if ($manifests.Count -ne 1) { throw 'Keep exactly one package manifest here, or pass -Manifest.' }
    $Manifest = $manifests[0].FullName
}
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$folder = Split-Path $manifestPath -Parent
$data = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($data.format_version -ne 1 -or $data.archive.name -notmatch '^ASMR-Cliper-\d+\.\d+\.\d+-win64-nv\.zip$' -or
    $data.archive.sha256 -notmatch '^[0-9a-f]{64}$' -or $data.archive.size -le 0 -or @($data.parts).Count -eq 0) {
    throw 'Invalid package manifest.'
}
$target = Join-Path $folder $data.archive.name
if (Test-Path -LiteralPath $target) {
    if ((Get-Item -LiteralPath $target).Length -eq $data.archive.size -and
        (Get-Sha256 $target) -eq $data.archive.sha256) {
        Write-Output "Already verified: $target"
        return
    }
    throw "Output already exists with different contents: $target"
}
[long]$totalSize = 0
$index = 0
foreach ($part in $data.parts) {
    $index++
    $expectedName = '{0}.{1:D3}' -f $data.archive.name, $index
    if ($part.name -cne $expectedName -or $part.sha256 -notmatch '^[0-9a-f]{64}$' -or
        $part.size -le 0 -or $part.size -ge 2GB) { throw 'Invalid or unordered part in manifest.' }
    $path = Join-Path $folder $part.name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne $part.size) {
        throw "Missing or incomplete part: $($part.name)"
    }
    $totalSize += $part.size
}
if ($totalSize -ne $data.archive.size) { throw 'Part sizes do not match the archive size.' }
if (([IO.DriveInfo]::new([IO.Path]::GetPathRoot($folder))).AvailableFreeSpace -lt $totalSize + 64MB) {
    throw 'Not enough disk space for the merged ZIP.'
}
$temporary = $target + '.joining'
$output = $null
$created = $false
try {
    $output = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $created = $true
    foreach ($part in $data.parts) {
        $path = Join-Path $folder $part.name
        Write-Output "Checking and joining $($part.name)"
        $inputStream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $hash = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = [BitConverter]::ToString($hash.ComputeHash($inputStream)).Replace('-', '').ToLowerInvariant()
            if ($actual -ne $part.sha256) { throw "SHA-256 mismatch: $($part.name)" }
            $inputStream.Position = 0
            $inputStream.CopyTo($output, 4MB)
        } finally {
            $hash.Dispose()
            $inputStream.Dispose()
        }
    }
    $output.Dispose()
    $output = $null
    if ((Get-Item -LiteralPath $temporary).Length -ne $data.archive.size -or
        (Get-Sha256 $temporary) -ne $data.archive.sha256) {
        throw 'Merged ZIP SHA-256/size mismatch.'
    }
    [IO.File]::Move($temporary, $target)
    Write-Output "Verified: $target"
    Write-Output 'Extract the complete ZIP, then run ASMR-Cliper\asmrcliper.exe.'
} finally {
    if ($null -ne $output) { $output.Dispose() }
    # Only remove the partial file created by this invocation in the manifest directory.
    if ($created -and (Test-Path -LiteralPath $temporary)) { Remove-Item -LiteralPath $temporary }
}
