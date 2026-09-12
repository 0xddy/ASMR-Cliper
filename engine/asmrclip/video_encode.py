"""Frame-accurate video cuts; encode pictures only after audio review succeeds."""
from bisect import bisect_left
from fractions import Fraction
import hashlib
from pathlib import Path
import struct

import av

from .common import event
from .media import source_intervals, stream_signature
from .progress import advance

TIME_BASE = Fraction(1, 90000)


def frame_index(source, index):
    rows = []
    with av.open(str(source)) as container:
        stream = container.streams[index]
        fallback = 1 / float(stream.average_rate or 25)
        for packet in container.demux(stream):
            if not packet.size: continue
            if packet.pts is None: raise ValueError('视频缺少显示时间戳，无法精确同步剪辑。')
            start = float(packet.pts * packet.time_base)
            duration = float(packet.duration * packet.time_base) if packet.duration else fallback
            rows.append((start, start + duration))
    rows.sort()
    if not rows or any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError('视频帧时间戳不完整或重复，无法精确切割。')
    return rows


def align_frames(source, meta, frames, plan, media):
    if '_video_frames' not in media:
        media['_video_frames'] = frame_index(source, media['video_index'])
    video = media['_video_frames']; starts = [r[0] for r in video]
    result = []
    for row in source_intervals(meta, frames, plan):
        first = bisect_left(starts, row['source_start'] - 1e-7)
        stop = bisect_left(starts, row['source_end'] - 1e-7)
        while stop > first and video[stop - 1][1] > row['source_end'] + 1e-7: stop -= 1
        if first == stop: continue
        start, end = video[first][0], video[stop - 1][1]
        result.append({'source_start': start, 'source_end': end,
            'analysis_start': row['analysis_start'] + start - row['source_start'],
            'analysis_end': row['analysis_start'] + end - row['source_start'],
            'video_frame_first': first, 'video_frame_stop': stop})
    if not result: raise ValueError('保留区间短于一个完整视频帧，无法生成视频；可选择仅音频。')
    return result


def video_spec(source, index):
    with av.open(str(source)) as container:
        stream = container.streams[index]; codec = stream.codec_context
        fmt = codec.format.name
        codec.thread_count = 2
        first = next(container.decode(stream), None)
        display = first.side_data.get('DISPLAYMATRIX') if first is not None else None
        return {'width': codec.width, 'height': codec.height, 'format': fmt,
            'rate': stream.average_rate or Fraction(25), 'codec': codec.name,
            'aspect': codec.sample_aspect_ratio,
            'bits': max(c.bits for c in codec.format.components),
            'display_matrix': struct.unpack('=9i', bytes(display)) if display is not None else None,
            'colors': {key: getattr(codec, key) for key in ('color_range', 'color_primaries', 'color_trc', 'colorspace')}}


def encode_attempt(source, target, media, mapping, spec, encoder):
    options = {'movflags': '+faststart', 'movie_timescale': '1000000'} if target.suffix == '.mp4' else {}
    durations = {}; count = 0; expected_count = sum(r['video_frame_stop'] - r['video_frame_first'] for r in mapping)
    gpu = encoder.endswith('_nvenc')
    pixel_format = 'p010le' if gpu and spec['format'] == 'yuv420p10le' else spec['format']
    with av.open(str(source)) as src, av.open(str(target), 'w', options=options) as dst:
        video = src.streams[media['video_index']]; video.codec_context.thread_count = 2
        output = dst.add_stream(encoder, rate=spec['rate'])
        output.width = spec['width']; output.height = spec['height']; output.pix_fmt = pixel_format
        output.time_base = TIME_BASE; output.codec_context.time_base = TIME_BASE
        output.codec_context.thread_count = 2; output.codec_context.max_b_frames = 0
        output.codec_context.gop_size = max(1, round(float(spec['rate']) * 2))
        if spec['aspect']: output.codec_context.sample_aspect_ratio = spec['aspect']
        if spec['display_matrix']: output.set_display_matrix(spec['display_matrix'])
        for key, value in spec['colors'].items(): setattr(output.codec_context, key, value)
        output.options = ({'preset': 'p5', 'rc': 'vbr', 'cq': '18', 'b': '0'} if gpu else
            {'preset': 'fast', 'crf': '18', **({'x265-params': 'pools=2:frame-threads=2:log-level=error:open-gop=0'} if encoder == 'libx265' else {})})
        output.codec_context.open()

        def mux(packets):
            for packet in packets:
                if packet.pts is None: raise RuntimeError('视频编码器未返回显示时间戳。')
                key = round(packet.pts * packet.time_base / TIME_BASE)
                if key not in durations: raise RuntimeError('视频编码器改变了显示时间轴。')
                packet.duration = max(1, round(durations.pop(key) / packet.time_base))
                dst.mux(packet)

        advance(0, expected_count, '视频帧')
        for row in mapping:
            # Decode preroll from the previous keyframe, but encode only the
            # approved frames. Seeking does not constrain the actual cut point.
            src.seek(int(row['source_start'] / video.time_base), stream=video, backward=True, any_frame=False)
            index = row['video_frame_first']
            for frame in src.decode(video):
                if frame.pts is None: raise ValueError('解码视频帧缺少显示时间戳。')
                time = float(frame.pts * frame.time_base)
                if time < row['source_start'] - 1e-7: continue
                if time >= row['source_end'] - 1e-7: break
                if index >= row['video_frame_stop']: raise ValueError('视频解码帧数与切点索引不一致。')
                expected, end = media['_video_frames'][index]
                if abs(time - expected) > max(float(frame.time_base), 1e-6):
                    raise ValueError('视频解码时间戳与切点索引不一致，已停止以防音画错位。')
                frame = frame.reformat(width=spec['width'], height=spec['height'], format=pixel_format)
                frame.pts = round((time - row['source_start'] + row['output_start']) / TIME_BASE)
                frame.time_base = TIME_BASE; frame.pict_type = 0
                durations[frame.pts] = Fraction(str(end - expected))
                mux(output.encode(frame)); index += 1; count += 1
                if count % 60 == 0: advance(count, expected_count, '视频帧')
            if index != row['video_frame_stop']: raise ValueError('未能解码所有保留视频帧，已停止发布。')
        mux(output.encode(None))
        if durations: raise RuntimeError('视频编码器未输出所有保留帧。')
        advance(count, expected_count, '视频帧')
    return count


def encode_video(source, target, media, report, cfg):
    target = Path(target); spec = video_spec(source, media['video_index'])
    hevc = spec['codec'] == 'hevc' or spec['bits'] > 8
    software = 'libx265' if hevc else 'libx264'
    encoders = [software]
    if cfg.get('device') != 'cpu' and spec['format'] in ('yuv420p', 'nv12', 'yuv420p10le'):
        encoders.insert(0, 'hevc_nvenc' if hevc else 'h264_nvenc')
    for encoder in encoders:
        try:
            event('log', '精确切割视频：' + ('NVIDIA 硬件编码' if encoder.endswith('_nvenc') else 'CPU 编码（限制线程）'))
            count = encode_attempt(source, target, media, report['mapping'], spec, encoder)
            break
        except av.error.FFmpegError as exc:
            target.unlink(missing_ok=True)
            if encoder == software: raise
            event('log', 'NVIDIA 视频编码不可用，改用 CPU：' + str(exc))
    digest = hashlib.sha256(); packets = 0
    with av.open(str(target)) as container:
        stream = container.streams.video[0]; signature = stream_signature(stream)
        last = None
        for packet in container.demux(stream):
            if not packet.size: continue
            if packet.pts is None or (last is not None and packet.pts <= last):
                raise RuntimeError('精确切割后视频时间轴不连续。')
            last = packet.pts; digest.update(bytes(packet)); packets += 1
    if packets != count: raise RuntimeError('精确切割后视频帧数不一致。')
    return {'video_payload_sha256': digest.hexdigest(), 'video_payload_unchanged': False,
        'video_packets': packets, 'video_codec': signature['codec'], 'video_signature': signature,
        'video_encoding': {'encoder': encoder, 'reencoded': True, 'quality': 18,
            'width': spec['width'], 'height': spec['height'], 'source_pixel_format': spec['format'],
            'output_pixel_format': signature['pixel_format'], 'frame_selection_verified': True}}


def mux_video(video, audio, target, report, encoded, cfg):
    from .audio_source import run_ffmpeg
    from .audio_fades import verify_stream_copy
    command = [str(cfg['ffmpeg']), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-y', '-copyts',
        '-i', str(video), '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy',
        '-avoid_negative_ts', 'disabled', '-map_metadata', '-1', '-map_chapters', '-1']
    if target.suffix in ('.mp4', '.mov'):
        command += ['-movflags', '+faststart', '-movie_timescale', '1000000', '-write_tmcd', '0']
    run_ffmpeg(command + [str(target)], '合并精确剪辑画面与已确认音轨')
    verify_stream_copy(video, target, 'video'); verify_stream_copy(audio, target, 'audio')
    result = {**report, **encoded, 'media_kind': 'video', 'keyframe_trim_seconds': 0.,
        'reviewed_audio_payload_verified': True, 'bytes': target.stat().st_size}
    result['streams'] = {**report['streams'], 'video': result.pop('video_signature')}
    return result
