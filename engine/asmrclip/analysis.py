import os
import wave
from pathlib import Path

import av
import numpy as np

from .common import event, read_json, save_json


def load_frames(path):
    with np.load(path) as data:
        return {key:data[key] for key in data.files}


def inspect_audio(source):
    with av.open(str(source)) as c:
        if not c.streams.audio:
            raise ValueError('文件中没有音频轨道。')
        s = c.streams.audio[0]
        codec = s.codec_context
        packet_copy=codec.name=='aac' and codec.profile=='LC' and codec.frame_size==1024
        if not codec.sample_rate or not 1<=len(codec.layout.channels)<=8:
            raise ValueError('音轨缺少有效采样率或声道配置。')
        if packet_copy and not codec.extradata:
            raise ValueError('缺少 AAC 配置信息，请使用 M4A / MP4 容器。')
        return {'sample_rate': codec.sample_rate, 'channels': len(codec.layout.channels),
                'codec': 'AAC LC' if packet_copy else codec.name, 'frame_samples': 1024,
                'aac_packet_grid':packet_copy,
                'duration': float(s.duration * s.time_base) if s.duration else float(c.duration or 0) / 1e6}


def analyze(source, cache):
    meta_path = cache / 'analysis.json'
    if meta_path.exists() and (cache / 'analysis.wav').exists() and (cache / 'frames.npz').exists():
        event('progress', '复用已完成的音频分析', 18)
        return read_json(meta_path), load_frames(cache / 'frames.npz')
    meta = inspect_audio(source)
    if not meta['aac_packet_grid']:
        return analyze_decoded(source,cache,meta)
    event('progress', '读取音频帧并生成本地分析副本', 2, audio=meta)
    cache.mkdir(parents=True, exist_ok=True)
    wav_temp = cache / 'analysis.part.wav'
    levels, packet_indices, source_times = [], [], []
    trailing_partial = False
    discarded_edge_samples = 0
    resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
    expected_duration = max(meta['duration'], 1)
    with av.open(str(source)) as container, wave.open(str(wav_temp), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        ordinal = -1
        stream = container.streams.audio[0]
        for packet in container.demux(stream):
            if not packet.size:
                continue
            ordinal += 1
            frames = packet.decode()
            if len(frames) > 1:
                raise ValueError('该 AAC 文件每包包含多个解码帧，当前无法保证逐帧剪辑。')
            for frame in frames:
                if 0 < frame.samples < 1024:
                    # MP4 edit lists may expose only part of the first/last AAC
                    # frame. A packet-copy editor drops that partial edge rather
                    # than reencoding it or shifting the analysis clock.
                    discarded_edge_samples += frame.samples
                    if levels:
                        trailing_partial = True
                    continue
                if frame.samples != 1024 or trailing_partial:
                    raise ValueError('该文件在音频内部包含非标准 AAC 帧长度，无法保证逐帧剪辑。')
                x = frame.to_ndarray().astype(np.float32)
                if frame.format.name not in ('fltp', 'flt'):
                    raise ValueError('不支持的 AAC 解码采样格式。')
                x = x.reshape(meta['channels'], -1) if frame.format.is_planar else x.reshape(-1, meta['channels']).T
                levels.append([float(np.sqrt(np.mean(x*x, axis=1)).max()), float(np.abs(x).max())])
                packet_indices.append(ordinal)
                source_times.append(float(packet.pts * packet.time_base) if packet.pts is not None else len(levels)*1024/meta['sample_rate'])
                if len(source_times)>1 and abs(source_times[-1]-source_times[-2]-1024/meta['sample_rate'])>.1:
                    raise ValueError('音轨内部存在时间戳跳变，无法保证剪辑时间轴。')
                # Container rounding drift must not change the analysis sample clock.
                frame.pts = None
                for mono in resampler.resample(frame):
                    wav.writeframesraw(mono.to_ndarray().tobytes())
                if len(levels) % 8192 == 0:
                    t = len(levels)*1024/meta['sample_rate']
                    event('progress', f'已分析 {int(t//60)} 分钟音频', min(18, 2+16*t/expected_duration))
        for mono in resampler.resample(None):
            wav.writeframesraw(mono.to_ndarray().tobytes())
    if not levels:
        raise ValueError('没有可解码的 AAC 音频帧。')
    meta['frames'] = len(levels)
    meta['discarded_partial_edge_samples'] = discarded_edge_samples
    meta['analysis_duration'] = len(levels)*1024/meta['sample_rate']
    with (cache / 'frames.part.npz').open('wb') as f:
        np.savez(f, levels=np.asarray(levels, np.float32), packets=np.asarray(packet_indices, np.int64),
                 source_times=np.asarray(source_times, np.float64))
    os.replace(cache / 'frames.part.npz', cache / 'frames.npz')
    os.replace(wav_temp, cache / 'analysis.wav')
    save_json(meta_path, meta)
    return meta, load_frames(cache / 'frames.npz')


def analyze_decoded(source,cache,meta):
    """Analyze other codecs on a PCM grid; exports still use original packets."""
    event('progress','读取音轨并生成本地分析副本（输出仍复制源编码）',2,audio=meta)
    cache.mkdir(parents=True,exist_ok=True)
    levels=[];source_times=[];origin=None;decoded=0;pending=np.empty((meta['channels'],0),np.float32)
    normalize=av.AudioResampler(format='fltp',layout=None,rate=meta['sample_rate'])
    mono=av.AudioResampler(format='s16',layout='mono',rate=16000)
    with av.open(str(source)) as container,wave.open(str(cache/'analysis.part.wav'),'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
        for frame in container.decode(audio=0):
            t=float(frame.pts*frame.time_base) if frame.pts is not None else None
            if origin is None:origin=t or 0.
            if t is not None and abs(t-(origin+decoded/meta['sample_rate']))>.1:
                raise ValueError('音轨内部存在时间戳跳变，无法保证音画同步。')
            decoded+=frame.samples
            frame.pts=None
            for normalized in normalize.resample(frame):
                pending=np.concatenate((pending,normalized.to_ndarray()),axis=1)
                while pending.shape[1]>=1024:
                    x=pending[:,:1024];pending=pending[:,1024:]
                    levels.append([float(np.sqrt(np.mean(x*x,axis=1)).max()),float(np.abs(x).max())])
                    source_times.append(origin+(len(levels)-1)*1024/meta['sample_rate'])
                    block=av.AudioFrame.from_ndarray(np.ascontiguousarray(x),format='fltp',layout=normalized.layout.name)
                    block.sample_rate=meta['sample_rate']
                    for output in mono.resample(block):wav.writeframesraw(output.to_ndarray().tobytes())
            if len(levels) and len(levels)%8192==0:
                seconds=len(levels)*1024/meta['sample_rate']
                expected=meta.get('duration',0)
                event('progress',f'已分析 {seconds/60:.0f} 分钟音轨',min(18,2+16*seconds/expected) if expected else None)
        for output in mono.resample(None):wav.writeframesraw(output.to_ndarray().tobytes())
    if not levels:raise ValueError('没有足够的可解码音频。')
    meta.update(frames=len(levels),analysis_duration=len(levels)*1024/meta['sample_rate'],
                discarded_partial_edge_samples=int(pending.shape[1]))
    with (cache/'frames.part.npz').open('wb') as f:
        np.savez(f,levels=np.asarray(levels,np.float32),packets=np.full(len(levels),-1,np.int64),
                 source_times=np.asarray(source_times,np.float64))
    os.replace(cache/'frames.part.npz',cache/'frames.npz');os.replace(cache/'analysis.part.wav',cache/'analysis.wav')
    save_json(cache/'analysis.json',meta)
    return meta,load_frames(cache/'frames.npz')
