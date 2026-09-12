"""Sample-clock fades at output edges and joins; video and edit times stay intact."""
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


def edges_for(mapping, rate, seconds, duration):
    ramps=[];edges=[]
    for row,incoming in ((mapping[0],True),(mapping[-1],False)):
        length=max(1,round(min(seconds,(row['output_end']-row['output_start'])/4)*rate))
        start=0 if incoming else max(0,round(duration*rate)-length)
        end=length if incoming else round(duration*rate)
        ramps.append({'start':start,'end':end,'in':incoming,'energy':0.,'samples':0})
        edges.append({'kind':'fade_in' if incoming else 'fade_out','start':start/rate,'end':end/rate,'seconds':(end-start)/rate})
    return ramps,edges


def audio_processing_enabled(cfg):
    return fades_enabled(cfg) or cfg.get('audio_output_codec','source')!='source'


def output_extension(media,cfg):
    selected=cfg.get('audio_output_codec','source')
    if selected=='source':return media['extension']
    if media['kind']=='audio':return {'aac':'.m4a','flac':'.flac','pcm':'.wav'}[selected]
    if selected in ('flac','pcm') or media['extension']=='.webm':return '.mkv'
    return media['extension']


def fades_enabled(cfg):
    return cfg.get('join_fade_enabled',False) or cfg.get('edge_fade_enabled',True)


def fade_status(cfg):
    return {'enabled':fades_enabled(cfg),'applied':False,'seconds':cfg.get('join_fade_seconds',.3),
            'join_enabled':cfg.get('join_fade_enabled',False),'edge_enabled':cfg.get('edge_fade_enabled',True),
            'edge_requested_seconds':cfg.get('edge_fade_seconds',.5),'joins':[],'edges':[]}


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
            gain=(np.arange(lo,hi,dtype=np.float64)-row['start'])/max(1,row['end']-row['start']-1)
            if not row['in']:gain=1-gain
            if row['end']-row['start']==1:gain[:]=0
            out[:,a:b]*=gain.astype(np.float32)
        yield offset,out


def encoder_options(source, report, selected='source'):
    with av.open(str(source)) as container:
        codec=container.streams.audio[0].codec_context
        name=codec.name;profile=codec.profile;rate=codec.sample_rate;layout=codec.layout.name
        sample_format=codec.format.name if codec.format else None
        bitrate=codec.bit_rate or round(report['average_bitrate'])
        bitrate_basis='source_stream' if codec.bit_rate else 'retained_audio_packets'
    encoders={'aac':'aac','mp3float':'libmp3lame','mp3':'libmp3lame','opus':'libopus',
              'vorbis':'libvorbis','flac':'flac','alac':'alac','ac3':'ac3','eac3':'eac3',
              'wmav2':'wmav2','wmav1':'wmav1'}
    target=name if selected=='source' else 'pcm_f32le' if selected=='pcm' else selected
    encoder=encoders.get(target,target if target.startswith('pcm_') else None)
    if not encoder or (selected=='source' and name=='aac' and profile!='LC'):
        raise ValueError(f'当前不支持保持 {name} / {profile} 编码类型处理，请选择 FLAC、PCM、AAC 或关闭淡化。')
    args=['-c:a',encoder,'-ar',str(rate),'-ac',str(report['channels'])]
    lossy=target not in ('flac','alac') and not target.startswith('pcm_')
    if lossy:
        if selected=='aac' and (name in ('flac','alac') or name.startswith('pcm_')):
            bitrate=128000*report['channels'];bitrate_basis='default_for_lossless_source'
        bitrate=max(8000,bitrate)
        if target=='aac':bitrate=min(bitrate,rate*6*report['channels'])
        args+=['-b:a',str(bitrate)]
    if target=='aac':args+=['-aac_pns','0']
    if target=='flac':
        args+=['-sample_fmt','s16' if sample_format and sample_format.startswith('s16') else 's32']
        if not sample_format or not sample_format.startswith('s16'):args+=['-bits_per_raw_sample','24']
    elif target=='alac' and sample_format:args+=['-sample_fmt','s16p' if sample_format.startswith('s16') else 's32p']
    return args,{'source_codec':name,'output_codec':target,'encoder':encoder,'sample_rate':rate,'layout':layout,
                 'target_bitrate':bitrate if lossy else None,'bitrate_basis':bitrate_basis if lossy else None,
                 'lossless':not lossy,'resampled':False}


def packet_summary(path):
    digest=hashlib.sha256();size=0;count=0
    with av.open(str(path)) as container:
        stream=container.streams.audio[0];signature=stream_signature(stream)
        for packet in container.demux(stream):
            if packet.size:digest.update(bytes(packet));size+=packet.size;count+=1
    return {'payload_sha256':digest.hexdigest(),'encoded_packets':count,
            'encoded_payload_bytes':size,'signature':signature}


def apply_fades(path, report, cfg, source, output_path=None):
    """Encode audio once, before speech review. No source or final file is edited."""
    if not audio_processing_enabled(cfg):return report
    path=Path(path);destination=Path(output_path) if output_path else path
    target=destination.with_name('encoded-'+destination.name)
    from .pauses import fade_seconds
    rate=report['sample_rate'];seconds=fade_seconds(cfg)
    ramps,joins=ramps_for(report['mapping'],rate,seconds) if cfg.get('join_fade_enabled',False) else ([],[])
    join_ramps=ramps[:];edges=[]
    if cfg.get('edge_fade_enabled',True):
        edge_seconds=min(cfg.get('edge_fade_seconds',.5),cfg.get('max_pause_seconds',1.5)*.45)
        edge_ramps,edges=edges_for(report['mapping'],rate,edge_seconds,report['duration'])
        ramps.extend(edge_ramps)
    if not ramps and cfg.get('audio_output_codec','source')=='source':return report
    ramps.sort(key=lambda row:row['start'])
    options,encoding=encoder_options(source,report,cfg.get('audio_output_codec','source'))
    command=[str(cfg['ffmpeg']),'-hide_banner','-v','error','-nostdin','-y',
             '-f','f32le','-ar',str(rate),'-ac',str(report['channels']),'-channel_layout',encoding['layout'],
             '-i','pipe:0','-map','0:a:0',*options,'-threads:a','2','-t',f"{report['duration']:.9f}"]
    if target.suffix.lower() in ('.m4a','.mp4','.mov'):command+=['-movflags','+faststart']
    if target.suffix.lower()=='.wav':command+=['-rf64','auto']
    command+=[str(target)]
    event('progress','处理音频输出与淡化（保持源采样率和声道）',89)
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
                            activity(f'音轨处理 {percent}% · {encoding["encoder"]}')
                            event('progress',f'音轨处理 {percent}%',89+percent*.07);last=percent
                    process.stdin.close()
                except BrokenPipeError:
                    # Read the actual encoder error below.
                    pass
                code=process.wait()
                errors.seek(0,os.SEEK_END);errors.seek(max(0,errors.tell()-3000))
                detail=errors.read().decode('utf-8',errors='replace').strip()
                if code or detail:raise RuntimeError('音轨处理失败：'+detail)
            finally:
                if process.poll() is None:process.kill();process.wait()
                if not process.stdin.closed:
                    try:process.stdin.close()
                    except BrokenPipeError:pass
        summary=packet_summary(target);signature=summary.pop('signature')
        if (signature['codec']!=encoding['output_codec'] or signature['sample_rate']!=rate
                or signature['channels']!=report['channels'] or not summary['encoded_packets']):
            raise AssertionError('导出音轨与所选编码、源采样率或声道配置不一致。')
        for i,join in enumerate(joins):
            levels=[10*np.log10(max(r['energy']/max(1,r['samples']),1e-12)) for r in join_ramps[i*2:i*2+2]]
            join.update(left_rms_db=round(float(levels[0]),2),right_rms_db=round(float(levels[1]),2),
                        level_difference_db=round(float(levels[1]-levels[0]),2))
        result={**report,**summary,'codec':signature['codec'],'audio_encoding':encoding,'payload_unchanged':False,'timeline_review':True,'bytes':target.stat().st_size,
                'average_bitrate':summary['encoded_payload_bytes']*8/report['duration'],
                'audio_fades':{**fade_status(cfg),'applied':bool(ramps),'seconds':seconds,'curve':'linear',
                               'requested_seconds':cfg.get('join_fade_seconds',.3),
                               'timeline_unchanged':True,'encoding':encoding,'joins':joins,'edges':edges}}
        result.pop('copied_packets',None)
        if 'streams' in result:result['streams']={**result['streams'],'audio':signature}
        os.replace(target,destination)
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
                ('payload_sha256','encoded_packets','encoded_payload_bytes','payload_unchanged','average_bitrate','audio_fades','codec','audio_encoding')}}
        result.pop('copied_packets',None)
        result['streams']={**original_report['streams'],'audio':packet_summary(target)['signature']}
        result['bytes']=target.stat().st_size
        result['reviewed_audio_payload_verified']=True
        os.replace(target,video)
        return result
    finally:
        if target.exists():target.unlink()
