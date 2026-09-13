import csv
import hashlib
import os
import shutil
import uuid
from collections import deque
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from .common import event, fingerprint, save_json
from . import __version__
from .exclusions import KEEP_DEFAULTS


def clock(seconds):
    ms=round(seconds*1000)
    sec,ms=divmod(ms,1000)
    minute,sec=divmod(sec,60)
    hour,minute=divmod(minute,60)
    return f'{hour:02}:{minute:02}:{sec:02}.{ms:03}'


def copy_packets(source, destination, meta, frames, plan):
    keeps=plan['keep_frames']
    if not keeps:
        raise ValueError('当前规则下没有可保留的片段。可切换宽松模式，或调小严格模式的余量 / 最短片段时长。')
    packet_indices=frames['packets']
    intervals=[(int(packet_indices[a]),int(packet_indices[b-1])+1) for a,b in keeps]
    expected=hashlib.sha256()
    sizes=[]
    mapping=[]
    tick=0
    rate=meta['sample_rate']
    frame_samples=meta['frame_samples']
    with av.open(str(source)) as src, av.open(str(destination),'w',format='ipod',options={'movflags':'+faststart'}) as dst:
        instream=src.streams.audio[0]
        outstream=dst.add_stream_from_template(instream)
        outstream.time_base=Fraction(1,rate)
        dst.metadata.update(src.metadata)
        cursor,ordinal=-1,-1
        cursor=0
        preroll=deque(maxlen=2)
        started=False

        def mux(p, pts):
            expected.update(bytes(p))
            sizes.append(p.size)
            p.pts=p.dts=pts
            p.duration=frame_samples
            p.time_base=Fraction(1,rate)
            p.stream=outstream
            dst.mux(p)

        for packet in src.demux(instream):
            if not packet.size:
                continue
            ordinal+=1
            if not started and ordinal<intervals[0][0]:
                preroll.append(packet)
                continue
            while cursor<len(intervals) and ordinal>=intervals[cursor][1]:
                cursor+=1
            if cursor>=len(intervals):
                break
            if ordinal<intervals[cursor][0]:
                continue
            if not started:
                for i,p in enumerate(preroll):
                    mux(p,(i-len(preroll))*frame_samples)
                started=True
            start_time=float(packet.pts*packet.time_base) if packet.pts is not None else 0
            end_time=start_time+frame_samples/rate
            if len(mapping)<=cursor:
                mapping.append({'source_start':start_time,'output_start':tick/rate,'packets':0,
                                'analysis_start':keeps[cursor][0]*frame_samples/rate,
                                'analysis_end':keeps[cursor][1]*frame_samples/rate})
            mux(packet,tick)
            tick+=frame_samples
            mapping[cursor].update(source_end=end_time,output_end=tick/rate,packets=mapping[cursor]['packets']+1)
    if len(mapping)!=len(keeps) or tick!=sum(b-a for a,b in keeps)*frame_samples:
        raise ValueError('源音频包映射不完整，已停止导出。')
    actual=hashlib.sha256()
    output_sizes=[]
    last_pts=None
    with av.open(str(destination)) as check:
        st=check.streams.audio[0]
        if st.codec_context.name!='aac' or st.codec_context.sample_rate!=rate or len(st.codec_context.layout.channels)!=meta['channels']:
            raise AssertionError('音频编码参数发生变化')
        for p in check.demux(st):
            if not p.size:
                continue
            if p.pts is None or (last_pts is not None and p.pts<=last_pts):
                raise AssertionError('导出时间戳不连续')
            last_pts=p.pts
            actual.update(bytes(p))
            output_sizes.append(p.size)
    if actual.digest()!=expected.digest() or sizes!=output_sizes:
        raise AssertionError('原始 AAC 帧校验失败')
    return {'duration':tick/rate,'segments':len(mapping),'mapping':mapping,'copied_packets':len(sizes),
            'payload_sha256':actual.hexdigest(),'payload_unchanged':True,
            'sample_rate':rate,'channels':meta['channels'],'codec':'AAC LC',
            'average_bitrate':sum(sizes)*8/(tick/rate),'bytes':destination.stat().st_size}


def check_joins(path, report):
    boundaries={round(s['output_start']*report['sample_rate']/1024) for s in report['mapping'][1:]}
    result=[]
    previous=deque(maxlen=4)
    with av.open(str(path)) as container:
        index=-1
        for frame in container.decode(audio=0):
            index+=1
            x=frame.to_ndarray().reshape(report['channels'],-1)
            previous.append(x)
            if index-1 not in boundaries or len(previous)<4:
                continue
            samples=np.concatenate(list(previous),axis=1)
            diff=np.abs(np.diff(samples,axis=1))
            cut=2048
            jump=float(diff[:,cut-8:cut+8].max())
            nearby=np.concatenate([diff[:,:cut-64].ravel(),diff[:,cut+64:].ravel()])
            normal=float(np.quantile(nearby,.999))
            ratio=jump/max(normal,1e-8)
            result.append({'time':(index-1)*1024/report['sample_rate'],'jump':jump,'ratio':ratio,
                           'review_suggested':bool(jump>.01 and ratio>3)})
    return result


def validate_decode(path, ffmpeg, report):
    from .audio_source import run_ffmpeg
    event('progress', '最终成片完整校验（限制解码线程）', 98)
    command = [str(ffmpeg), '-hide_banner', '-v', 'error', '-xerror', '-nostdin', '-nostats',
               '-progress', 'pipe:1', '-threads:v', '2', '-threads:a', '2',
               '-filter_threads', '1', '-filter_complex_threads', '1', '-i', str(path),
               '-map', '0:a:0', '-map', '0:v:0?', '-f', 'null', '-']
    run_ffmpeg(command, '最终成片完整解码校验', report['duration'], (98, 99))


def verify_reviewed_audio(reviewed, final):
    # Final video must contain exactly the audio payload and clock that were reviewed.
    if reviewed['payload_sha256'] != final['payload_sha256'] or reviewed['copied_packets'] != final['copied_packets']:
        raise AssertionError('最终视频音轨与已复核音轨内容不一致。')
    keys = ('source_start', 'source_end', 'output_start', 'output_end', 'analysis_start', 'analysis_end',
            'audio_source_start', 'audio_source_end', 'audio_packets')
    if (len(reviewed['mapping']) != len(final['mapping']) or
            any(a[key] != b[key] for a, b in zip(reviewed['mapping'], final['mapping']) for key in keys)):
        raise AssertionError('最终视频音轨与已复核音轨时间轴不一致。')


def export(source, output_dir, meta, frames, plan, cfg, source_fingerprint, reviewer=None, allow_review_findings=False,
           media_context=None):
    if not plan['keep_frames']:
        raise ValueError('当前规则下没有可保留的片段。请切换宽松模式或调小严格模式参数。')
    from .media import inspect_media,copy_media_packets,align_video,source_intervals,video_groups
    from .audio_fades import apply_fades,mux_faded_audio,audio_processing_enabled,output_extension,fade_status
    media=media_context if media_context is not None else inspect_media(source,cfg.get('output_kind','auto'))
    precise=media['kind']=='video' and cfg.get('video_cut_mode','copy')=='precise'
    if precise:
        from .video_encode import align_frames,encode_video,mux_video
        # H.264 / HEVC pictures need a compatible container, even for WebM inputs.
        media['extension']='.mp4' if media['extension'] in ('.mp4','.mov') and media['audio_codec'] in ('aac','alac','mp3') else '.mkv'
    output_dir=Path(output_dir).resolve()
    output_dir.mkdir(parents=True,exist_ok=True)
    token=uuid.uuid4().hex[:8]
    staging=output_dir/f'.asmrclip-{token}.part'
    staging.mkdir()
    mode={'strict':'v2','relaxed':'v3','extract':'v4'}[cfg['mode']]
    title={'strict':'严格模式（V2）','relaxed':'宽松模式（V3）','extract':'提取模式（V4）'}[cfg['mode']]
    source=Path(source)
    name=source.stem[:100]
    final_dir=output_dir/f'{name}_{mode}_{datetime.now():%Y%m%d_%H%M%S}_{token}'
    extension=output_extension(media,cfg)
    filename=f'{name}_ASMR_{mode}{extension}'
    raw_audio=staging/('source-audio'+media["extension"])
    processed_audio=staging/('audio-review'+extension if media["kind"]=='video' else filename)
    try:
        candidate=None;intervals=None;report=None
        if media['kind']=='video':
            # Packet metadata only; cache the keyframe index across speech-review retries.
            if precise:
                event('progress','按视频帧精度确定切点（无需完整关键帧组）',89)
                intervals=align_frames(source,meta,frames,plan,media)
            else:
                if '_video_groups' not in media:media['_video_groups']=video_groups(source,media['video_index'])
                intervals=align_video(source,media['video_index'],source_intervals(meta,frames,plan),media['_video_groups'])
                from .pauses import limit_video
                intervals=limit_video(intervals,media['_video_groups'],frames['levels'],meta['frame_samples']/meta['sample_rate'],cfg)
            if precise or reviewer is not None or audio_processing_enabled(cfg):
                event('progress','按最终视频切点准备独立音轨（不处理画面）',89)
                candidate=processed_audio
                audio_media={**media,'kind':'audio','video_index':None,'video_codec':None}
                report=copy_media_packets(source,raw_audio,meta,frames,plan,audio_media,intervals)
        elif meta.get('aac_packet_grid',meta['codec']=='AAC LC'):
            event('progress','复制原音频包并校验内容',89)
            report=copy_packets(source,raw_audio,meta,frames,plan)
            report['media_kind']='audio'
        else:report=copy_media_packets(source,raw_audio,meta,frames,plan,media)
        if report is not None:
            report=apply_fades(raw_audio,report,cfg,source,processed_audio)
            if processed_audio.exists():raw_audio.unlink()
            else:os.replace(raw_audio,processed_audio)
            report.setdefault('audio_fades',fade_status(cfg))
        if reviewer is not None:
            from .reviewer import SpeechRemaining, mapped_output_to_source
            review=reviewer.inspect(candidate or staging/filename,report)
            review['export_mapping']=report['mapping']
            if review['status']!='passed':
                if review['status']!='speech_found':
                    raise RuntimeError('成片复核未正常完成，请查看模型运行日志。')
                mapped=mapped_output_to_source(review['findings'],report['mapping'])
                if allow_review_findings or not mapped:
                    review['status']='needs_review'
                    review['note']='仍有模型疑似话语，成片已保存；请在处理记录中查看待复听位置。'
                    if not mapped:review['note']+=' 疑似位置无法映射为可调整的源片段，保留当前候选。'
                else:raise SpeechRemaining(review)
        elif cfg.get('review_enabled',False) or cfg['mode']=='extract':
            raise RuntimeError('请求了成片复核，但复核模型未运行，不能发布结果。')
        else:review={'status':'disabled'}
        from .progress import phase
        phase(6, '复核通过，准备导出' if review['status']=='passed' else '准备最终导出与校验', legacy=(98,100))
        if media['kind']=='video':
            if precise:
                event('progress','音轨已确认，开始精确剪辑并编码画面',98)
                video=staging/('video-encoded'+extension)
                encoded=encode_video(source,video,media,report,cfg)
                final=mux_video(video,candidate,staging/filename,report,encoded,cfg)
                final['video_frame_trim_seconds']=sum(r['source_end']-r['source_start'] for r in source_intervals(meta,frames,plan))-final['duration']
                video.unlink()
            else:
                event('progress','按已确认时间轴复制视频与音轨，合并封装',98)
                final=copy_media_packets(source,staging/filename,meta,frames,plan,{**media,'extension':extension},intervals)
                if candidate is not None:
                    if not report['payload_unchanged']:
                        final=mux_faded_audio(staging/filename,candidate,final,report,cfg)
                    else:verify_reviewed_audio(report,final)
            if candidate is not None:
                review['export_mapping']=final['mapping'];candidate.unlink()
            report=final
            report['audio_review_before_video_export']=reviewer is not None
        report.setdefault('audio_fades',fade_status(cfg))
        report['speech_review']=review
        event('progress','检查最终音轨拼接处',98)
        report['joins'],report['pause_check']=inspect_output_sound(staging/filename,report,cfg)
        if not report['pause_check']['within_limit']:
            event('log',f"成片仍有超过 {cfg.get('max_pause_seconds',1.5):g} 秒的低电平空窗，位置已记入空窗检查.csv。")
        report['join_review_count']=sum(r['review_suggested'] for r in report['joins'])
        validate_decode(staging/filename,cfg['ffmpeg'],report)
        report['decode_validation']={'scope':'final_output_only','video_threads':2,'audio_threads':2}
        report['audio_preparation']=meta.get('audio_preparation',{'method':'cached_or_direct_audio_only'})
        report['speech_review']['previous_passes']=plan.get('review_passes',[])
        report.update(source=str(source.resolve()),mode=cfg['mode'],output=str(final_dir/filename),decode_verified=True,
                      language=plan.get('language','auto'),settings={k:cfg[k] for k in ['strict_pre','strict_post','strict_min_section','strict_dense_gap','silence_db','max_pause_seconds','audit']})
        report['engine_version']=__version__
        report['settings']['output_kind']=cfg.get('output_kind','auto')
        report['settings']['video_cut_mode']=cfg.get('video_cut_mode','copy')
        report['settings']['audio_output_codec']=cfg.get('audio_output_codec','source')
        report['settings'].update(join_fade_enabled=cfg.get('join_fade_enabled',False),join_fade_seconds=cfg.get('join_fade_seconds',.3),
                                  edge_fade_enabled=cfg.get('edge_fade_enabled',True),edge_fade_seconds=cfg.get('edge_fade_seconds',.5))
        report['settings'].update(review_enabled=cfg.get('review_enabled',False),review_required=cfg['mode']=='extract',review_max_passes=cfg.get('review_max_passes',3))
        report['settings'].update(speech_model=cfg.get('speech_model','whisper-large-v3'),review_model_id=cfg.get('review_model_id','whisper-large-v3'))
        report['settings'].update({k:cfg.get(k,v) for k,v in KEEP_DEFAULTS.items()})
        report['acoustic_exclusion_counts']={k:len(plan.get('acoustic_exclusions',{}).get(k,[])) for k in ('voice','airflow','drinking','expressive_breaks','soft_laugh','loud_laugh','impacts','heartbeat','tapping','transitions')}
        report['transition_review']=plan.get('acoustic_exclusions',{}).get('transition_review',{})
        report['drinking_review']=plan.get('acoustic_exclusions',{}).get('drinking_review',{})
        if fingerprint(source)!=source_fingerprint:
            raise RuntimeError('处理期间源文件发生变化，请重新开始。')
        with (staging/'剪辑时间对照.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f)
            writer.writerow(['片段','源文件开始','源文件结束','剪辑版开始','剪辑版结束'])
            for i,s in enumerate(report['mapping'],1):
                writer.writerow([i,clock(s['source_start']),clock(s['source_end']),clock(s['output_start']),clock(s['output_end'])])
        save_json(staging/'校验报告.json',report)
        save_json(staging/'剪辑计划.json',plan)
        with (staging/'空窗检查.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['成片开始','成片结束','空窗秒数','设置上限秒数'])
            for row in report['pause_check']['over_limit']:
                writer.writerow([clock(row['start']),clock(row['end']),round(row['duration'],3),cfg.get('max_pause_seconds',1.5)])
        with (staging/'接缝淡化.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['成片接缝','淡出秒数','淡入秒数','左侧音量 dBFS','右侧音量 dBFS','右侧减左侧 dB'])
            for row in report['audio_fades']['joins']:
                writer.writerow([clock(row['time']),row['fade_out_seconds'],row['fade_in_seconds'],row['left_rms_db'],row['right_rms_db'],row['level_difference_db']])
        with (staging/'首尾淡化.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['位置','成片开始','成片结束','实际淡化秒数'])
            for row in report['audio_fades']['edges']:
                writer.writerow(['开头淡入' if row['kind']=='fade_in' else '结尾淡出',clock(row['start']),clock(row['end']),row['seconds']])
        with (staging/'人声复核.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['成片开始','成片结束','模型疑似文字','状态'])
            for s in report['speech_review'].get('findings',[]):writer.writerow([clock(s['start']),clock(s['end']),s['text'],'待复听'])
        with (staging/'过渡复核.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['原片分析开始','原片分析结束','检测判定','依据'])
            labels={'remove':'确认中断残留','keep_asmr':'存在声音动作','uncertain':'未确认，不据此删除'}
            for r in report['transition_review'].get('candidates',[]):
                writer.writerow([clock(r['start']),clock(r['end']),labels[r['decision']],r['reason']])
        with (staging/'饮水复核.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['原片分析开始','原片分析结束','检测判定','依据'])
            for r in report['drinking_review'].get('candidates',[]):
                writer.writerow([clock(r['start']),clock(r['end']),'确认饮水休息' if r['decision']=='remove' else '未确认，不据此删除',r['reason']])
        review_status={'passed':'模型未检出残留话语','needs_review':'仍有疑似话语，待复听位置见人声复核.csv','disabled':'未开启'}[report['speech_review']['status']]
        video_note=(f"视频 {report['video_codec']} 精确切割并重新编码（{report['video_encoding']['encoder']}）；按视频帧向内调整额外剪去 {report['video_frame_trim_seconds']:.3f} 秒。\n"
                    "画面经过有损编码，源分辨率和保留帧时间轴不变；最终音轨与已确认音轨的包内容和时间戳校验通过。\n") if precise else (
                    f"视频 {report['video_codec']} 原编码复制；关键帧向内调整额外剪去 {report['keyframe_trim_seconds']:.3f} 秒。\n"
                    f"视频包原样校验通过；音画使用同一时间轴，音轨边界留空最多 {report['max_audio_boundary_gap']*1000:.1f} 毫秒。\n") if media['kind']=='video' else ''
        audio_note=(f"音轨已处理；首尾 {len(report['audio_fades']['edges'])} 处，接缝 {len(report['audio_fades']['joins'])} 处；音轨重新编码为 {report['codec']}。\n"
                    "淡出后淡入，不重叠片段、不改变时间轴。\n"
                    "采样率与声道沿用源音轨；编码和目标码率见校验报告 audio_encoding。实际淡化位置见首尾淡化.csv、接缝淡化.csv。") if not report['payload_unchanged'] else (
                    f"复制原 {report['codec']} 音频包，未重新编码。保留帧 SHA-256 与包长度校验通过。")
        note=f'''ASMR-Cliper {__version__} · {title}
时长：{clock(report['duration'])}；保留 {report['segments']} 段。
{video_note}{audio_note}
采样率 {report['sample_rate']} Hz；声道 {report['channels']}。完整解码检查通过。
VBR 平均码率约 {report['average_bitrate']/1000:.1f} kb/s，会随所保留的内容变化。
拼接处建议复听数量：{report['join_review_count']}，详情见校验报告。
最长空窗期设置：{cfg.get('max_pause_seconds',1.5):g} 秒；实际低电平连续空窗最长约 {report['pause_check']['max_detected_seconds']:g} 秒。静音按电平与峰值共同判断，超限位置见空窗检查.csv。
模式参数与切点依据见剪辑计划和校验报告；原音频未修改。
使用首个音轨分析并输出；其他音轨、字幕与附件不输出，以免带回未经检查的人声或失效时间轴。
说话声始终删除。其他声音按“保留声音”选项处理；默认保留轻笑、心跳和 ASMR 道具敲击。检出区间及保留选项见剪辑计划中的 acoustic_exclusions 和校验报告 settings。
音色突然变闷后的短过渡，只有确认是非 ASMR 中断残留才删除；正常低频 ASMR 保留。候选、模型证据和判定见校验报告 transition_review，时间使用原片分析时间轴。
饮水休息另经声音语义与连续动作复核，按证据关联前后瓶盖动作；单独开瓶、水声或含糊口腔音不据此删除。判定见饮水复核.csv 和校验报告 drinking_review，时间使用原片分析时间轴。
成片大模型复核：{review_status}。复核模型、覆盖范围和修改记录见校验报告。V4 只接纳正向声学证据区间，勾选声音不会将休息或未知声音变成提取目标。
这是本地模型自动剪辑结果。轻声说话与口腔音有时会混淆，声学检查不能保证每一处主观听感无缝。
'''
        (staging/'剪辑说明.txt').write_text(note,encoding='utf-8')
        os.replace(staging,final_dir)
        return report
    finally:
        if staging.exists() and staging.resolve().parent==output_dir and staging.name.startswith('.asmrclip-'):
            shutil.rmtree(staging)


def check_timeline_joins(path,report):
    """Check small PCM windows around video-clock joins, with bounded memory."""
    rate=report['sample_rate'];channels=report['channels']
    points=[round(row['output_start']*rate) for row in report['mapping'][1:]]
    results=[];index=0;cursor=0;origin=0;buffer=np.empty((channels,0),np.float32)
    resampler=av.AudioResampler(format='fltp',rate=rate)
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                x=converted.to_ndarray()
                start=round(float(converted.pts*converted.time_base)*rate) if converted.pts is not None else cursor
                if start>cursor:
                    if start-cursor>rate:raise ValueError('成片音轨出现超过一秒的空洞。')
                    buffer=np.concatenate((buffer,np.zeros((channels,start-cursor),np.float32)),axis=1);cursor=start
                if start<cursor:x=x[:,min(x.shape[1],cursor-start):]
                buffer=np.concatenate((buffer,x),axis=1);cursor+=x.shape[1]
                while index<len(points) and points[index]+2048<=cursor:
                    cut=points[index]-origin
                    if cut>=2048:
                        samples=buffer[:,cut-2048:cut+2048];diff=np.abs(np.diff(samples,axis=1))
                        jump=float(diff[:,2040:2056].max())
                        nearby=np.concatenate((diff[:,:1984].ravel(),diff[:,2112:].ravel()))
                        ratio=jump/max(float(np.quantile(nearby,.999)),1e-8)
                        results.append({'time':points[index]/rate,'jump':jump,'ratio':ratio,'review_suggested':bool(jump>.01 and ratio>3)})
                    index+=1
                if buffer.shape[1]>8192:origin=cursor-8192;buffer=buffer[:,-8192:]
    return results


def inspect_output_sound(path,report,cfg):
    """Check join discontinuities and final quiet runs in the same audio pass."""
    from .audio_fades import pcm_blocks
    from .pauses import QuietMeter
    rate=report['sample_rate'];channels=report['channels']
    meter=QuietMeter(rate,channels,cfg['silence_db'])
    points=[round(row['output_start']*rate) for row in report['mapping'][1:]]
    results=[];index=0;origin=0;buffer=np.empty((channels,0),np.float32)
    for offset,x in pcm_blocks(path,report):
        meter.add(x);buffer=np.concatenate((buffer,x),axis=1);cursor=offset+x.shape[1]
        while index<len(points) and points[index]+2048<=cursor:
            cut=points[index]-origin
            if cut>=2048:
                samples=buffer[:,cut-2048:cut+2048];diff=np.abs(np.diff(samples,axis=1))
                jump=float(diff[:,2040:2056].max());nearby=np.concatenate((diff[:,:1984].ravel(),diff[:,2112:].ravel()))
                ratio=jump/max(float(np.quantile(nearby,.999)),1e-8)
                results.append({'time':points[index]/rate,'jump':jump,'ratio':ratio,'review_suggested':bool(jump>.01 and ratio>3)})
            index+=1
        if buffer.shape[1]>8192:origin=cursor-8192;buffer=buffer[:,-8192:]
    return results,meter.finish(cfg.get('max_pause_seconds',1.5))
