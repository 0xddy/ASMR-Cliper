"""Publish a verified portable ZIP as resumable, size-limited GitHub Release assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from download_support import sha256

ASSET_LIMIT = 2 * 1024**3
PART_SIZE = 1900 * 1024**2
BUFFER_SIZE = 4 * 1024**2


def gh(*args, allow_missing=False, timeout=120):
    result = subprocess.run(['gh', *map(str, args)], capture_output=True, text=True,
                            encoding='utf8', errors='replace', timeout=timeout)
    if result.returncode:
        if allow_missing and (result.stderr.strip() == 'release not found' or '(HTTP 404)' in result.stderr):
            return None
        raise RuntimeError('GitHub CLI failed: ' + result.stderr.strip())
    return result.stdout


def read_release(repository, tag):
    # gh also finds drafts by their pending tag; REST /releases/tags only finds published tags.
    value = gh('release', 'view', tag, '--repo', repository, '--json', 'databaseId', allow_missing=True)
    if value is None:
        return None
    release_id = json.loads(value)['databaseId']
    return json.loads(gh('api', f'repos/{repository}/releases/{release_id}'))


def asset_info(path):
    return {'name': path.name, 'size': path.stat().st_size, 'sha256': sha256(path)}


def matches(remote, local):
    return (remote.get('state') == 'uploaded' and remote.get('size') == local['size'] and
            remote.get('digest') == 'sha256:' + local['sha256'])


def upload(repository, tag, path, expected):
    """Only replace incomplete assets in this run's draft; published releases are immutable."""
    if not 0 < expected['size'] < ASSET_LIMIT:
        raise ValueError('Release asset must be nonempty and smaller than 2 GiB: ' + path.name)
    for attempt in range(3):
        release = read_release(repository, tag)
        remote = next((a for a in release['assets'] if a['name'] == path.name), {})
        if matches(remote, expected):
            print('Verified existing asset: ' + path.name, flush=True)
            return
        if not release['draft']:
            raise RuntimeError('Refusing to change an already published release: ' + tag)
        print('Uploading ' + path.name, flush=True)
        try:
            gh('release', 'upload', tag, path, '--repo', repository, '--clobber', timeout=1800)
        except (RuntimeError, subprocess.TimeoutExpired):
            # A disconnected response can still mean the upload completed. Verify before retrying.
            if attempt == 2:
                current = read_release(repository, tag)
                if any(a['name'] == path.name and matches(a, expected) for a in current['assets']):
                    return
                raise
        else:
            current = read_release(repository, tag)
            if any(a['name'] == path.name and matches(a, expected) for a in current['assets']):
                return
            if attempt == 2:
                raise RuntimeError('Uploaded asset SHA-256/size verification failed: ' + path.name)
        time.sleep(3 * (attempt + 1))


def split_archive(archive, folder, part_size):
    """Keep only one temporary part on disk at a time, including while resuming uploads."""
    with archive.open('rb') as source:
        for index in range(1, (archive.stat().st_size + part_size - 1) // part_size + 1):
            part = folder / f'{archive.name}.{index:03d}'
            digest = hashlib.sha256()
            size = 0
            created = False
            try:
                with part.open('xb') as output:
                    created = True
                    while size < part_size:
                        block = source.read(min(BUFFER_SIZE, part_size - size))
                        if not block:
                            break
                        output.write(block)
                        digest.update(block)
                        size += len(block)
                yield part, {'name': part.name, 'size': size, 'sha256': digest.hexdigest()}
            finally:
                if created:
                    part.unlink(missing_ok=True)


def validate_archive(archive, checksum, commit, target='win64-nv'):
    if target not in ('win64-nv', 'win64-app'):
        raise ValueError('Unknown package target.')
    match = re.fullmatch(r'ASMR-Cliper-(\d+\.\d+\.\d+)-' + re.escape(target) + r'\.zip', archive.name)
    if not match:
        raise ValueError('Unexpected portable archive filename.')
    expected = checksum.read_text(encoding='utf-8-sig').strip().split()
    if len(expected) != 2 or expected[1] != archive.name or not re.fullmatch(r'[0-9a-f]{64}', expected[0]):
        raise ValueError('Invalid archive SHA-256 file.')
    if sha256(archive) != expected[0]:
        raise ValueError('Archive SHA-256 mismatch.')
    with zipfile.ZipFile(archive) as package:
        info = json.loads(package.read('ASMR-Cliper/build-info.json'))
        if target == 'win64-app':
            names = set(package.namelist())
            required = {'ASMR-Cliper/engine/main.py', 'ASMR-Cliper/asmrcliper.exe',
                        'ASMR-Cliper/runtime/tools/ffmpeg.exe', 'ASMR-Cliper/runtime/tools/ffprobe.exe'}
            allowed_tools = {'ffmpeg.exe', 'ffprobe.exe', 'FFmpeg-LICENSE.txt', 'FFmpeg-README.txt'}
            unwanted = any(name.startswith('ASMR-Cliper/models/') or
                           (name.startswith('ASMR-Cliper/runtime/') and
                            name not in {'ASMR-Cliper/runtime/tools/' + tool for tool in allowed_tools})
                           for name in names)
            if (info.get('target') != target or info.get('includes_python') is not False or
                    info.get('includes_models') is not False or not required <= names or unwanted):
                raise ValueError('Application package must contain FFmpeg and exclude Python/model environments.')
    if (info.get('version') != match[1] or info.get('commit') != commit or
            info.get('verification', {}).get('passed') is not True):
        raise ValueError('Archive must be verified and belong to this exact source revision.')
    return match[1], expected[0]


def publish(archive, checksum, repository, commit, run_id, part_size=PART_SIZE, *, target='win64-nv'):
    archive, checksum = archive.resolve(), checksum.resolve()
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('An explicit repository and full source commit are required.')
    if not re.fullmatch(r'[1-9][0-9]*', run_id) or not 0 < part_size < ASSET_LIMIT:
        raise ValueError('Invalid workflow run ID or part size.')
    version, archive_hash = validate_archive(archive, checksum, commit, target)
    app_only = target == 'win64-app'
    if app_only and archive.stat().st_size >= ASSET_LIMIT:
        raise ValueError('Application ZIP must be smaller than 2 GiB.')
    count = (archive.stat().st_size + part_size - 1) // part_size
    if count > 990:
        raise ValueError('Too many Release assets.')
    if not app_only and shutil.disk_usage(archive.parent).free < min(part_size, archive.stat().st_size) + 128 * 1024**2:
        raise RuntimeError('Release staging needs space for one part plus 128 MiB.')
    tag = f'v{version}-{target}-{run_id}'
    manifest = {'format_version': 1, 'archive': {'name': archive.name, 'size': archive.stat().st_size,
                                               'sha256': archive_hash}, 'parts': []}
    with tempfile.TemporaryDirectory(prefix='release-assets-', dir=archive.parent) as temp:
        folder = Path(temp).resolve()
        # TemporaryDirectory only removes this newly created, bounded staging directory.
        if folder.parent != archive.parent:
            raise ValueError('Release staging escaped the archive directory.')
        notes = folder / 'release-notes.md'
        if app_only:
            notes.write_text(
                'Windows x64 程序包，包含程序、引擎脚本、FFmpeg 和 ffprobe。'
                '不包含 Python、推理依赖或模型。\n\n'
                f'下载并完整解压 `{archive.name}`，运行 `ASMR-Cliper\\asmrcliper.exe`。'
                '无需合并分卷。首次使用时，在「运行环境」点击「补齐环境」安装分析依赖与模型。\n\n'
                '构建已测试 GUI、FFmpeg 和 ffprobe；未执行需要模型的剪辑推理。\n\n'
                f'- ZIP SHA-256：`{archive_hash}`\n'
                f'- 源码提交：`{commit}`\n'
                f'- [构建日志与测试报告](https://github.com/{repository}/actions/runs/{run_id})\n',
                encoding='utf8')
        else:
            notes.write_text(
                f'Windows x64 完整集成包，包含 Python、NVIDIA 推理依赖、FFmpeg 和全部模型。\n\n'
                f'## 下载与使用\n\n'
                f'1. 下载全部 **{count} 个** `{archive.name}.001` 等分卷、`{archive.name}.parts.json` '
                '和 `merge-win64-nv.ps1`，放到同一个目录。\n'
                '2. 在该目录打开 PowerShell，运行：\n\n'
                '```powershell\npowershell -NoProfile -ExecutionPolicy Bypass -File .\\merge-win64-nv.ps1\n```\n\n'
                '3. 脚本会校验每个分卷和合并后的 ZIP。完整解压 ZIP 后，运行 `ASMR-Cliper\\asmrcliper.exe`。'
                '合并需要额外预留一个完整 ZIP 的磁盘空间。\n\n'
                'NVIDIA 加速需要兼容的显卡驱动；构建机仅验证 CUDA 依赖，没有执行 GPU 推理。\n\n'
                f'- 完整 ZIP SHA-256：`{archive_hash}`\n'
                f'- 源码提交：`{commit}`\n'
                f'- [构建日志与原始完整 ZIP（Artifacts 保留 7 天）](https://github.com/{repository}/actions/runs/{run_id})\n',
                encoding='utf8')
        release = read_release(repository, tag)
        if release is None:
            gh('release', 'create', tag, '--repo', repository, '--target', commit, '--draft',
               '--title', f'ASMR-Cliper {version} · {target}', '--notes-file', notes)
            release = read_release(repository, tag)
        if release['target_commitish'] != commit:
            raise ValueError('Existing release belongs to a different source revision.')
        expected_assets = []
        if app_only:
            paths = [archive]
        else:
            parts = split_archive(archive, folder, part_size)
            try:
                for part, identity in parts:
                    upload(repository, tag, part, identity)
                    manifest['parts'].append(identity)
                    expected_assets.append(identity)
            finally:
                parts.close()
            manifest_path = folder / (archive.name + '.parts.json')
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
            merge = folder / 'merge-win64-nv.ps1'
            shutil.copy2(Path(__file__).with_name(merge.name), merge)
            paths = [manifest_path, merge]
        checksum_copy = folder / (archive.name + '.sha256')
        shutil.copy2(checksum, checksum_copy)
        for path in (*paths, checksum_copy):
            identity = asset_info(path)
            upload(repository, tag, path, identity)
            expected_assets.append(identity)
        release = read_release(repository, tag)
        remote_assets = {asset['name']: asset for asset in release['assets']}
        if (set(remote_assets) != {asset['name'] for asset in expected_assets} or
                any(not matches(remote_assets[asset['name']], asset) for asset in expected_assets)):
            raise RuntimeError('Release asset set is incomplete or failed verification; keeping the draft.')
        if release['draft']:
            gh('release', 'edit', tag, '--repo', repository, '--draft=false', '--notes-file', notes)
        release = read_release(repository, tag)
        if release['draft']:
            raise RuntimeError('GitHub did not publish the draft release.')
        return release['html_url']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--checksum', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--target', choices=('win64-nv', 'win64-app'), default='win64-nv')
    args = parser.parse_args()
    url = publish(args.archive, args.checksum, args.repository, args.commit, args.run_id, target=args.target)
    print('Published: ' + url, flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as summary:
            summary.write(f'### Release published\n\n[Download the verified portable package]({url})\n')


if __name__ == '__main__':
    main()
