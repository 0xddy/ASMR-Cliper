"""Bound task scratch storage without touching models, results or active jobs."""
import contextlib
import os
from pathlib import Path
import re
import shutil
import stat
import time

from .common import ROOT, event, fingerprint, read_json, save_json

RETENTION_SECONDS = 7 * 24 * 3600
MAX_INACTIVE_BYTES = 8 * 1024**3
MIN_FREE_BYTES = 2 * 1024**3
IDENTITY = re.compile(r'[0-9a-f]{24}')
FILES = frozenset(('analysis.json', 'analysis.wav', 'analysis.part.wav', 'frames.npz',
    'frames.part.npz', 'speech.json', 'acoustic-cache.json', 'semantic-cache.json',
    'extraction-evidence.json', 'exclusions-latest.json', 'audit-latest.json',
    'post-review-latest.json', 'plan-strict.json', 'plan-relaxed.json', 'plan-extract.json',
    'spectral-transitions.npz', 'spectral-transitions.part.npz', 'transition-review.json', 'source-audio.json',
    'menu-windows.json', 'evidence.json', 'drinking-review.json', 'whisper-review.json'))


class CacheBusy(RuntimeError):
    pass


def plain(path):
    """Do not follow symlinks or Windows directory junctions during cleanup."""
    info = path.lstat()
    return not (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


@contextlib.contextmanager
def file_lock(path):
    if path.exists() and not plain(path):
        raise ValueError('缓存锁不能为链接。')
    with path.open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0'); file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CacheBusy('同一缓存已有任务正在使用。') from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def cache_lock(cache):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    if not plain(cache):
        raise ValueError('任务缓存目录不能为链接。')
    with file_lock(cache / '.lock'):
        yield


def safe_task(root, cache, protected=()):
    root, cache = Path(root).absolute(), Path(cache).absolute()
    if not IDENTITY.fullmatch(cache.name):
        return False
    if cache.parent != root and cache.parent != root / 'program-menus':
        return False
    try:
        # Check the lexical path as well as its final resolved location.
        if not plain(root) or not plain(cache) or not plain(cache.parent):
            return False
        if not cache.resolve().is_relative_to(root.resolve()):
            return False
        return not any(Path(p).resolve().is_relative_to(cache.resolve()) for p in protected if p)
    except OSError:
        return False


def removable(name):
    base = name[:-4] if name.endswith('.tmp') else name
    return (base in FILES or re.fullmatch(r'speech-[0-9a-f]{12}\.jsonl', base) or
            re.fullmatch(r'source-audio(?:\.part)?\.(mp4|mov|mkv|webm|mka)', base))


def payload_files(cache):
    """Allowlist only files written by the engine; ignore arbitrary user files."""
    for path in cache.iterdir():
        if not plain(path):
            continue
        if path.is_file() and removable(path.name):
            yield path
        elif path.is_dir() and path.name == 'review-results':
            for folder, dirs, files in os.walk(path, followlinks=False):
                folder = Path(folder)
                dirs[:] = [d for d in dirs if plain(folder / d)]
                for name in files:
                    item = folder / name
                    if re.fullmatch(r'[0-9a-f]{64}\.json(?:\.tmp)?', name) and plain(item):
                        yield item
        elif path.is_dir() and path.name.startswith('inference-'):
            audio = path / 'audio.npy'
            if audio.is_file() and plain(audio):
                yield audio


def purge(root, cache, protected=()):
    """Caller holds the task lock. Keep the lock inode to avoid lock races."""
    result = {'freed_bytes': 0, 'removed_files': 0, 'remaining_bytes': 0, 'errors': []}
    if not safe_task(root, cache, protected):
        result['errors'].append('缓存路径不满足清理范围，已跳过。')
        return result
    root, cache = Path(root).resolve(), Path(cache).resolve()
    for path in list(payload_files(cache)):
        try:
            if not plain(path) or not path.resolve().is_relative_to(cache):
                continue
            size = path.stat().st_size
            path.unlink()
            result['freed_bytes'] += size; result['removed_files'] += 1
        except OSError as exc:
            result['errors'].append(f'{path.name}: {exc}')
    result['remaining_bytes'] = sum(p.stat().st_size for p in payload_files(cache))
    # Empty scratch/review directories are harmless, but keep them tidy as well.
    for child in cache.iterdir():
        if not child.is_dir() or not plain(child) or not (child.name == 'review-results' or child.name.startswith('inference-')):
            continue
        for folder, _, _ in os.walk(child, topdown=False, followlinks=False):
            path = Path(folder)
            if plain(path) and path.resolve().is_relative_to(cache):
                try: path.rmdir()
                except OSError: pass
    return result


def state(cache):
    try:
        value = read_json(cache / 'task-state.json')
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError): return {}


def begin(cache, source):
    save_json(Path(cache) / 'task-state.json',
              {'version': 1, 'status': 'active', 'last_used': time.time(), 'source': str(source)})


def finish(cfg, cache, output):
    """Only called after final media/sidecars have been published, under lock."""
    result = {'freed_bytes': 0, 'removed_files': 0, 'remaining_bytes': 0, 'errors': []}
    try:
        output = Path(output).resolve()
        if not output.is_file():
            raise ValueError('成片尚未保存，保留重试缓存。')
        result = purge(cfg['cache_dir'], cache, (cfg.get('input'), output))
        save_json(Path(cache) / 'task-state.json', {'version': 1, 'status': 'completed',
            'last_used': time.time(), 'output': str(output), 'cleanup': result})
    except (OSError, ValueError, TypeError) as exc:
        result['errors'].append(str(exc))
    result['status'] = 'partial' if result['errors'] else 'cleaned'
    event('log', f'任务分析缓存已清理，释放 {result["freed_bytes"] / 1024**2:.1f} MB。' +
          (' 部分文件暂时无法清理，下次任务会重试。' if result['errors'] else ''))
    return result


def completed_sources():
    """Migrate old caches only when history proves the final media was saved."""
    result = set()
    try:
        for row in read_json(ROOT / 'config/history.json'):
            # Users may move/delete a finished export later; its successful
            # history record still proves these old analysis files are disposable.
            if row.get('decode_verified') is True and row.get('output'):
                try: result.add(fingerprint(Path(row['source'])))
                except (KeyError, OSError): pass
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return result


def maintain(cfg, exclude=(), *, now=None, budget=MAX_INACTIVE_BYTES,
             retention=RETENTION_SECONDS, min_free=MIN_FREE_BYTES, completed=None):
    """Prune inactive task caches at task boundaries, oldest first, best effort."""
    root = Path(cfg['cache_dir']).absolute()
    total = {'freed_bytes': 0, 'removed_files': 0, 'active_skipped': 0, 'errors': []}
    if not root.is_dir() or not plain(root):
        return total
    completed = completed_sources() if completed is None else completed
    now = time.time() if now is None else now
    excluded = {Path(p).resolve() for p in exclude}
    protected = (cfg.get('input'), cfg.get('output_dir'))
    try:
        with file_lock(root / '.maintenance.lock'), contextlib.ExitStack() as locks:
            candidates = list(root.iterdir())
            menus = root / 'program-menus'
            if menus.is_dir() and plain(menus): candidates += list(menus.iterdir())
            entries = []
            for cache in candidates:
                if not cache.is_dir() or cache.resolve() in excluded or not safe_task(root, cache, protected):
                    continue
                # Legacy task roots always have source.json; menu roots have a known evidence file.
                if not (cache / 'source.json').is_file() and not (cache / 'task-state.json').is_file() and not (cache / 'menu-windows.json').is_file():
                    continue
                try: locks.enter_context(cache_lock(cache))
                except CacheBusy: total['active_skipped'] += 1; continue
                files = list(payload_files(cache)); size = sum(p.stat().st_size for p in files)
                if not files: continue
                record = state(cache)
                last_used = max([float(record.get('last_used', 0))] + [p.stat().st_mtime for p in files])
                ready = record.get('status') == 'completed' or (not record and cache.name in completed)
                entries.append((last_used, cache, size, ready))
            remaining = sum(row[2] for row in entries)
            budget = min(budget, max(0, remaining - max(0, min_free - shutil.disk_usage(root).free)))
            # Finished work is disposable even when newer than useful retry data.
            for last_used, cache, size, ready in sorted(entries, key=lambda row: (not row[3], row[0])):
                if ready or now - last_used > retention or remaining > budget:
                    result = purge(root, cache, protected)
                    remaining -= result['freed_bytes']
                    total['freed_bytes'] += result['freed_bytes']; total['removed_files'] += result['removed_files']
                    total['errors'] += result['errors']
    except CacheBusy:
        return total
    except (OSError, ValueError, TypeError) as exc:
        total['errors'].append(str(exc))
    if total['freed_bytes'] or total['errors']:
        event('log', f'整理过期任务缓存，释放 {total["freed_bytes"] / 1024**2:.1f} MB。' +
              (' 部分缓存未能清理，已保留。' if total['errors'] else ''))
    return total
