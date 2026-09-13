"""Package the Windows application with FFmpeg, without Python or model environments."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import zipfile

from download_support import download, fetch_text, sha256
from package_win64_nv import clean_env, inside, portable_config, read, save, version

SOURCE_FILES = (
    'engine/requirements.txt', 'engine/neural-requirements.txt',
    'config/defaults.json', 'config/environment.json',
    'scripts/environment.ps1', 'scripts/setup-runtime.ps1',
    'scripts/environment_manager.py', 'scripts/download_support.py', 'scripts/neural_environment.py',
    'docs/ENVIRONMENT.md', 'docs/模式提示词.md',
    'docs/prompts/strict-v2.txt', 'docs/prompts/relaxed-v3.txt', 'docs/prompts/extract-v4.txt',
    'third_party/nlohmann/LICENSE.MIT',
    'third_party/octicons/LICENSE.MIT',
)
TOOL_FILES = ('ffmpeg.exe', 'ffprobe.exe', 'FFmpeg-LICENSE.txt', 'FFmpeg-README.txt')


def package_files(root):
    """An allowlist prevents local environments and GUI test state from entering the ZIP."""
    names = set(SOURCE_FILES) | {'README.md', 'asmrcliper.exe', 'build-info.json'}
    names.update('runtime/tools/' + name for name in TOOL_FILES)
    for pattern in ('engine/*.py', 'engine/asmrclip/*.py'):
        names.update(file.relative_to(root).as_posix() for file in root.glob(pattern))
    for name in sorted(names):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('External link in application package: ' + name)
        if not path.is_file():
            raise ValueError('Missing application package file: ' + name)
        yield path, name


def prepare(source, native, root):
    build = read(native / 'build-source.json')
    commit = os.environ.get('GITHUB_SHA', '')
    if (not re.fullmatch(r'[0-9a-f]{40}', commit) or build.get('commit') != commit or
            build.get('version') != version(source)):
        raise ValueError('Native executable must belong to this exact source revision and version.')
    if sha256(native / 'asmrcliper.exe') != build.get('exe_sha256', '').lower():
        raise ValueError('Native executable SHA-256 mismatch.')
    if root.exists():
        raise ValueError('Application staging directory already exists.')
    root.mkdir(parents=True)
    names = set(SOURCE_FILES) | {'engine/main.py', 'engine/neural_worker.py', 'engine/asmrclip/__init__.py'}
    for pattern in ('engine/*.py', 'engine/asmrclip/*.py'):
        names.update(file.relative_to(source).as_posix() for file in source.glob(pattern))
    for name in sorted(names):
        original = inside(source, name)
        if (source / name).is_symlink():
            raise ValueError('Source file is a link: ' + name)
        target = inside(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    shutil.copy2(native / 'asmrcliper.exe', root / 'asmrcliper.exe')
    shutil.copy2(source / 'docs/APP_README.md', root / 'README.md')
    save(root / 'config/defaults.json', portable_config(read(root / 'config/defaults.json')))
    save(root / 'build-info.json', {
        'product': 'ASMR-Cliper', 'target': 'win64-app', 'version': version(source), 'commit': commit,
        'run_id': os.environ.get('GITHUB_RUN_ID', ''), 'exe_sha256': build['exe_sha256'].lower(),
        'built_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'includes_python': False, 'includes_models': False,
    })


def install_ffmpeg(root, downloads):
    manifest = read(root / 'config/environment.json')['ffmpeg']
    proxy = os.environ.get('ASMRCLIP_BUILD_PROXY', '')
    cfg = {'proxy_enabled': bool(proxy), 'proxy_url': proxy}
    checksum = fetch_text(manifest['checksum_url'], cfg, 8192).strip().split()[0].lower()
    if not re.fullmatch(r'[0-9a-f]{64}', checksum):
        raise ValueError('Invalid FFmpeg publisher checksum.')
    print('Downloading and verifying FFmpeg essentials (no Python or models).', flush=True)
    archive = download(manifest['url'], downloads / 'ffmpeg-essentials.zip', cfg, expected_hash=checksum)
    destination = root / 'runtime/tools'
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        executables = [name for name in package.namelist() if name.endswith('/bin/ffmpeg.exe')]
        if len(executables) != 1:
            raise ValueError('Expected exactly one FFmpeg executable in the publisher archive.')
        prefix = executables[0].removesuffix('bin/ffmpeg.exe')
        for original, name in (('bin/ffmpeg.exe', 'ffmpeg.exe'), ('bin/ffprobe.exe', 'ffprobe.exe'),
                               ('LICENSE', 'FFmpeg-LICENSE.txt'), ('README.txt', 'FFmpeg-README.txt')):
            if package.namelist().count(prefix + original) != 1:
                raise ValueError('Missing or duplicate FFmpeg archive member: ' + original)
            with package.open(prefix + original) as source, (destination / name).open('xb') as target:
                shutil.copyfileobj(source, target)
    info = read(root / 'build-info.json')
    info['ffmpeg'] = {**manifest, 'archive_sha256': checksum,
                      'files': {name: sha256(destination / name) for name in TOOL_FILES}}
    save(root / 'build-info.json', info)


def verify(root, reports):
    reports.mkdir(parents=True, exist_ok=True)
    env = clean_env(root, offline=True)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE

    def run(args):
        return subprocess.run(list(map(str, args)), check=True, capture_output=True, text=True,
                              encoding='utf8', errors='replace', env=env, cwd=root, timeout=60,
                              startupinfo=startup)

    # Parse source files without importing third-party modules or generating bytecode.
    for path, name in package_files(root):
        if path.suffix == '.py':
            compile(path.read_bytes(), name, 'exec')
    if read(root / 'config/defaults.json') != portable_config(read(root / 'config/defaults.json')):
        raise ValueError('Application defaults contain machine-specific settings.')
    info = read(root / 'build-info.json')
    result = {'engine_inference_tested': False, 'tools': {}}
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        binary = root / 'runtime/tools' / name
        if sha256(binary) != info['ffmpeg']['files'][name]:
            raise ValueError('FFmpeg binary checksum changed: ' + name)
        result['tools'][name] = run([binary, '-version']).stdout.splitlines()[0]
    audio = reports / 'ffmpeg-smoke.wav'
    run([root / 'runtime/tools/ffmpeg.exe', '-hide_banner', '-v', 'error', '-nostdin', '-y',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.1', '-c:a', 'pcm_s16le', audio])
    probe = json.loads(run([root / 'runtime/tools/ffprobe.exe', '-v', 'error',
                           '-show_streams', '-of', 'json', audio]).stdout)
    if not any(stream.get('codec_name') == 'pcm_s16le' for stream in probe.get('streams', [])):
        raise RuntimeError('FFmpeg/ffprobe audio smoke test failed.')
    gui_report = reports / 'app-gui-controls.json'
    run([root / 'asmrcliper.exe', '--test-controls', gui_report])
    if read(gui_report).get('passed') is not True:
        raise RuntimeError('Relocated application GUI checks failed.')
    result.update(passed=True, gui_controls=True, ffmpeg_audio=True)
    save(reports / 'app-validation.json', result)
    info['verification'] = result
    save(root / 'build-info.json', info)


def archive(root, output):
    info = read(root / 'build-info.json')
    if info.get('target') != 'win64-app' or info.get('verification', {}).get('passed') is not True:
        raise ValueError('Only a verified application package can be archived.')
    output.mkdir(parents=True, exist_ok=True)
    target = output / f'ASMR-Cliper-{info["version"]}-win64-app.zip'
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for path, name in package_files(root):
            package.write(path, 'ASMR-Cliper/' + name)
    with zipfile.ZipFile(target) as package:
        if package.testzip() is not None:
            raise ValueError('Application ZIP failed its CRC check.')
    target.with_suffix('.zip.sha256').write_text(f'{sha256(target)}  {target.name}\n', encoding='ascii')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as handle:
            handle.write(f'archive={target}\narchive_name={target.name}\n')
    print('Verified application ZIP: ' + str(target), flush=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    root = work / 'payload/ASMR Cliper 程序'
    prepare(args.source.resolve(), work / 'native', root)
    install_ffmpeg(root, work / 'downloads')
    verify(root, work / 'reports')
    archive(root, work / 'artifacts')


if __name__ == '__main__':
    main()
