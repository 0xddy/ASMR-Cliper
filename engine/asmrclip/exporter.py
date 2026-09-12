import csv
import hashlib
import os
import shutil
import subprocess
import uuid
from collections import deque
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from .common import event, fingerprint, save_json
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


def export(source, output_dir, meta, frames, plan, cfg, source_fingerprint, reviewer=None, allow_review_findings=False):
    if not plan['keep_frames']:
        raise ValueError('当前规则下没有可保留的片段。请切换宽松模式或调小严格模式参数。')
    from .media import inspect_media,copy_media_packets
    media=inspect_media(source,cfg.get('output_kind','auto'))
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
    filename=f'{name}_ASMR_{mode}{media["extension"]}'
    try:
        event('progress','复制原编码音视频包并校验内容' if media['kind']=='video' else '复制原音频包并校验内容',89)
        if media['kind']=='audio' and meta.get('aac_packet_grid',meta['codec']=='AAC LC'):
            report=copy_packets(source,staging/filename,meta,frames,plan)
            report['media_kind']='audio'
        else:report=copy_media_packets(source,staging/filename,meta,frames,plan,media)
        event('progress','完整解码与拼接处波形检查',94)
        run=subprocess.run([cfg['ffmpeg'],'-v','error','-xerror','-i',str(staging/filename),'-map','0:a:0','-map','0:v:0?','-f','null','-'],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if run.returncode or run.stderr.strip():
            raise RuntimeError('解码校验未通过：'+run.stderr.decode('utf-8',errors='replace')[-1500:])
        report['joins']=check_timeline_joins(staging/filename,report) if report.get('timeline_review') else check_joins(staging/filename,report)
        report['join_review_count']=sum(r['review_suggested'] for r in report['joins'])
        if reviewer is not None:
            from .reviewer import SpeechRemaining
            report['speech_review']=reviewer.inspect(staging/filename,report)
            report['speech_review']['export_mapping']=report['mapping']
            if report['speech_review']['status']!='passed':
                if allow_review_findings and cfg['mode']!='extract' and report['speech_review']['status']=='speech_found':
                    report['speech_review']['status']='needs_review'
                    report['speech_review']['note']='已达到设定复核轮数，仍有模型疑似话语；成片已保存，请按人声复核.csv 的位置复听。'
                else:raise SpeechRemaining(report['speech_review'])
        elif cfg.get('review_enabled',False) or cfg['mode']=='extract':
            raise RuntimeError('请求了成片复核，但复核模型未运行，不能发布结果。')
        else:report['speech_review']={'status':'disabled'}
        report['speech_review']['previous_passes']=plan.get('review_passes',[])
        report.update(source=str(source.resolve()),mode=cfg['mode'],output=str(final_dir/filename),decode_verified=True,
                      language=plan.get('language','auto'),settings={k:cfg[k] for k in ['strict_pre','strict_post','strict_min_section','strict_dense_gap','silence_db','silence_seconds','audit']})
        report['engine_version']='0.6.0'
        report['settings']['output_kind']=cfg.get('output_kind','auto')
        report['settings'].update(review_enabled=cfg.get('review_enabled',False),review_required=cfg['mode']=='extract',review_max_passes=cfg.get('review_max_passes',3))
        report['settings'].update(speech_model=cfg.get('speech_model','whisper-large-v3'),review_model_id=cfg.get('review_model_id','whisper-large-v3'))
        report['settings'].update({k:cfg.get(k,v) for k,v in KEEP_DEFAULTS.items()})
        report['acoustic_exclusion_counts']={k:len(plan.get('acoustic_exclusions',{}).get(k,[])) for k in ('voice','airflow','drinking','expressive_breaks','soft_laugh','loud_laugh','impacts','heartbeat','tapping')}
        if fingerprint(source)!=source_fingerprint:
            raise RuntimeError('处理期间源文件发生变化，请重新开始。')
        with (staging/'剪辑时间对照.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f)
            writer.writerow(['片段','源文件开始','源文件结束','剪辑版开始','剪辑版结束'])
            for i,s in enumerate(report['mapping'],1):
                writer.writerow([i,clock(s['source_start']),clock(s['source_end']),clock(s['output_start']),clock(s['output_end'])])
        save_json(staging/'校验报告.json',report)
        save_json(staging/'剪辑计划.json',plan)
        with (staging/'人声复核.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.writer(f);writer.writerow(['成片开始','成片结束','模型疑似文字','状态'])
            for s in report['speech_review'].get('findings',[]):writer.writerow([clock(s['start']),clock(s['end']),s['text'],'待复听'])
        review_status={'passed':'模型未检出残留话语','needs_review':'仍有疑似话语，待复听位置见人声复核.csv','disabled':'未开启'}[report['speech_review']['status']]
        video_note=(f"视频 {report['video_codec']} 原编码复制；关键帧向内调整额外剪去 {report['keyframe_trim_seconds']:.3f} 秒。\n"
                    f"视频包原样校验通过；音画使用同一时间轴，音轨边界留空最多 {report['max_audio_boundary_gap']*1000:.1f} 毫秒。\n") if media['kind']=='video' else ''
        note=f'''ASMR-Cliper 0.6.0 · {title}
时长：{clock(report['duration'])}；保留 {report['segments']} 段。
{video_note}复制原 {report['codec']} 音频包，未重新编码。采样率 {report['sample_rate']} Hz；声道 {report['channels']}。
保留帧 SHA-256 与包长度校验通过；完整解码检查通过。
VBR 平均码率约 {report['average_bitrate']/1000:.1f} kb/s，会随所保留的内容变化。
拼接处建议复听数量：{report['join_review_count']}，详情见校验报告。
模式参数与切点依据见剪辑计划和校验报告；原音频未修改。
使用首个音轨分析并输出；其他音轨、字幕与附件不输出，以免带回未经检查的人声或失效时间轴。
说话声始终删除。其他声音按“保留声音”选项处理；默认保留轻笑、心跳和 ASMR 道具敲击。检出区间及保留选项见剪辑计划中的 acoustic_exclusions 和校验报告 settings。
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
