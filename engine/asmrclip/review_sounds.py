"""Resolve ASR / sound-type conflicts on the actual candidate audio."""
import copy
import hashlib
from pathlib import Path
import numpy as np
from .common import event


def whisper_context(r):
    s=r.get('semantic',{})
    style=(r.get('whisper',0)>=.12 and r.get('whisper',0)>=r.get('voiced',0)*1.2) or (
        r.get('voiced',0)<.15 and s.get('whisper_asmr',0)>=.45 and s.get('whisper_asmr',0)-s.get('normal_speech',0)>=.15)
    return (not r.get('quiet') and style and max(r.get(k,0) for k in ('voiced','expressive','loud_laugh','impact'))<.35
            and s.get('whisper_asmr',0)>=.3 and s.get('whisper_asmr',0)-s.get('normal_speech',0)>=.04)


def whispered(r):
    s=r.get('semantic',{})
    return s.get('whisper_asmr',0)>=.3 and s.get('whisper_asmr',0)-max(s.get(k,0) for k in (
        'normal_speech','mouth','surface','tapping','heartbeat','break','other'))>=.025


def asmr_evidence(r):
    s=r.get('semantic',{});target=max(s.get(k,0) for k in ('mouth','surface','tapping','heartbeat'))
    return (target>=.35 and target-max(s.get(k,0) for k in ('speech','normal_speech','break','other'))>=.06)


def decision(context,probes,keep_whisper):
    if not probes or context.get('quiet'):return 'speech'
    if keep_whisper and whisper_context(context) and all(whispered(r) for r in probes):return 'whisper'
    if (max(context.get(k,0) for k in ('voiced','expressive','whisper'))<.2
            and asmr_evidence(context) and all(asmr_evidence(r) for r in probes)):
        return 'conflict'
    return 'speech'


def verify(path,review,cfg):
    # Raw ASR caching stays independent of retention choices and sound models.
    if review.get('model')!='Qwen3-ASR-1.7B' or not review.get('findings'):return review
    candidates=[r for r in review['findings'] if not r.get('review_only') and 0<r['end']-r['start']<=8]
    if not candidates:return review
    from .reviewer import decode_review_audio
    from .classifier import Classifier
    from .semantic import SoundMatcher
    from .progress import scope,advance
    result=copy.deepcopy(review)
    pcm=decode_review_audio(path,True);duration=len(pcm)/16000
    # Packet payloads exclude timestamps. Re-muxing the same packets can move
    # samples or insert timeline gaps, so bind window caches to decoded PCM.
    identity=hashlib.sha256(pcm).hexdigest()[:20]
    cache=Path(cfg.get('_task_cache',Path(cfg['cache_dir'])/'export-review'))/'review-results'/('sounds-pcm-1-'+identity)
    cache.mkdir(parents=True,exist_ok=True)
    contexts=[];probes=[];groups=[]
    for r in candidates:
        middle=(r['start']+r['end'])/2
        a=max(0,min(middle-5,duration-10));b=min(duration,a+max(10,r['end']-a))
        contexts.append((a,b))
        lo=max(0,r['start']-.3);hi=min(duration,r['end']+.3)
        starts=list(np.arange(lo,max(lo,hi-3),1.5))+[max(lo,hi-3)]
        groups.append((len(probes),len(probes)+len(starts)))
        probes.extend((float(t),float(min(duration,t+3))) for t in starts)
    classifier=Classifier(cfg,pcm,cache)
    with SoundMatcher(cfg,pcm,cache) as matcher:
        if not matcher.available():
            for r in result['findings']:
                r.update(review_only=True,reason='声音复核模型未就绪，无法据转写继续扩大删除')
            result['sound_verification']={'status':'unavailable'}
            return result
        try:
            with scope('检查疑似话语的声音上下文',.85,.9):records=classifier.windows(contexts)
            classifier.close()
            with scope('核对话语与 ASMR 声音证据',.9,1):
                scored=matcher.score(records,lambda n,total:advance(n,total,'声音上下文'))
                fine=matcher.score([{'start':a,'end':b} for a,b in probes],lambda n,total:advance(n,total,'话语片段'))
        finally:classifier.close()
    choices={};evidence=[]
    for r,c,(a,b) in zip(candidates,scored,groups):
        choice=decision(c,fine[a:b],cfg.get('keep_whisper',True))
        choices[(r['start'],r['end'],r['text'])]=choice
        evidence.append({'start':r['start'],'end':r['end'],'decision':choice,'context':c,'probes':fine[a:b]})
    remaining=[];allowed=list(result.get('allowed_whisper',[]))
    for r in result['findings']:
        choice=choices.get((r['start'],r['end'],r['text']),'speech')
        if choice=='whisper':allowed.append({**r,'reason':'成片声音复核确认轻语 / 耳语，已勾选保留'})
        else:
            if choice=='conflict':r.update(review_only=True,reason='转写与连续 ASMR 声音证据冲突，保留待复听')
            remaining.append(r)
    result.update(findings=remaining,allowed_whisper=allowed,status='speech_found' if remaining else 'passed',
                  sound_verification={'status':'checked','evidence':evidence})
    if not remaining:result['note']='检出词句经成片声音复核确认属于允许保留的轻语 / 耳语。'
    counts={k:sum(r['decision']==k for r in evidence) for k in ('whisper','conflict','speech')}
    event('log',f'成片声音复核：{counts["whisper"]} 处轻语保留，{counts["conflict"]} 处转写冲突仅标记待复听，{counts["speech"]} 处话语候选继续处理。')
    return result
