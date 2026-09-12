"""Extract and validate an audio-only analysis source without decoding pictures."""
import hashlib
from itertools import zip_longest
import os
from pathlib import Path
import subprocess
import tempfile

import av

from .common import event, fingerprint, read_json, save_json
from .media import stream_signature


def run_ffmpeg(command, message, duration=0, progress_range=(1, 2)):
    # stderr goes to a file so a damaged input cannot fill a pipe and deadlock.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            last = -1
            for raw in process.stdout:
                key, _, value = raw.decode('utf8', errors='replace').strip().partition('=')
                if key != 'out_time_us' or not duration:
                    continue
                try: fraction = max(0., min(1., int(value) / 1e6 / duration))
                except ValueError: continue
                percent = int(fraction * 100)
                if percent != last:
                    event('progress', message, progress_range[0] + fraction * (progress_range[1] - progress_range[0]))
                    last = percent
            code = process.wait()
            errors.seek(0, os.SEEK_END)
            size = errors.tell()
            errors.seek(max(0, size - 3000))
            detail = errors.read().decode('utf8', errors='replace').strip()
            if code or detail:
                raise RuntimeError(message + '失败：' + detail)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(4 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_audio_copy(source, track):
    digest = hashlib.sha256()
    count = 0
    clock_error = 0.
    with av.open(str(source)) as original, av.open(str(track)) as extracted:
        if len(extracted.streams) != 1 or len(extracted.streams.audio) != 1:
            raise ValueError('分析缓存必须仅包含一个音轨。')
        a, b = original.streams.audio[0], extracted.streams.audio[0]
        if stream_signature(a) != stream_signature(b):
            raise ValueError('提取音轨改变了原音频编码参数。')
        left = (p for p in original.demux(a) if p.size)
        right = (p for p in extracted.demux(b) if p.size)
        for p, q in zip_longest(left, right):
            if p is None or q is None or p.size != q.size or bytes(p) != bytes(q):
                raise ValueError('提取音轨与源文件的音频包不一致，无法安全映射剪辑时间轴。')
            if p.pts is None or q.pts is None:
                raise ValueError('音轨缺少时间戳，无法保证音画同步。')
            error = abs(float(p.pts * p.time_base - q.pts * q.time_base))
            clock_error = max(clock_error, error)
            if error > 1 / a.codec_context.sample_rate + 1e-9:
                raise ValueError('提取音轨改变了源时间戳，已停止分析以防音画错位。')
            digest.update(bytes(p))
            count += 1
    if not count:
        raise ValueError('提取的音轨为空。')
    return {'packets': count, 'payload_sha256': digest.hexdigest(), 'payload_unchanged': True,
            'max_timestamp_error': clock_error, 'video_decoded': False}


def extract_audio(source, cache, ffmpeg):
    source, cache = Path(source), Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    identity = fingerprint(source)
    # Keeping the container family preserves codec delay, skip samples and clocks.
    suffix = source.suffix.lower()
    suffix = suffix if suffix in ('.mp4', '.mov', '.mkv', '.webm') else '.mka'
    track = cache / ('source-audio' + suffix)
    receipt = cache / 'source-audio.json'
    try: saved = read_json(receipt)
    except (OSError, ValueError): saved = {}
    key = {'source': identity, 'version': 1, 'file': track.name}
    if (saved.get('key') == key and track.is_file() and saved.get('bytes') == track.stat().st_size
            and saved.get('file_sha256') == file_hash(track)):
        event('log', '复用已校验的独立音轨缓存。')
        return track, saved['verification']
    with av.open(str(source)) as container:
        stream = container.streams.audio[0]
        duration = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / 1e6
    temp = track.with_name(track.stem + '.part' + suffix)
    command = [str(ffmpeg), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-nostats', '-y',
               '-progress', 'pipe:1', '-copyts', '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
               '-c:a', 'copy', '-map_metadata', '-1', '-map_chapters', '-1', '-avoid_negative_ts', 'disabled']
    if suffix in ('.mp4', '.mov'):
        command += ['-movie_timescale', '1000000']
    command.append(str(temp))
    event('progress', '提取独立音轨（原编码复制，不解码视频）', 1)
    try:
        run_ffmpeg(command, '提取独立音轨', duration)
        proof = verify_audio_copy(source, temp)
        if fingerprint(source) != identity:
            raise RuntimeError('提取期间源文件发生变化，请重新开始。')
        os.replace(temp, track)
        save_json(receipt, {'key': key, 'bytes': track.stat().st_size, 'file_sha256': file_hash(track), 'verification': proof})
        return track, proof
    finally:
        temp.unlink(missing_ok=True)


def analyze_input_audio(source, cache, cfg, media):
    from .analysis import analyze
    # Old completed caches already contain only PCM audio and the original packet clock.
    if all((cache / name).is_file() for name in ('analysis.json', 'analysis.wav', 'frames.npz')):
        return analyze(source, cache)
    if not media.get('input_has_video'):
        return analyze(source, cache)
    track, proof = extract_audio(source, cache, cfg['ffmpeg'])
    meta, frames = analyze(track, cache)
    meta['audio_preparation'] = {'method': 'ffmpeg_stream_copy', **proof}
    save_json(cache / 'analysis.json', meta)
    return meta, frames
