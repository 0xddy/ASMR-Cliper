$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Drawing
$root=Join-Path (Split-Path $PSScriptRoot -Parent) 'src'
function RoundPath([float]$x,[float]$y,[float]$w,[float]$h,[float]$radius) {
    $path=New-Object Drawing.Drawing2D.GraphicsPath
    $diameter=$radius*2
    $path.AddArc($x,$y,$diameter,$diameter,180,90)
    $path.AddArc($x+$w-$diameter,$y,$diameter,$diameter,270,90)
    $path.AddArc($x+$w-$diameter,$y+$h-$diameter,$diameter,$diameter,0,90)
    $path.AddArc($x,$y+$h-$diameter,$diameter,$diameter,90,90)
    $path.CloseFigure()
    return $path
}
$frames=New-Object 'Collections.Generic.List[byte[]]'
$sizes=@(16,24,32,48,64,128,256)
foreach($size in $sizes) {
    $bitmap=New-Object Drawing.Bitmap($size,$size)
    $g=[Drawing.Graphics]::FromImage($bitmap)
    $g.SmoothingMode=[Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.PixelOffsetMode=[Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([Drawing.Color]::Transparent)
    $g.ScaleTransform($size/256.0,$size/256.0)
    $bg=New-Object Drawing.SolidBrush([Drawing.Color]::FromArgb(7,90,82))
    $white=New-Object Drawing.SolidBrush([Drawing.Color]::White)
    $path=RoundPath 8 8 240 240 56
    $g.FillPath($bg,$path);$path.Dispose()
    $headphone=New-Object Drawing.Pen([Drawing.Color]::White,16)
    $headphone.StartCap=[Drawing.Drawing2D.LineCap]::Round;$headphone.EndCap=[Drawing.Drawing2D.LineCap]::Round
    $arc=New-Object Drawing.Drawing2D.GraphicsPath
    $arc.AddLine(61,150,61,118)
    $arc.AddBezier(61,118,61,75,88,46,128,46)
    $arc.AddBezier(128,46,168,46,195,75,195,118)
    $arc.AddLine(195,118,195,150)
    $g.DrawPath($headphone,$arc);$arc.Dispose()
    foreach($left in @(46,178)) {$path=RoundPath $left 125 32 68 16;$g.FillPath($white,$path);$path.Dispose()}
    $wave=New-Object Drawing.Pen([Drawing.Color]::FromArgb(128,227,195),12)
    $wave.StartCap=[Drawing.Drawing2D.LineCap]::Round;$wave.EndCap=[Drawing.Drawing2D.LineCap]::Round
    $g.DrawLine($wave,106,128,106,164);$g.DrawLine($wave,128,107,128,185);$g.DrawLine($wave,150,128,150,164)
    $stream=New-Object IO.MemoryStream
    $bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
    $frames.Add($stream.ToArray())
    if($size -eq 256) {$bitmap.Save((Join-Path $root 'app-icon.png'),[Drawing.Imaging.ImageFormat]::Png)}
    $stream.Dispose();$wave.Dispose();$headphone.Dispose();$white.Dispose();$bg.Dispose();$g.Dispose();$bitmap.Dispose()
}
$file=[IO.File]::Create((Join-Path $root 'app.ico'))
$writer=New-Object IO.BinaryWriter($file)
$writer.Write([uint16]0);$writer.Write([uint16]1);$writer.Write([uint16]$sizes.Count)
$offset=6+16*$sizes.Count
for($i=0;$i -lt $sizes.Count;$i++) {
    $dimension=if($sizes[$i] -eq 256){0}else{$sizes[$i]}
    $writer.Write([byte]$dimension);$writer.Write([byte]$dimension);$writer.Write([byte]0);$writer.Write([byte]0)
    $writer.Write([uint16]1);$writer.Write([uint16]32);$writer.Write([uint32]$frames[$i].Length);$writer.Write([uint32]$offset)
    $offset+=$frames[$i].Length
}
foreach($bytes in $frames){$writer.Write($bytes)}
$writer.Dispose();$file.Dispose()
Write-Output 'Created app.ico with 7 sizes and app-icon.png; vector design is in src/app.svg.'
