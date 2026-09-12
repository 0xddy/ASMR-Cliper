"""Build a clean, relocatable NVIDIA bundle. Never reuse the developer's venv."""
import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_support import download, extract_checked, sha256

MODEL_COMPONENTS = {'whisper', 'review', 'ast', 'qwen', 'aligner', 'clap'}
SOURCE_FOLDERS = ('engine', 'scripts', 'src', 'tests', 'third_party')
SOURCE_FILES = ('CMakeLists.txt', 'config/defaults.json', 'config/environment.json',
                'docs/模式提示词.md', 'docs/ENVIRONMENT.md', 'docs/BUILD_WIN64_NV.md',
                'docs/PORTABLE_README.md', 'docs/THIRD_PARTY_RUNTIME.md')
SOURCE_FILES += tuple('docs/prompts/' + name for name in ('strict-v2.txt', 'relaxed-v3.txt', 'extract-v4.txt'))
RUNTIME_FOLDERS = {'python', 'neural', 'tools'}
PTH = 'python312.zip\n.\nLib/site-packages\nimport site\n'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def inside(root, relative):
    path = (root / relative).resolve()
    if path == root.resolve() or not path.is_relative_to(root.resolve()):
        raise ValueError('Path escapes package root: ' + str(relative))
    return path


def assets(manifest):
    result = manifest['assets']
    if {a['component'] for a in result} != MODEL_COMPONENTS:
        raise ValueError('Full bundle requires all six model components.')
    seen = set()
    for asset in result:
        path = asset['path']
        if (PureWindowsPath(path).is_absolute() or PureWindowsPath(path).drive or
                '\\' in path or '..' in Path(path).parts or not path.startswith('models/') or path in seen):
            raise ValueError('Invalid/duplicate model path: ' + path)
        if not re.fullmatch(r'[0-9a-f]{64}', asset['sha256']) or asset['size'] <= 0:
            raise ValueError('Missing model size/SHA-256: ' + path)
        seen.add(path)
    return result


def portable_config(cfg):
    cfg = dict(cfg)
    for key in ('input', 'output'):
        cfg.pop(key, None)
    cfg.update(proxy_enabled=False, proxy_url='', device='auto',
               ffmpeg='runtime/tools/ffmpeg.exe', cache_dir='runtime/cache',
               whisper_model='models/whisper-review', review_model='models/whisper-review',
               ast_model='models/ast', speech_model='whisper-large-v3', review_model_id='whisper-large-v3')
    return cfg


def excluded(path):
    return ('__pycache__' in path.parts or path.suffix.lower() in ('.pyc', '.pyo', '.lnk') or
            path.name in ('user.json', 'history.json') or path.name.endswith(('.part', '.part.json')))


def prepare(source, root, python_archive, crt):
    if root.exists():
        raise ValueError('Package staging directory already exists.')
    root.mkdir(parents=True)
    for folder in SOURCE_FOLDERS:
        for file in (source / folder).rglob('*'):
            if file.is_file() and not excluded(file.relative_to(source)):
                if file.is_symlink() or not file.resolve().is_relative_to(source.resolve()):
                    raise ValueError('Source tree contains an external link: ' + str(file))
                target = inside(root, file.relative_to(source))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, target)
    for name in SOURCE_FILES:
        target = inside(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    save(root / 'config/defaults.json', portable_config(read(root / 'config/defaults.json')))
    manifest = read(root / 'config/environment.json')
    assets(manifest)
    if sha256(python_archive) != manifest['python']['sha256'] or python_archive.stat().st_size != manifest['python']['size']:
        raise ValueError('Embedded Python archive does not match the manifest.')
    extract_checked(python_archive, root / 'runtime/python')
    # Bootstrap Python runs this phase; the staged interpreter must not load these DLLs yet.
    copy_crt(crt, root / 'runtime/python')
    (root / 'runtime/python/python312._pth').write_text(PTH, encoding='ascii')
    (root / 'runtime/python/Lib/site-packages').mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'docs/PORTABLE_README.md', root / 'README.md')
    shutil.copy2(source / 'docs/THIRD_PARTY_RUNTIME.md', root / 'THIRD_PARTY_RUNTIME.md')
    save(root / 'build-info.json', {'product': 'ASMR-Cliper', 'target': 'win64-nv',
         'version': version(source), 'commit': os.environ.get('GITHUB_SHA', ''),
         'run_id': os.environ.get('GITHUB_RUN_ID', ''), 'built_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
         'models': assets(manifest), 'python': manifest['python'], 'pip': manifest['pip']})


def version(source):
    match = re.search(r'project\(ASMRCLIP VERSION (\d+\.\d+\.\d+)\b', (source / 'CMakeLists.txt').read_text(encoding='utf8'))
    if not match:
        raise ValueError('Cannot read the application version from CMakeLists.txt.')
    return match[1]


def clean_env(root, offline=False):
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'pip_proxy',
                           'pythonpath', 'pythonhome', 'pip_index_url', 'pip_extra_index_url', 'asmrclip_build_proxy') or key.upper().startswith('CUDA_PATH'):
            env.pop(key, None)
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', PIP_NO_CACHE_DIR='1',
               PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_CONFIG_FILE=os.devnull,
               HF_HOME=str(root / 'runtime/cache/huggingface'), TRANSFORMERS_VERBOSITY='error')
    proxy = os.environ.get('ASMRCLIP_BUILD_PROXY', '')
    if proxy and not offline:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, PIP_PROXY=proxy)
    if offline:
        # Do not accidentally satisfy imports/DLLs from the builder's Python or CUDA installation.
        system = Path(os.environ['SystemRoot'])
        env['PATH'] = os.pathsep.join(str(p) for p in (system / 'System32', system))
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
    return env


def assert_core_python(root):
    if Path(sys.executable).resolve() != (root / 'runtime/python/python.exe').resolve():
        raise RuntimeError('Run this phase with the staged runtime/python/python.exe, not a system Python or venv.')


def copy_crt(crt, target):
    files = list(crt.glob('*.dll'))
    if not {'msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'} <= {p.name.lower() for p in files}:
        raise ValueError('Incomplete x64 Microsoft.VC143.CRT directory.')
    target.mkdir(parents=True, exist_ok=True)
    for file in files:
        shutil.copy2(file, target / file.name)


def install(root, crt):
    assert_core_python(root)
    import environment_manager as manager
    import neural_environment
    manager.ROOT = root
    manager.CONFIG = portable_config(read(root / 'config/defaults.json'))
    proxy = os.environ.get('ASMRCLIP_BUILD_PROXY', '')
    if proxy:
        manager.CONFIG.update(proxy_enabled=True, proxy_url=proxy)
    manifest = read(root / 'config/environment.json')
    # Core CRT was staged by prepare, before this interpreter loaded and locked its DLLs.
    # Neither environment inherits a system pip index/cache configuration.
    env = clean_env(root)
    manager.child_environment = lambda for_download=True: clean_env(root, offline=not for_download)
    manager.install_dependencies(manifest)
    # Hosted runners have no NVIDIA device; never infer which torch wheel to install from GPU detection.
    neural_environment.install(root, manifest, manager.file_download, extract_checked, manager.emit, env,
                               use_cuda=True, prepare_runtime=lambda folder: copy_crt(crt, folder))
    manager.install_ffmpeg(manifest)
    for asset in assets(manifest):
        print('Model: ' + asset['path'], flush=True)
        last = [-1]
        def progress(done, total, speed, cached):
            percent = int(done * 100 / total) if total else 0
            if percent // 10 != last[0]:
                print(f'  {percent}% ({done / 1048576:.0f} MiB)', flush=True)
                last[0] = percent // 10
        download(asset['url'], inside(root, asset['path']), manager.CONFIG, asset['sha256'], asset['size'], progress)
    info = read(root / 'build-info.json')
    info['ffmpeg'] = {**manifest['ffmpeg'], 'archive_sha256': sha256(root / 'runtime/downloads/ffmpeg-essentials.zip')}
    info['crt'] = [{'name': p.name, 'sha256': sha256(p)} for p in sorted(crt.glob('*.dll'))]
    info['packages'] = {}
    for name in ('python', 'neural'):
        run = subprocess.run([str(root / f'runtime/{name}/python.exe'), '-m', 'pip', 'list', '--format=json'],
                             check=True, capture_output=True, text=True, encoding='utf8', env=env, timeout=120)
        info['packages'][name] = json.loads(run.stdout)
    save(root / 'build-info.json', info)


CORE_PROBE = r'''
import sys, json, importlib, importlib.metadata, struct
from pathlib import Path
root = Path(sys.argv[1])
assert sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8
sys.path.insert(0, str(root / 'engine'))
from asmrclip.common import configure_dlls
configure_dlls()
for name in ('av','numpy','scipy','faster_whisper','ctranslate2','onnxruntime','transformers'):
    importlib.import_module(name)
for line in (root / 'engine/requirements.txt').read_text().splitlines():
    if '==' in line:
        name, expected = line.strip().split('==')
        assert importlib.metadata.version(name) == expected, name
import onnxruntime as ort
assert ort.__version__ == '1.23.2'
assert 'CUDAExecutionProvider' in ort.get_available_providers()
for component in ('cublas','cudnn','cuda_nvrtc'):
    assert list((Path(sys.prefix) / 'Lib/site-packages/nvidia' / component).rglob('*.dll')), component
print(json.dumps({'python':sys.version.split()[0], 'onnxruntime':ort.__version__, 'providers':ort.get_available_providers()}))
'''

NEURAL_PROBE = r'''
import sys, json, importlib.metadata, struct
assert sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8
import torch, transformers, numpy, scipy, librosa, nagisa, soynlp
from qwen_asr import Qwen3ASRModel
from transformers import ClapModel
assert torch.__version__.split('+')[0] == '2.9.1' and torch.version.cuda == '12.8'
assert transformers.__version__ == '4.57.6'
assert importlib.metadata.version('qwen-asr') == '0.0.6'
print(json.dumps({'python':sys.version.split()[0], 'torch':torch.__version__, 'cuda_build':torch.version.cuda,
                  'gpu_available_on_builder':torch.cuda.is_available(), 'qwen_asr':importlib.metadata.version('qwen-asr')}))
'''


def verify(root, report):
    assert_core_python(root)
    report.parent.mkdir(parents=True, exist_ok=True)
    manifest = read(root / 'config/environment.json')
    cfg = read(root / 'config/defaults.json')
    if cfg != portable_config(cfg):
        raise ValueError('Package contains non-portable default settings.')
    result = {'models': [], 'runtimes': {}, 'gpu_inference_tested': False}
    for asset in assets(manifest):
        path = inside(root, asset['path'])
        if path.stat().st_size != asset['size'] or sha256(path) != asset['sha256']:
            raise ValueError('Model checksum mismatch: ' + asset['path'])
        result['models'].append(asset['path'])
    env = clean_env(root, offline=True)
    for name, probe in (('python', CORE_PROBE), ('neural', NEURAL_PROBE)):
        folder = root / 'runtime' / name
        if (folder / 'python312._pth').read_text(encoding='utf8').replace('\r\n', '\n') != PTH:
            raise ValueError('Embedded Python paths are not relative.')
        if (folder / 'pyvenv.cfg').exists():
            raise ValueError('A venv must not be shipped as a portable runtime.')
        for dll in ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'):
            if not (folder / dll).is_file():
                raise ValueError('Missing app-local CRT: ' + name + '/' + dll)
        run = subprocess.run([str(folder / 'python.exe'), '-X', 'utf8', '-c', probe, str(root)],
                             capture_output=True, text=True, encoding='utf8', errors='replace', env=env, cwd=root, timeout=180)
        if run.returncode:
            raise RuntimeError(name + ' offline import check failed:\n' + run.stdout + run.stderr)
        result['runtimes'][name] = json.loads(run.stdout.strip().splitlines()[-1])
    ffmpeg = subprocess.run([str(root / 'runtime/tools/ffmpeg.exe'), '-version'], check=True,
                            capture_output=True, text=True, encoding='utf8', env=env, timeout=30)
    result['ffmpeg'] = ffmpeg.stdout.splitlines()[0]
    gui_report = report.with_name('portable-gui-controls.json')
    # Native --test-controls exits itself. No hidden background app remains after validation.
    subprocess.run([str(root / 'asmrcliper.exe'), '--test-controls', str(gui_report)], check=True, env=env, cwd=root, timeout=60)
    if not read(gui_report)['passed']:
        raise RuntimeError('Relocated GUI control checks failed.')
    result['passed'] = True
    save(report, result)
    info = read(root / 'build-info.json')
    info['verification'] = result
    save(root / 'build-info.json', info)
    print('Portable offline validation passed (CUDA build checked; no GPU inference on hosted runner).', flush=True)


def package_files(root):
    for file in sorted(root.rglob('*')):
        relative = file.relative_to(root)
        if not file.is_file() or excluded(relative):
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
            raise ValueError('External link in package: ' + str(relative))
        if relative.parts[0] in ('build', 'bin', 'output', '.git', '.vs'):
            continue
        if relative.parts[0] == 'runtime' and (len(relative.parts) < 3 or relative.parts[1] not in RUNTIME_FOLDERS):
            continue
        # pip's generated console launchers contain the original interpreter path.
        # The application always invokes `python -m pip` / explicit Python scripts.
        if relative.parts[0] == 'runtime' and len(relative.parts) > 2 and relative.parts[2] == 'Scripts':
            continue
        yield file, relative


def archive(root, output):
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError('Archive output must be outside the package directory.')
    info = read(root / 'build-info.json')
    if not info.get('verification', {}).get('passed'):
        raise ValueError('Refusing to publish a bundle without successful portable verification.')
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"ASMR-Cliper-{info['version']}-win64-nv.zip"
    if target.exists():
        raise FileExistsError('Archive already exists: ' + str(target))
    files = list(package_files(root))
    # ZIP can be larger than the source for incompressible files. Keep an explicit margin.
    needed = sum(file.stat().st_size for file, _ in files) + 1024**3
    if shutil.disk_usage(output).free < needed:
        raise RuntimeError(f'Archive volume needs {needed / 1024**3:.1f} GiB free.')
    temp = target.with_suffix('.zip.part')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as package:
        for file, relative in files:
            package.write(file, 'ASMR-Cliper/' + relative.as_posix())
    with zipfile.ZipFile(temp) as package:
        bad = package.testzip()
        if bad:
            raise ValueError('Archive CRC validation failed: ' + bad)
    temp.rename(target)
    checksum = sha256(target)
    target.with_suffix('.zip.sha256').write_text(f'{checksum}  {target.name}\n', encoding='ascii')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf8') as handle:
            handle.write(f'archive={target}\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as handle:
            handle.write(f'### ASMR-Cliper win64-nv\n\n- Package: `{target.name}` ({target.stat().st_size / 1024**3:.2f} GiB)\n'
                         f'- SHA-256: `{checksum}`\n- Includes both Python runtimes, CUDA dependencies, FFmpeg and all six model components.\n'
                         '- Download the ZIP artifact and extract it; launch `ASMR-Cliper/asmrcliper.exe`.\n'
                         '- Build, regression tests, relocation/import checks, model SHA-256 and archive CRC checks passed.\n'
                         '- GPU inference requires the destination machine\'s NVIDIA driver and was not tested on this runner.\n')
    print('Created ' + str(target), flush=True)


def snapshot(root, output):
    """Upload clean payload files; hard links avoid duplicating model storage."""
    if output.exists() or output.resolve().is_relative_to(root.resolve()):
        raise ValueError('Snapshot requires a new directory outside the payload.')
    output.mkdir(parents=True)
    for file, relative in package_files(root):
        target = inside(output, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(file, target)
        except OSError:
            shutil.copy2(file, target)
    print('Download snapshot ready: ' + str(output), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'install', 'verify', 'archive', 'snapshot'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--python-archive', type=Path)
    parser.add_argument('--crt', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    required = {'prepare': ('source', 'python_archive', 'crt'), 'install': ('crt',), 'verify': ('report',), 'archive': ('output',), 'snapshot': ('output',)}
    for name in required[args.phase]:
        if getattr(args, name) is None:
            parser.error('--' + name.replace('_', '-') + ' is required for ' + args.phase)
    root = args.root.resolve()
    if args.phase == 'prepare': prepare(args.source.resolve(), root, args.python_archive, args.crt.resolve())
    elif args.phase == 'install': install(root, args.crt.resolve())
    elif args.phase == 'verify': verify(root, args.report.resolve())
    elif args.phase == 'snapshot': snapshot(root, args.output.resolve())
    else: archive(root, args.output.resolve())


if __name__ == '__main__':
    main()
