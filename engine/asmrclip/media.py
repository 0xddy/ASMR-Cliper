"""Original-packet audio/video exports with inward-only video boundaries."""
import hashlib
from array import array
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from .common import event


def video_stream(container):
    return next((s for s in container.streams.video if not int(s.disposition) & 0x400), None)


def inspect_media(source, output_kind='auto'):
    if output_kind not in ('auto', 'audio', 'video'):
        raise ValueError('输出类型必须为跟随输入、仅音频或视频。')
    with av.open(str(source)) as container:
        if not container.streams.audio:
            raise ValueError('文件没有音轨，无法根据声音进行 ASMR 剪辑。')
        audio=container.streams.audio[0]
        video=video_stream(container)
        use_video=video is not None and output_kind!='audio'
        if output_kind=='video' and video is None:
            raise ValueError('当前文件没有视频画面，请选择“跟随输入”或“仅音频”。')
        suffix=Path(source).suffix.lower()
        # Keep compatible source containers; Matroska handles other source codecs.
        extension=(suffix if suffix in ('.mp4','.mov','.mkv','.webm') else '.mkv') if use_video else (
            '.m4a' if audio.codec_context.name=='aac' and audio.codec_context.profile=='LC' else '.mka')
        return {'kind':'video' if use_video else 'audio','extension':extension,
                'audio_index':audio.index,'video_index':video.index if use_video else None,
                'audio_codec':audio.codec_context.name,
                'audio_tracks':len(container.streams.audio),
                'video_codec':video.codec_context.name if use_video else None}


def source_intervals(meta,frames,plan):
    dt=meta['frame_samples']/meta['sample_rate']
    # Also validate older analysis caches before using their source clock.
    if np.any(np.abs(np.diff(frames['source_times'])-dt)>.1):
        raise ValueError('音轨时间轴有跳变，无法安全同步剪辑。')
    return [{'source_start':float(frames['source_times'][a]),
             'source_end':float(frames['source_times'][b-1])+dt,
             'analysis_start':a*dt,'analysis_end':b*dt}
            for a,b in plan['keep_frames']]


def video_groups(source, index):
    """Index complete GOPs in decode order, retaining their presentation bounds."""
    groups=[];current=None;ordinal=-1
    with av.open(str(source)) as container:
        stream=container.streams[index]
        fallback=1/float(stream.average_rate or 25)
        for packet in container.demux(stream):
            if not packet.size:continue
            ordinal+=1
            if packet.pts is None:
                raise ValueError('视频缺少时间戳，无法保证原编码剪辑与音画同步。')
            pts=float(packet.pts*packet.time_base)
            end=pts+(float(packet.duration*packet.time_base) if packet.duration else fallback)
            if packet.is_keyframe:
                if current:
                    current.update(end=pts,stop=ordinal)
                    groups.append(current)
                current={'start':pts,'first':ordinal,'min_pts':pts,'max_end':end}
            if current:
                current['min_pts']=min(current['min_pts'],pts)
                current['max_end']=max(current['max_end'],end)
        if current:
            current.update(end=current['max_end'],stop=ordinal+1)
            groups.append(current)
    return groups


def align_video(source,index,intervals):
    event('progress','查找视频关键帧并向保留片段内部对齐',89)
    groups=video_groups(source,index)
    aligned=[];cursor=0
    for group in groups:
        while cursor<len(intervals) and intervals[cursor]['source_end']<group['end']-1e-7:
            cursor+=1
        if cursor==len(intervals):break
        interval=intervals[cursor]
        # Open GOP leading/trailing pictures may refer outside this group. Do
        # not copy them across a splice or expose pictures from a deleted area.
        if (group['start']<interval['source_start']-1e-7 or group['end']>interval['source_end']+1e-7
                or group['min_pts']<group['start']-1e-7 or group['max_end']>group['end']+1e-5):continue
        if aligned and aligned[-1]['stop']==group['first'] and aligned[-1]['interval']==cursor:
            aligned[-1].update(source_end=group['end'],stop=group['stop'])
        else:
            aligned.append({'source_start':group['start'],'source_end':group['end'],
                            'first':group['first'],'stop':group['stop'],'interval':cursor})
    if not aligned:
        raise ValueError('保留区间内没有可完整复制的视频关键帧片段。可选择“仅音频”，或保留更长片段。')
    for row in aligned:
        original=intervals[row.pop('interval')]
        row['analysis_start']=original['analysis_start']+row['source_start']-original['source_start']
        row['analysis_end']=row['analysis_start']+row['source_end']-row['source_start']
    return aligned


def stream_signature(stream):
    codec=stream.codec_context
    result={'codec':codec.name,'extradata':hashlib.sha256(codec.extradata or b'').hexdigest()}
    if stream.type=='video':
        result.update(width=codec.width,height=codec.height,pixel_format=codec.format.name if codec.format else None)
    else:result.update(sample_rate=codec.sample_rate,channels=len(codec.layout.channels))
    return result


def copy_media_packets(source,destination,meta,frames,plan,media):
    intervals=source_intervals(meta,frames,plan)
    if media['kind']=='video':intervals=align_video(source,media['video_index'],intervals)
    if not intervals:raise ValueError('没有可导出的片段。')
    # Map audio packets on the same source clock as the video, retaining only
    # complete packets inside accepted intervals. Gaps stay local to each join;
    # they must never accumulate as audio/video drift.
    cursor=0.;mapping=[]
    for row in intervals:
        row={**row,'output_start':cursor,'audio_packets':0,'video_packets':0}
        cursor+=row['source_end']-row['source_start'];row['output_end']=cursor;mapping.append(row)
    starts=[r['source_start'] for r in mapping]
    firsts=[r['first'] for r in mapping] if media['kind']=='video' else []
    indices=[media['audio_index']]+([media['video_index']] if media['kind']=='video' else [])
    hashes={i:hashlib.sha256() for i in indices};sizes={i:[] for i in indices}
    timestamps={i:array('d') for i in indices}
    signatures={};last_dts={};ordinal=-1
    fmt={'.mp4':'mp4','.mov':'mov','.mkv':'matroska','.webm':'webm','.mka':'matroska','.m4a':'ipod'}[destination.suffix.lower()]
    options={'movflags':'+faststart','movie_timescale':'1000000','write_tmcd':'0'} if fmt in ('mp4','mov','ipod') else {}
    with av.open(str(source)) as src, av.open(str(destination),'w',format=fmt,options=options) as dst:
        streams={i:src.streams[i] for i in indices}
        targets={i:dst.add_stream_from_template(streams[i]) for i in indices}
        for i,st in streams.items():
            signatures[st.type]=stream_signature(st)
            targets[i].time_base=st.time_base
            targets[i].metadata.update(st.metadata)
        dst.metadata.update(src.metadata)
        for packet in src.demux(list(streams.values())):
            if not packet.size:continue
            i=packet.stream.index;kind=packet.stream.type
            if packet.pts is None:
                raise ValueError('媒体包缺少时间戳，无法无损对齐。')
            pts=float(packet.pts*packet.time_base)
            duration=float(packet.duration*packet.time_base)
            if kind=='video':
                ordinal+=1;j=bisect_right(firsts,ordinal)-1
                if j<0 or ordinal>=mapping[j]['stop']:continue
            else:
                if duration<=0:raise ValueError('音频包缺少时长，无法保证无损同步。')
                j=bisect_right(starts,pts+1e-8)-1
                if j<0 or pts+duration>mapping[j]['source_end']+1e-7:continue
            if packet.dts is None:raise ValueError('保留区间的视频解码时间戳缺失，无法保证同步。')
            row=mapping[j];row[kind+'_packets']+=1
            if kind=='audio':
                row.setdefault('audio_source_start',pts)
                row['audio_source_end']=pts+duration
            shift=round(Fraction(str(row['output_start']-row['source_start']))/packet.time_base)
            packet.pts+=shift;packet.dts+=shift
            if i in last_dts and packet.dts<=last_dts[i]:
                raise ValueError('视频帧重排使拼接时间戳冲突，无法安全复制原编码；请选择仅音频输出。')
            last_dts[i]=packet.dts
            timestamps[i].extend((float(packet.pts*packet.time_base),float(packet.dts*packet.time_base)))
            hashes[i].update(bytes(packet));sizes[i].append(packet.size)
            packet.stream=targets[i];dst.mux(packet)
    if any(not row['audio_packets'] or (media['kind']=='video' and not row['video_packets']) for row in mapping):
        raise ValueError('部分保留区间缺少完整的音频或视频包，已停止发布。')
    actual={i:hashlib.sha256() for i in indices};actual_sizes={i:[] for i in indices};output_stats={};max_timestamp_error=0.
    with av.open(str(destination)) as check:
        output_streams={s.index:indices[n] for n,s in enumerate(check.streams)}
        for stream in check.streams:
            signature=stream_signature(stream)
            if signature!=signatures[stream.type]:
                raise AssertionError('导出的音视频编码参数与源文件不一致。')
            output_stats[stream.type]=signature
        decoded_dts={}
        for packet in check.demux():
            if not packet.size:continue
            i=output_streams[packet.stream.index]
            if (packet.dts is None and (packet.stream.type!='video' or len(actual_sizes[i])>=16)) or (packet.dts is not None and i in decoded_dts and packet.dts<=decoded_dts[i]):
                raise AssertionError('成片时间戳不连续。')
            n=len(actual_sizes[i])*2
            if packet.pts is None or n+1>=len(timestamps[i]):raise AssertionError('成片包时间戳数量异常。')
            # Matroska stores presentation times; the demuxer reconstructs DTS,
            # which can differ for VFR B-frames without changing displayed time.
            stored_dts=packet.dts is not None and not (packet.stream.type=='video' and fmt in ('matroska','webm'))
            error=max(abs(float(packet.pts*packet.time_base)-timestamps[i][n]),
                      abs(float(packet.dts*packet.time_base)-timestamps[i][n+1]) if stored_dts else 0.)
            max_timestamp_error=max(max_timestamp_error,error)
            if error>float(packet.time_base)+1e-6:
                raise AssertionError(f'容器改变了音画时间轴，无法保证同步：{packet.stream.type}，偏差 {error:.6f} 秒。')
            if packet.dts is not None:decoded_dts[i]=packet.dts
            actual[i].update(bytes(packet));actual_sizes[i].append(packet.size)
    if any(actual[i].digest()!=hashes[i].digest() or actual_sizes[i]!=sizes[i] for i in indices):
        raise AssertionError('原音视频包内容校验失败。')
    audio_index=media['audio_index']
    for row in mapping:
        row['audio_start_gap']=row['audio_source_start']-row['source_start']
        row['audio_end_gap']=row['source_end']-row['audio_source_end']
    report={'duration':cursor,'segments':len(mapping),'mapping':mapping,'media_kind':media['kind'],
            'payload_sha256':actual[audio_index].hexdigest(),'payload_unchanged':True,
            'copied_packets':len(sizes[audio_index]),'sample_rate':meta['sample_rate'],'channels':meta['channels'],
            'codec':media['audio_codec'],'average_bitrate':sum(sizes[audio_index])*8/cursor,'bytes':destination.stat().st_size,
            'streams':output_stats,'timeline_review':True,'audio_track':media['audio_index'],
            'source_audio_tracks':media['audio_tracks'],'max_audio_boundary_gap':max(max(r['audio_start_gap'],r['audio_end_gap']) for r in mapping)}
    report.update(timestamps_verified=True,max_timestamp_rounding_error=max_timestamp_error)
    if media['kind']=='video':
        vi=media['video_index']
        report.update(video_payload_sha256=actual[vi].hexdigest(),video_payload_unchanged=True,
                      video_packets=len(sizes[vi]),video_codec=media['video_codec'],
                      keyframe_trim_seconds=sum(r['source_end']-r['source_start'] for r in source_intervals(meta,frames,plan))-cursor)
    return report
