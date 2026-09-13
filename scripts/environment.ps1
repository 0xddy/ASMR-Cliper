param(
    [ValidateSet('inspect','install','testproxy')][string]$Action = 'inspect',
    [string]$Config = '',
    [ValidateSet('all','python','dependencies','whisper','ast','review','ffmpeg','qwen','aligner','clap','neural')][string]$Component = 'all',
    [string]$Root = (Split-Path $PSScriptRoot -Parent)
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = [IO.Path]::GetFullPath($Root)
function Emit($Kind, $Message, $Extra = @{}) {
    $data = @{type=$Kind; message=$Message}
    foreach ($key in $Extra.Keys) { $data[$key] = $Extra[$key] }
    [Console]::WriteLine(($data | ConvertTo-Json -Compress -Depth 10))
}
try {
    if (-not $Config) { $Config = Join-Path $Root 'config/defaults.json' }
    $cfg = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    $manifest = Get-Content -LiteralPath (Join-Path $Root 'config/environment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $proxy = $null
    if ($cfg.proxy_enabled -and $Action -ne 'inspect') {
        $uri = $null
        if (-not [Uri]::TryCreate($cfg.proxy_url, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @('http','https') -or -not $uri.Host) { throw '代理地址无效，请填写 http://主机:端口。' }
        $proxy = New-Object Net.WebProxy($uri)
        if ($uri.UserInfo) {
            $parts = $uri.UserInfo.Split(':',2)
            $password = ''; if ($parts.Count -gt 1) { $password = [Uri]::UnescapeDataString($parts[1]) }
            $proxy.Credentials = New-Object Net.NetworkCredential([Uri]::UnescapeDataString($parts[0]),$password)
        }
    }
    function Request($Url) {
        $req = [Net.HttpWebRequest]::Create($Url)
        $req.Proxy = $proxy; $req.Timeout = 25000; $req.ReadWriteTimeout = 25000
        $req.UserAgent = 'ASMRCLIP/0.2'
        return $req
    }
    if ($Action -eq 'testproxy') {
        $results = @(); $passed = $true
        foreach ($endpoint in @(@('Python','https://www.python.org/ftp/python/3.12.10/'),@('PyPI','https://pypi.org/pypi/pip/json'),@('Hugging Face','https://huggingface.co/api/models/Xenova/ast-finetuned-audioset-10-10-0.4593'))) {
            $timer = [Diagnostics.Stopwatch]::StartNew(); $ok = $false; $detail = ''
            try { $response = (Request $endpoint[1]).GetResponse(); $response.Close(); $ok = $true; $detail = '连接成功' }
            catch { $passed = $false; $detail = $_.Exception.Message }
            $results += @{name=$endpoint[0]; ok=$ok; milliseconds=$timer.ElapsedMilliseconds; detail=$detail}
            Emit 'log' ($endpoint[0] + '：' + $detail)
        }
        Emit 'proxy_result' $(if ($passed) {'下载连接测试通过。'} else {'部分下载站点无法连接，请检查代理或重试。'}) @{ok=$passed; results=$results; progress=100}
        if ($passed) { exit 0 } else { exit 2 }
    }
    if ($Action -eq 'install') {
        [IO.Directory]::CreateDirectory((Join-Path $Root 'runtime')) | Out-Null
        try { $bootstrapLock = [IO.File]::Open((Join-Path $Root 'runtime/environment-bootstrap.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None) }
        catch { throw '已有另一个环境安装任务正在运行。' }
    }
    $python = $null
    foreach ($candidate in @((Join-Path $Root 'runtime/venv/Scripts/python.exe'), (Join-Path $Root 'runtime/python/python.exe'))) {
        if (Test-Path -LiteralPath $candidate) {
            try { $version = & $candidate -I -c 'import sys; print(sys.version_info.major,sys.version_info.minor)' 2>$null; if ($LASTEXITCODE -eq 0 -and $version -eq '3 12') { $python=$candidate; break } } catch {}
        }
    }
    if (-not $python -and $Action -eq 'inspect') {
        $components = @{}
        $requiredComponents = @('python','dependencies','ast','ffmpeg')
        $modelComponents = @{'whisper-large-v3'='review'; 'whisper-turbo'='whisper'; 'qwen3-asr'='qwen'}
        $primaryModel = if ($cfg.speech_model) { $cfg.speech_model } else { 'whisper-large-v3' }
        $reviewModel = if ($cfg.review_model_id) { $cfg.review_model_id } else { 'whisper-large-v3' }
        $requiredComponents += $modelComponents[$primaryModel]
        $reviewRequired = $cfg.review_enabled -ne $false -or $cfg.mode -eq 'extract'
        if ($reviewRequired) { $requiredComponents += $modelComponents[$reviewModel] }
        if ($requiredComponents -contains 'qwen') { $requiredComponents += @('aligner','neural') }
        if ($cfg.mode -eq 'extract' -or $cfg.keep_drinking -ne $true -or $cfg.keep_whisper -ne $false -or ($reviewRequired -and $reviewModel -eq 'qwen3-asr')) {
            $requiredComponents += @('clap','neural')
        }
        foreach ($entry in @(@('python','Python 运行时'),@('dependencies','分析依赖'),@('whisper','Whisper Turbo'),@('review','Whisper large-v3'),@('qwen','Qwen3-ASR-1.7B'),@('aligner','Qwen 时间定位'),@('clap','ASMR 声音识别'),@('neural','Qwen / ASMR 识别依赖'),@('ast','声音分类模型'),@('ffmpeg','FFmpeg'),@('gpu','GPU 加速'))) {
            $state = @{id=$entry[0]; name=$entry[1]; status='missing'; detail='安装 Python 后检查并复用已有组件'; required=($requiredComponents -contains $entry[0])}
            if ($entry[0] -eq 'python') { $state.detail = '缺少可用的项目 Python 3.12 运行时' }
            if ($entry[0] -eq 'gpu') { $state.status = 'optional'; $state.detail = '可选，安装后检测' }
            $components[$entry[0]]=$state; Emit 'component' '' $state
        }
        Emit 'environment' '需要安装运行环境，点击「补齐环境」。' @{ready=$false; components=$components; progress=100}
        exit 0
    }
    if (-not $python) {
        Emit 'progress' '正在下载项目专用 Python 运行时…' @{progress=1}
        $downloadDir = Join-Path $Root 'runtime/downloads'
        [IO.Directory]::CreateDirectory($downloadDir) | Out-Null
        $zipPath = Join-Path $downloadDir 'python-3.12.10-embed-amd64.zip'
        $partPath = $zipPath + '.part'
        $valid = (Test-Path -LiteralPath $zipPath) -and ((Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash -eq $manifest.python.sha256)
        if (-not $valid) {
            $response = (Request $manifest.python.url).GetResponse()
            try {
                $stream = $response.GetResponseStream(); $target = [IO.File]::Open($partPath,[IO.FileMode]::Create)
                try {
                    $buffer = New-Object byte[] 262144; $done = [long]0; $total = $response.ContentLength
                    $timer = [Diagnostics.Stopwatch]::StartNew(); $last = [long]-1000
                    while (($count = $stream.Read($buffer,0,$buffer.Length)) -gt 0) {
                        $target.Write($buffer,0,$count); $done += $count
                        if ($timer.ElapsedMilliseconds-$last -gt 400) { $last=$timer.ElapsedMilliseconds; Emit 'download' '下载 Python 运行时' @{file='Python 3.12'; bytes=$done; total=$total; speed=($done/[Math]::Max(1,$timer.Elapsed.TotalSeconds)); progress=([Math]::Min(7,1+6*$done/[Math]::Max(1,$total)))} }
                    }
                } finally { $target.Dispose(); $stream.Dispose() }
            } finally { $response.Close() }
            if ((Get-FileHash -LiteralPath $partPath -Algorithm SHA256).Hash -ne $manifest.python.sha256) { throw 'Python 下载文件校验失败，请重试。' }
            Move-Item -LiteralPath $partPath -Destination $zipPath -Force
        }
        $pythonDir = Join-Path $Root 'runtime/python'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Directory]::CreateDirectory($pythonDir) | Out-Null
        $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
        try {
            foreach ($entry in $archive.Entries) {
                $destination = [IO.Path]::GetFullPath((Join-Path $pythonDir $entry.FullName))
                if (-not $destination.StartsWith($pythonDir + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw '压缩包包含无效路径。' }
                if (-not $entry.Name) { [IO.Directory]::CreateDirectory($destination) | Out-Null; continue }
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$destination,$true)
            }
        } finally { $archive.Dispose() }
        [IO.Directory]::CreateDirectory((Join-Path $pythonDir 'Lib/site-packages')) | Out-Null
        [IO.File]::WriteAllText((Join-Path $pythonDir 'python312._pth'), "python312.zip`r`n.`r`nLib/site-packages`r`nimport site`r`n",[Text.Encoding]::ASCII)
        $python = Join-Path $pythonDir 'python.exe'
        Emit 'component' 'Python 运行时已安装。' @{id='python'; name='Python 运行时'; status='ready'; detail='Python 3.12.10 · 项目专用'; required=$true}
    }
    & $python -X utf8 -u (Join-Path $PSScriptRoot 'environment_manager.py') --action $Action --config $Config --root $Root --component $Component
    exit $LASTEXITCODE
} catch {
    $message = $_.Exception.Message
    if ($cfg -and $cfg.proxy_url) { $message = $message.Replace($cfg.proxy_url,'[已配置的代理]') }
    Emit 'error' $message
    exit 1
}

finally { if ($bootstrapLock) { $bootstrapLock.Dispose() } }
