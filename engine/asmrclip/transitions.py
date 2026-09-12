"""Confirm short spectral dropouts before excluding an interrupted gesture.

Spectral changes propose locations only. Low-frequency ASMR and uncertain
content are protected; neither quietness nor absent speech proves a break.
"""
import json
import os
from pathlib import Path

import numpy as np

from .common import event, merge, save_json
from .exclusions import selected_exclusions, strong_voice
from .semantic import positive

STEP=.25
RATE=16000
SCHEMA=1


def spectral_trace(pcm):
    """Gain-independent spectral shape on 250 ms blocks, bounded working RAM."""
    size=round(RATE*STEP);count=len(pcm)//size
    result=np.empty((count,3),np.float32)
    frequency=np.fft.rfftfreq(4096,1/RATE)
    audible=(frequency>=80)&(frequency<=7600)
    high=(frequency>=2000)&(frequency<=7600)
    window=np.hanning(size)
    for begin in range(0,count,128):
        end=min(count,begin+128)
        blocks=np.asarray(pcm[begin*size:end*size],np.float64).reshape(-1,size)/32768
        blocks=blocks-blocks.mean(axis=1,keepdims=True)
        rms=np.sqrt(np.mean(blocks*blocks,axis=1))
        power=np.abs(np.fft.rfft(blocks*window,n=4096,axis=1))**2
        total=power[:,audible].sum(axis=1)
        centroid=(power[:,audible]*frequency[audible]).sum(axis=1)/np.maximum(total,1e-20)
        fraction=power[:,high].sum(axis=1)/np.maximum(total,1e-20)
        result[begin:end]=np.c_[20*np.log10(np.maximum(rms,1e-10)),
                                10*np.log10(np.maximum(fraction,1e-10)),centroid]
    return result


def cached_trace(pcm,cache):
    wav=cache/'analysis.wav';path=cache/'spectral-transitions.npz'
    stat=wav.stat() if wav.exists() else None
    identity=json.dumps([SCHEMA,RATE,STEP,len(pcm),stat.st_size if stat else None,stat.st_mtime_ns if stat else None])
    if stat and path.exists():
        try:
            with np.load(path,allow_pickle=False) as saved:
                trace=saved['trace']
                if (str(saved['identity'])==identity and trace.shape==(len(pcm)//round(RATE*STEP),3)
                        and np.isfinite(trace).all()):return trace
        except (OSError,ValueError,KeyError):pass
    trace=spectral_trace(pcm)
    if stat:
        temp=path.with_suffix('.part.npz')
        with temp.open('wb') as f:np.savez(f,identity=identity,trace=trace)
        os.replace(temp,path)
    return trace


def candidates(trace):
    """Find abrupt relative dulling with recovery; never delete from this alone."""
    result=[];i=8
    while i+8<len(trace):
        before=np.median(trace[i-8:i],axis=0)
        onset=trace[i:i+2]
        # A gain change preserves both spectral fraction and centroid. Avoid
        # noise-floor estimates and recordings that were already bass-heavy.
        if not (before[0]>-60 and before[2]>=900 and np.all(onset[:,0]>-68)
                and np.all(onset[:,1]<=before[1]-7) and np.all(onset[:,2]<=before[2]*.60)):
            i+=1;continue
        limit=min(len(trace)-8,i+round(20/STEP)+2)
        end=i+2
        while end<limit:
            block=np.median(trace[end:end+2],axis=0)
            if block[1]>before[1]-4 and block[2]>before[2]*.72:break
            end+=1
        if end==limit or end-i<5 or end-i>round(20/STEP):
            i+=1;continue
        middle=trace[i:end]
        dull=(middle[:,1]<=before[1]-6)&(middle[:,2]<=before[2]*.65)
        if float(dull.mean())<.8:
            i+=1;continue
        result.append({'start':i*STEP,'end':end*STEP,
            'high_band_drop_db':float(before[1]-np.median(middle[:,1])),
            'centroid_before_hz':float(before[2]),'centroid_inside_hz':float(np.median(middle[:,2]))})
        i=end
    return result


def probe_windows(candidate,duration):
    a,b=candidate['start'],candidate['end']
    before=(max(0,a-4),a);after=(b,min(duration,b+4))
    if min(before[1]-before[0],after[1]-after[0])<2:return []
    # Every probe is wholly inside the suspect phase; a wide window straddling
    # the next ASMR gesture must not mislabel an idle interval as ASMR.
    if b-a<=2:body=[(a,b)]
    else:
        body=[(float(t),float(t+2)) for t in np.arange(a,b-2+1e-6,1)]
        if body[-1][1]<b-1e-6:body.append((b-2,b))
    return [before,after]+body


def non_asmr_residue(record):
    s=record.get('semantic',{})
    target=max((s.get(k,0.) for k in ('mouth','surface','heartbeat','tapping')),default=0.)
    # Require affirmative room-noise/hum/static evidence, not low ASMR scores
    # alone. Preserve meaningful low-frequency gestures and gentle laughter.
    texture=max(record.get(k,0.) for k in ('texture','mouth','heartbeat','tapping','soft_laugh'))
    background=s.get('background',0.)
    return (background>=.23 and background>=s.get('other',0.)-1e-6
            and background-max(target,s.get('speech',0.),s.get('break',0.))>=.055
            and target<.23 and texture<.12)


def decision(records,cfg):
    if len(records)<3:return 'uncertain','缺少完整的前后声音上下文'
    before,after,*body=records
    if any(positive(r,cfg=cfg) or max(r.get(k,0.) for k in ('mouth','heartbeat','tapping','soft_laugh'))>=.2 for r in body):
        return 'keep_asmr','低频阶段仍有 ASMR 动作或允许保留声音的证据'
    if not positive(before,cfg=cfg) or not positive(after,cfg=cfg):
        return 'uncertain','尚未确认前后为稳定 ASMR 动作'
    if all(non_asmr_residue(r) for r in body):
        return 'remove','频谱突降后持续背景残留，内部窗口均无有效 ASMR，随后恢复 ASMR'
    return 'uncertain','内部声音证据不足或存在混合动作，不据此删除，记录候选'


def acoustic_veto(records):
    """Skip semantic inference when the acoustic evidence already vetoes removal."""
    before,after,*body=records
    if any(max(r.get(k,0.) for k in ('mouth','heartbeat','tapping','soft_laugh'))>=.2 for r in body):
        return 'keep_asmr','基础声音分类仍检出明确动作，保留过渡候选'
    if any(max(r.get(k,0.) for k in ('texture','mouth','heartbeat','tapping','soft_laugh'))>=.12 for r in body):
        return 'uncertain','内部仍有声音动作证据，不作为背景残留删除'
    if any(r.get('quiet') or strong_voice(r) or r.get('texture',0.)<.035
           or max(r.get(k,0.) for k in ('expressive','impact','loud_laugh'))>=.3 for r in (before,after)):
        return 'uncertain','前后缺少稳定 ASMR 的基础声学证据'
    return None


def review_transitions(cfg,pcm,cache,classifier,matcher,speech,music,exclusions,duration):
    found=candidates(cached_trace(pcm,cache))
    blocked=merge(speech['spoken']+music+exclusions.get('voice',[])+selected_exclusions(exclusions,cfg))
    relevant=[r for r in found if sum(max(0,min(r['end'],b)-max(r['start'],a)) for a,b in blocked)<r['end']-r['start']-1e-6]
    report={'version':SCHEMA,'status':'checked','candidates':[],
            'note':'相对音色变化只是候选。只有前后 ASMR 与内部背景残留证据共同成立时删除，未知内容不自动删除。'}
    rows=[];slices=[]
    for r in relevant:
        probes=probe_windows(r,duration)
        if probes:slices.append((r,len(rows),len(probes)));rows+=probes
    if rows and not matcher.available():
        report['status']='needs_model'
        report['candidates']=[{**r,'decision':'uncertain','reason':'缺少 CLAP 声音模型或其运行环境'} for r,_,_ in slices]
        event('log','检出音色突降候选；请在运行环境安装 ASMR 声音识别及其依赖后进行确认。此次保留这些未确认过渡。')
    elif rows:
        event('progress',f'复核 {len(slices)} 处音色突降过渡',65.2)
        acoustic=classifier.windows(rows)
        pending=[];pending_slices=[]
        for r,index,count in slices:
            probes=acoustic[index:index+count]
            veto=acoustic_veto(probes)
            if veto:
                choice,reason=veto
                report['candidates'].append({**r,'decision':choice,'reason':reason,'probes':probes})
            else:
                pending_slices.append((r,len(pending),count));pending+=probes
        if pending:classifier.close()
        from .progress import advance
        scored=matcher.score(pending,
            lambda done,total:advance(done,total,'过渡窗口'),required_keys=('background',))
        report['semantic_candidates']=len(pending_slices)
        for r,index,count in pending_slices:
            probes=scored[index:index+count]
            choice,reason=decision(probes,cfg)
            report['candidates'].append({**r,'decision':choice,'reason':reason,'probes':probes})
    report['candidates'].sort(key=lambda r:(r['start'],r['end']))
    intervals=merge([[r['start'],r['end']] for r in report['candidates'] if r['decision']=='remove'])
    report['removed']=intervals
    save_json(cache/'transition-review.json',report)
    return intervals,report
