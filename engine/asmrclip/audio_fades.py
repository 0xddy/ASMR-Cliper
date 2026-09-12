"""Optional, sample-clock fades at output joins; video and edit times stay intact."""
import hashlib
import os
import subprocess
import tempfile
from itertools import zip_longest
from pathlib import Path

import av
import numpy as np

from .common import event
from .media import stream_signature
from .progress import activity


def ramps_for(mapping, rate, seconds):
    ramps=[];joins=[]
    for left,right in zip(mapping,mapping[1:]):
        cut=round(right['output_start']*rate)
        # Reserve the middle half even in unusually short retained fragments.
        before=max(1,round(min(seconds,(left['output_end']-left['output_start'])/4)*rate))
        after=max(1,round(min(seconds,(right['output_end']-right['output_start'])/4)*rate))
        joins.append({'time':cut/rate,'fade_out_seconds':before/rate,'fade_in_seconds':after/rate})
        ramps.extend([{'start':cut-before,'end':cut,'in':False,'energy':0.,'samples':0},
                      {'start':cut,'end':cut+after,'in':True,'energy':0.,'samples':0}])
    return ramps,joins


def pcm_blocks(path, report):
    """Native-rate planar float PCM, streaming on the reviewed playback clock."""
    rate=report['sample_rate'];channels=report['channels'];limit=round(report['duration']*rate);cursor=0
    resampler=av.AudioResampler(format='fltp',rate=rate)
    def append(frame):
        nonlocal cursor
        data=frame.to_ndarray()
        start=round(float(frame.pts*frame.time_base)*rate) if report.get('timeline_review') and frame.pts is not None else cursor
        if start-cursor>rate:raise ValueError('淡化音轨时间轴存在超过一秒的空洞。')
        if cursor<min(start,limit):
            end=min(start,limit);yield cursor,np.zeros((channels,end-cursor),np.float32);cursor=end
        if start<cursor:data=data[:,min(data.shape[1],cursor-start):]
        data=data[:,:max(0,limit-cursor)]
        if data.shape[1]:yield cursor,data;cursor+=data.shape[1]
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            if not report.get('timeline_review'):frame.pts=None
            for output in resampler.resample(frame):yield from append(output)
            if cursor>=limit:break
        for output in resampler.resample(None):yield from append(output)
    if limit-cursor>rate:raise ValueError('淡化音轨长度与剪辑时间轴不一致。')
    if cursor<limit:yield cursor,np.zeros((channels,limit-cursor),np.float32)


def envelope(blocks, ramps):
    index=0
    for offset,pcm in blocks:
        end=offset+pcm.shape[1];out=pcm.copy()
        while index<len(ramps) and ramps[index]['end']<=offset:index+=1
        for current in range(index,len(ramps)):
            row=ramps[current]
            if row['start']>=end:break
            lo=max(offset,row['start']);hi=min(end,row['end'])
            if hi<=lo:continue
            a,b=lo-offset,hi-offset
            raw=pcm[:,a:b].astype(np.float64)
            row['energy']+=float(np.square(raw).sum());row['samples']+=raw.size
            gain=(np.arange(lo,hi,dtype=np.float64)-row['start'])/(row['end']-row['start'])
            if not row['in']:gain=1-gain
            out[:,a:b]*=gain.astype(np.float32)
        yield offset,out


def encoder_options(source, report):
    with av.open(str(source)) as container:
        codec=container.streams.audio[0].codec_context
        name=codec.name;profile=codec.profile;rate=codec.sample_rate;layout=codec.layout.name
        sample_format=codec.format.name if codec.format else None
        bitrate=codec.bit_rate or round(report['average_bitrate'])
    encoders={'aac':'aac','mp3float':'libmp3lame','mp3':'libmp3lame','opus':'libopus',
              'vorbis':'libvorbis','flac':'flac','alac':'alac','ac3':'ac3','eac3':'eac3',
              'wmav2':'wmav2','wmav1':'wmav1'}
    encoder=encoders.get(name,name if name.startswith('pcm_') else None)
    if not encoder or (name=='aac' and profile!='LC'):
        raise ValueError(f'当前不支持对 {name} / {profile} 保持编码类型淡化，请关闭接缝淡化后导出。')
    args=['-c:a',encoder,'-ar',str(rate),'-ac',str(report['channels'])]
    lossy=name not in ('flac','alac') and not name.startswith('pcm_')
    if lossy:args+=['-b:a',str(max(8000,bitrate))]
    if name=='aac':args+=['-aac_pns','0']
    if name in ('flac','alac') and sample_format:args+=['-sample_fmt','s16p' if name=='alac' and sample_format.startswith('s16') else 's32p' if name=='alac' else 's16' if sample_format.startswith('s16') else 's32']
    return args,{'source_codec':name,'encoder':encoder,'sample_rate':rate,'layout':layout,
                 'target_bitrate':max(8000,bitrate) if lossy else None}


def packet_summary(path):
    digest=hashlib.sha256();size=0;count=0
    with av.open(str(path)) as container:
        stream=container.streams.audio[0];signature=stream_signature(stream)
        for packet in container.demux(stream):
            if packet.size:digest.update(bytes(packet));size+=packet.size;count+=1
    return {'payload_sha256':digest.hexdigest(),'encoded_packets':count,
            'encoded_payload_bytes':size,'signature':signature}


def apply_fades(path, report, cfg, source):
    """Encode audio once, before speech review. No source or final file is edited."""
    if not cfg.get('join_fade_enabled',False) or len(report['mapping'])<2:return report
    path=Path(path);target=path.with_name('faded-'+path.name)
    from .pauses import fade_seconds
    rate=report['sample_rate'];seconds=fade_seconds(cfg)
    ramps,joins=ramps_for(report['mapping'],rate,seconds)
    options,encoding=encoder_options(source,report)
    command=[str(cfg['ffmpeg']),'-hide_banner','-v','error','-nostdin','-y',
             '-f','f32le','-ar',str(rate),'-ac',str(report['channels']),'-channel_layout',encoding['layout'],
             '-i','pipe:0','-map','0:a:0',*options,'-threads:a','2','-t',f"{report['duration']:.9f}"]
    if target.suffix.lower() in ('.m4a','.mp4','.mov'):command+=['-movflags','+faststart']
    command+=[str(target)]
    event('progress',f'接缝淡出 / 淡入，每侧最多 {seconds:g} 秒（音轨重新编码）',89)
    try:
        with tempfile.TemporaryFile() as errors:
            process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=errors,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                last=-1
                try:
                    for offset,pcm in envelope(pcm_blocks(path,report),ramps):
                        process.stdin.write(np.ascontiguousarray(pcm.T,dtype='<f4').tobytes())
                        percent=int(100*(offset+pcm.shape[1])/rate/report['duration'])
                        if percent!=last:
                            activity(f'音频接缝淡化 {percent}% · 音轨重新编码')
                            event('progress',f'音频接缝淡化 {percent}%',89+percent*.07);last=percent
                    process.stdin.close()
                except BrokenPipeError:
                    # Read the actual encoder error below.
                    pass
                code=process.wait()
                errors.seek(0,os.SEEK_END);errors.seek(max(0,errors.tell()-3000))
                detail=errors.read().decode('utf-8',errors='replace').strip()
                if code or detail:raise RuntimeError('音频接缝淡化失败：'+detail)
            finally:
                if process.poll() is None:process.kill();process.wait()
                if not process.stdin.closed:
                    try:process.stdin.close()
                    except BrokenPipeError:pass
        summary=packet_summary(target);signature=summary.pop('signature')
        if (signature['codec']!=encoding['source_codec'] or signature['sample_rate']!=rate
                or signature['channels']!=report['channels'] or not summary['encoded_packets']):
            raise AssertionError('淡化改变了音频编码类型、采样率或声道配置。')
        for i,join in enumerate(joins):
            levels=[10*np.log10(max(r['energy']/max(1,r['samples']),1e-12)) for r in ramps[i*2:i*2+2]]
            join.update(left_rms_db=round(float(levels[0]),2),right_rms_db=round(float(levels[1]),2),
                        level_difference_db=round(float(levels[1]-levels[0]),2))
        result={**report,**summary,'payload_unchanged':False,'timeline_review':True,'bytes':target.stat().st_size,
                'average_bitrate':summary['encoded_payload_bytes']*8/report['duration'],
                'audio_fades':{'enabled':True,'applied':True,'seconds':seconds,'curve':'linear',
                               'requested_seconds':cfg.get('join_fade_seconds',.3),
                               'timeline_unchanged':True,'encoding':encoding,'joins':joins}}
        result.pop('copied_packets',None)
        if 'streams' in result:result['streams']={**result['streams'],'audio':signature}
        os.replace(target,path)
        return result
    finally:
        if target.exists():target.unlink()


def verify_stream_copy(original, final, kind):
    """Check every packet and its presentation clock after replacing a video audio track."""
    with av.open(str(original)) as a,av.open(str(final)) as b:
        left=getattr(a.streams,kind)[0];right=getattr(b.streams,kind)[0]
        if stream_signature(left)!=stream_signature(right):raise AssertionError('合并淡化音轨后编码参数发生变化。')
        for p,q in zip_longest((p for p in a.demux(left) if p.size),(p for p in b.demux(right) if p.size)):
            if p is None or q is None or bytes(p)!=bytes(q):raise AssertionError('合并淡化音轨后原编码包校验失败。')
            if p.pts is None or q.pts is None or abs(float(p.pts*p.time_base-q.pts*q.time_base))>max(float(p.time_base),float(q.time_base))+1e-6:
                raise AssertionError('合并淡化音轨后音画时间轴发生偏移。')


def mux_faded_audio(video, audio, original_report, faded_report, cfg):
    from .audio_source import run_ffmpeg
    video=Path(video);target=video.with_name('joined-'+video.name)
    command=[str(cfg['ffmpeg']),'-hide_banner','-v','error','-xerror','-nostdin','-y','-copyts',
             '-i',str(video),'-i',str(audio),'-map','0:v:0','-map','1:a:0','-map_metadata','0',
             '-c','copy','-avoid_negative_ts','disabled']
    if target.suffix.lower() in ('.mp4','.mov'):command+=['-movflags','+faststart','-movie_timescale','1000000','-write_tmcd','0']
    command+=[str(target)]
    try:
        run_ffmpeg(command,'合并原编码画面与已复核的淡化音轨')
        verify_stream_copy(video,target,'video');verify_stream_copy(audio,target,'audio')
        result={**original_report,**{key:faded_report[key] for key in
                ('payload_sha256','encoded_packets','encoded_payload_bytes','payload_unchanged','average_bitrate','audio_fades')}}
        result.pop('copied_packets',None)
        result['streams']={**original_report['streams'],'audio':packet_summary(target)['signature']}
        result['bytes']=target.stat().st_size
        result['reviewed_audio_payload_verified']=True
        os.replace(target,video)
        return result
    finally:
        if target.exists():target.unlink()
