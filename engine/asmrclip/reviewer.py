"""Review decoded candidate exports with a separate full-size ASR model."""
import dataclasses
import hashlib
import json
from pathlib import Path

import av
import numpy as np

from .common import event, merge, ROOT, read_json, save_json
from .recognition import GENERIC, NONLEX
from .progress import scope, advance, activity


class SpeechRemaining(Exception):
    def __init__(self, report):
        super().__init__('候选成片仍有疑似话语，需要重新剪辑。')
        self.report=report


def validate_review_model(cfg):
    path=Path(cfg['review_model'])
    from .model_catalog import VOICE_MODELS
    component=VOICE_MODELS[cfg.get('review_model_id','whisper-large-v3')]['component']
    assets=[a for a in read_json(ROOT/'config/environment.json')['assets'] if a['component']==component]
    if not assets or any(not (path/Path(a['path']).name).is_file() or (path/Path(a['path']).name).stat().st_size!=a['size'] for a in assets):
        raise RuntimeError('缺少完整的成片复核模型，请在运行环境中下载所选模型。')
    return path


def review_cache_key(path,language,payload,duration,rate,channels):
    # Bump schema when inference parameters or speech acceptance rules change.
    from .model_catalog import model_signature
    signature=model_signature(path)
    if path.name=='qwen-asr':signature+=model_signature(path.parent/'qwen-aligner')
    identity=['full-review-4',str(path.resolve()),signature,language,payload,round(duration,6),rate,channels]
    return hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()


def review_speech(segment):
    import re
    if segment.get('backend')=='qwen3-asr':
        from .recognition import qwen_speech
        return qwen_speech(segment)
    text=segment.get('text','').strip()
    clean=re.sub(r'[\W_]+','',text)
    words=segment.get('words') or []
    score=sum(w.get('probability',0.) for w in words)/max(1,len(words))
    if not clean or NONLEX.fullmatch(clean) or segment.get('compression_ratio',0)>2.5:return False
    # Non-speech ASMR often hallucinates stock video outros. Keep evidence
    # strong enough to avoid treating every invented transcript as speech.
    if GENERIC.search(text) and score<.75:return False
    return (segment['end']>segment['start'] and segment.get('no_speech_prob',0)<.85
            and segment.get('avg_logprob',-9)>-1.4 and score>=.30)


def output_to_source(findings, keeps, dt):
    """Split words crossing a splice into their original analysis intervals."""
    result=[];cursor=0.
    for a,b in keeps:
        length=(b-a)*dt
        for item in findings:
            if item.get('review_only'):continue
            lo=max(cursor,item['start']);hi=min(cursor+length,item['end'])
            if hi>lo:result.append([a*dt+lo-cursor,a*dt+hi-cursor])
        cursor+=length
    return merge(result,.25)


def mapped_output_to_source(findings,mapping):
    result=[]
    for row in mapping:
        for item in findings:
            if item.get('review_only'):continue
            lo=max(row['output_start'],item['start']);hi=min(row['output_end'],item['end'])
            if hi>lo:
                result.append([row['analysis_start']+lo-row['output_start'],row['analysis_start']+hi-row['output_start']])
    return merge(result,.25)


def decode_review_audio(path,timeline=False):
    parts=[]
    cursor=0
    resampler=av.AudioResampler(format='s16',layout='mono',rate=16000)
    def append(frame):
        nonlocal cursor
        data=frame.to_ndarray().reshape(-1)
        start=round(float(frame.pts*frame.time_base)*16000) if timeline and frame.pts is not None else cursor
        if start>cursor:parts.append(np.zeros(start-cursor,np.int16));cursor=start
        if start<cursor:data=data[min(len(data),cursor-start):]
        if len(data):parts.append(data);cursor+=len(data)
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            if not timeline:frame.pts=None
            for mono in resampler.resample(frame):append(mono)
        for mono in resampler.resample(None):append(mono)
    return np.concatenate(parts) if parts else np.empty(0,np.int16)


def review_windows(length):
    # faster-whisper's batch collector requires nonoverlapping clips within
    # each call. Inspect boundaries in a separate group instead of submitting
    # overlapping clips that can make its VAD duration accounting negative.
    full=[{'start':t,'end':min(t+28,length)} for t in range(0,int(np.ceil(length)),28)]
    boundaries=[{'start':t-4,'end':min(t+4,length)} for t in range(28,int(np.ceil(length)),28)]
    return [full,boundaries]


class Reviewer:
    def __init__(self,cfg,language,cache=None):
        path=validate_review_model(cfg)
        self.cfg={**cfg,'speech_model':cfg.get('review_model_id','whisper-large-v3'),'whisper_model':str(path),'language':language}
        self.recognizer=None
        self.language=None if language=='auto' else language
        self.path=str(path.resolve())
        self.cache=cache

    def close(self):
        if self.recognizer:self.recognizer.close();self.recognizer=None

    def inspect_chunk(self, pcm, identity):
        """Reuse only an identical decoded chunk, with both coverage passes."""
        length=len(pcm)/16000
        groups=review_windows(length)
        windows_checked=sum(map(len,groups))
        # PCM includes splice gaps/decoder state, so a changed join cannot
        # reuse source-based evidence. Times below remain local to this chunk.
        digest=hashlib.sha256(pcm.tobytes()).hexdigest()
        key=hashlib.sha256(json.dumps([identity,digest,len(pcm),groups]).encode()).hexdigest()
        path=Path(self.cache)/'chunks'/(key+'.json') if self.cache else None
        if path and path.exists():
            try:cached=read_json(path)
            except (OSError,ValueError):cached={}
            if (cached.get('cache_key')==key and cached.get('windows_checked')==windows_checked
                    and isinstance(cached.get('findings'),list)):
                return cached['findings'],windows_checked,True
        if self.recognizer is None:
            from .recognition import Recognizer
            self.recognizer=Recognizer(self.cfg)
        audio=pcm.astype(np.float32)/32768
        findings=[]
        checked=0
        for index,clips in enumerate(groups):
            if not clips:continue
            with scope(('全段检查 1/2','边界检查 2/2')[index],checked/windows_checked,(checked+len(clips))/windows_checked):
                segments=self.recognizer.transcribe(audio,clips)
            checked+=len(clips)
            for s in segments:
                reason=''
                if not review_speech(s):
                    from .recognition import qwen_lexical,qwen_alignment_issue
                    if s.get('backend')!='qwen3-asr' or not qwen_lexical(s):continue
                    reason=qwen_alignment_issue(s)
                    if not reason:continue
                from .recognition import word_intervals
                spans=word_intervals([s]) if not reason and all('start' in w and 'end' in w for w in s.get('words',[])) else [[s['start'],s['end']]]
                for a,b in spans:
                    findings.append({'start':a,'end':b,'text':s['text'],
                        **({'review_only':True,'reason':reason} if reason else {}),
                        'avg_logprob':s.get('avg_logprob'),'word_probability':sum(w.get('probability',0) for w in s['words'])/max(1,len(s['words'])) if s.get('backend')!='qwen3-asr' else None})
        # Publish only after full coverage AND the boundary pass complete.
        if path:save_json(path,{'cache_key':key,'windows_checked':windows_checked,'findings':findings})
        return findings,windows_checked,False

    def inspect(self,path,export_report):
        activity('解码候选音轨，准备本轮复核')
        pcm=decode_review_audio(path,export_report.get('timeline_review',False))
        duration=len(pcm)/16000
        if not len(pcm):raise RuntimeError('候选成片解码为空，不能标记复核通过。')
        payload=export_report['payload_sha256']
        if export_report.get('timeline_review'):
            payload+=hashlib.sha256(json.dumps(export_report['mapping'],sort_keys=True).encode()).hexdigest()
        key=review_cache_key(Path(self.path),self.language,payload,duration,
                             export_report.get('sample_rate'),export_report.get('channels'))
        cached_path=Path(self.cache)/(key+'.json') if self.cache else None
        if cached_path and cached_path.exists():
            try:cached=read_json(cached_path)
            except (OSError,ValueError):cached={}
            if cached.get('cache_key')==key and isinstance(cached.get('review'),dict):
                result=cached['review'];result['cache_reused']=True
                event('log','复用同一音频帧内容与模型版本的完整成片复核。')
                advance(85 if self.cfg.get('speech_model')=='qwen3-asr' else 100,100,'完整复核缓存')
                return result
        findings=[];windows_checked=0;chunks_reused=0
        # Keep the existing model/rule signature and add the execution device.
        # Bump the chunk schema if transcription parameters or acceptance change.
        chunk_identity=review_cache_key(Path(self.path),self.language,'review-chunks-1',0,16000,1)+':'+self.cfg['device']
        # Full timeline coverage plus an extra pass at inference boundaries.
        # Adjacent 300-second batches overlap by two seconds as well.
        bases=range(0,len(pcm),300*16000)
        coverage=.85 if self.cfg.get('speech_model')=='qwen3-asr' else 1.
        for index,base in enumerate(bases):
            chunk=pcm[base:min(len(pcm),base+302*16000)]
            with scope(f'音频块 {index+1}/{len(bases)}',coverage*index/len(bases),coverage*(index+1)/len(bases)):
                rows,count,reused=self.inspect_chunk(chunk,chunk_identity)
                if reused:activity('复用已完成的全段与边界检查')
            for s in rows:
                a=max(0,s['start']+base/16000);b=min(duration,s['end']+base/16000)
                if b>a:findings.append({**s,'start':a,'end':b})
            windows_checked+=count
            chunks_reused+=int(reused)
            event('progress',f'成片大模型复核：{min(duration,base/16000+300)/60:.1f} / {duration/60:.1f} 分钟',95+3*min(1,(base/16000+300)/duration))
        unique={(round(s['start'],2),round(s['end'],2),s['text']):s for s in findings}
        findings=sorted(unique.values(),key=lambda s:s['start'])
        if chunks_reused:event('log',f'复用 {chunks_reused} 段音频内容完全一致的成片复核，包含边界检查。')
        from .model_catalog import VOICE_MODELS
        result={'status':'speech_found' if findings else 'passed','model':VOICE_MODELS[self.cfg['speech_model']]['name'],'model_path':self.path,
                'scope':'decoded_candidate_full_timeline','duration':duration,'windows_checked':windows_checked,
                'chunks_reused':chunks_reused,
                'candidate_payload_sha256':export_report['payload_sha256'],'findings':findings,
                'note':'检出达到阈值的疑似话语，需要重新剪辑复核。' if findings else '模型未检出达到话语阈值的残留，不等于人工听审或绝对无说话保证。'}
        if cached_path:save_json(cached_path,{'cache_key':key,'review':result})
        return result
