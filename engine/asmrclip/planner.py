import math

import numpy as np
from scipy.ndimage import binary_closing, maximum_filter1d

from .common import complement, merge
from .exclusions import strong_voice, selected_exclusions, laugh_kind


def dense_conversations(episodes):
    dense = []
    for i, (a, _) in enumerate(episodes):
        group = []
        for c, d in episodes[i:]:
            if c-a > 120:
                break
            group.append([c,d])
        span = group[-1][1]-a
        spoken = sum(d-c for c,d in group)
        if (len(group)>=4 and span<=135) or (len(group)>=3 and spoken>=20) or (len(group)>=2 and spoken>=35 and spoken/max(span,1)>=.3):
            dense.append([a,group[-1][1]])
    return merge(dense,25)


def scene_guard(probes, edge, side):
    guard, reason = edge, []
    for p in probes:
        texture, breath, voice = p['texture'], p['breath'], p['speech']
        if strong_voice(p):
            guard = p['end'] if side == 'start' else p['start']
            reason.append('避开说话或带情绪的发声')
            continue
        if texture>.10 and texture>breath*1.8 and not (voice>.65 and texture<.18):
            reason.append('出现连续非语言声音')
            break
        if (breath>.045 and breath>texture*1.5) or (voice>.45 and texture<.075 and not laugh_kind(p)):
            guard = p['end'] if side == 'start' else p['start']
            reason.append('避开与话语相连的呼吸或发声尾音')
    return guard, reason


def make_plan(meta, frames, speech, music, cfg, classifier=None, exclusions=None):
    dt = meta['frame_samples']/meta['sample_rate']
    rms = frames['levels'][:,0]
    db = 20*np.log10(np.maximum(rms,1e-10))
    n, duration = len(db), len(db)*dt
    wide = 20*np.log10(np.maximum(maximum_filter1d(rms,size=9,mode='nearest'),1e-10))
    active_db = cfg['silence_db']+6
    review, rejected = [], []
    spoken = merge(speech['spoken']+(exclusions or {}).get('voice',[]),.65)
    excluded = selected_exclusions(exclusions or {},cfg)
    extraction=(exclusions or {}).get('extraction',{})
    if cfg['mode']=='extract':
        # Retention checkboxes are permissions, never evidence that vaping,
        # drinking or a sudden impact is itself an ASMR extraction target.
        excluded=merge(excluded+sum(((exclusions or {}).get(k,[]) for k in ('airflow','drinking','impacts','loud_laugh')),[]))
        excluded=merge(excluded+complement(extraction.get('intervals',[]),duration))

    def strict_cut(t, side):
        pos = int(np.clip(math.ceil(t/dt) if side=='start' else math.floor(t/dt),0,n-1))
        lo,hi = (pos,min(n-1,pos+round(6/dt))) if side=='start' else (max(0,pos-round(6/dt)),pos)
        candidates = np.arange(lo,hi+1)
        cost = np.maximum(wide[candidates],-62)+abs(candidates-pos)*dt*1.2
        return int(candidates[np.argmin(cost)])

    def event_cut(edge, side, bounds):
        if side=='start' and edge<=0:
            return 0,'file_edge'
        if side=='end' and edge>=duration:
            return n,'file_edge'
        pos = int(np.clip(math.ceil(edge/dt) if side=='start' else math.floor(edge/dt),0,n-1))
        available = max(1,(bounds[1]-bounds[0])//2)
        choices = []
        for reach in sorted(set([min(round(s/dt),available) for s in [18,36,72]]+[available])):
            lo = max(bounds[0],pos-reach) if side=='end' else max(bounds[0],pos)
            hi = min(bounds[1]-1,pos) if side=='end' else min(bounds[1]-1,pos+reach)
            if hi<=lo:
                return int(np.clip(lo,0,n-1)),'unresolved'
            local = db[lo:hi+1]
            threshold = float(np.clip(np.percentile(local,25),cfg['silence_db']-3,-40))
            spans = np.flatnonzero(np.diff(np.r_[False,local<threshold,False])).reshape(-1,2)
            for a,b in spans:
                if (b-a)*dt<.20:
                    continue
                a+=lo
                b+=lo
                c=a+min(round(.65/dt),(b-a)//2) if side=='end' else b-min(round(.75/dt),(b-a)//2)-1
                c=int(np.clip(c,lo,hi))
                choices.append((max(float(wide[c]),-60)+abs(c-pos)*dt*.85,c,'natural_pause'))
            if choices:
                break
        if choices:
            _,chosen,kind=min(choices)
            return chosen,kind
        candidates=np.arange(lo,hi+1)
        score=np.maximum(wide[candidates],-60)+abs(candidates-pos)*dt*.85
        return int(candidates[np.argmin(score)]),'local_gesture_trough'

    if cfg['mode']=='strict':
        episodes=merge([[s['start'],s['end']] for s in speech['accepted']]+spoken,1.5)
        dense=dense_conversations(episodes)
        removed=merge([[max(0,a-cfg['strict_pre']),min(duration,b+cfg['strict_post'])] for a,b in episodes+dense],cfg['strict_dense_gap'])
        blocked=[[strict_cut(a,'end'),strict_cut(b,'start')] for a,b in removed]
        blocked += [[strict_cut(a,'end'),strict_cut(b,'start')] for a,b in music]
        blocked += [[strict_cut(a,'end'),strict_cut(b,'start')] for a,b in excluded]
        units=[[a,b] for a,b in complement(blocked,n) if (b-a)*dt>=cfg['strict_min_section']]
        review=[{'start':a*dt,'end':b*dt,'reason':'严格模式：保留达到连续时长要求的片段'} for a,b in units]
    else:
        if classifier is None:
            raise ValueError('宽松 / 提取模式需要声学上下文分类器。')
        dense=[]
        free=complement(spoken+music+excluded,duration)
        sections=[]
        windows=[]
        for a,b in free:
            if b-a<2:
                continue
            offsets=[x for x in [0,2,4,7,11,15] if x+2<min(18,(b-a)/2)]
            count=len(offsets)
            sections.append((a,b,len(windows),count))
            windows += [(a+x,a+x+2) for x in offsets]
            windows += [(b-x-2,b-x) for x in offsets]
        probes=classifier.windows(windows)
        units=[]
        for a,b,index,count in sections:
            starts,ends=probes[index:index+count],probes[index+count:index+count*2]
            ga,ra=scene_guard(starts,a,'start') if a>0 else (a,[])
            gb,rb=scene_guard(ends,b,'end') if b<duration else (b,[])
            bounds=(math.ceil(a/dt),min(n,math.floor(b/dt)))
            start,ks=event_cut(ga,'start',bounds)
            end,ke=event_cut(gb,'end',bounds)
            good_start=ks in ('natural_pause','file_edge') or (ks=='local_gesture_trough' and wide[start]<-46)
            good_end=ke in ('natural_pause','file_edge') or (ke=='local_gesture_trough' and wide[min(end,n-1)]<-46)
            activity=db[start:end]>active_db if end>start else np.array([],bool)
            gestures=binary_closing(activity,structure=np.ones(7,bool)) if len(activity) else activity
            spans=np.flatnonzero(np.diff(np.r_[False,gestures,False])).reshape(-1,2)
            longest=max(((y-x)*dt for x,y in spans),default=0)
            texture=max((p['texture'] for p in starts+ends),default=0)
            complete=float(np.sum(activity))*dt>=2 and (longest>=.5 or texture>.08)
            record={'raw_start':a,'raw_end':b,'start':start*dt,'end':end*dt,'start_boundary':ks,'end_boundary':ke,
                    'start_adjustment':start*dt-a,'end_adjustment':b-end*dt,'context':ra+rb}
            if not (complete and good_start and good_end):
                record['reason']='缺少完整声音动作' if not complete else '继续外扩后仍未找到适合的边界'
                rejected.append(record)
                continue
            units.append([start,end])
            review.append(record)

    from .pauses import limit_keeps,quiet_spans
    quiet=quiet_spans(frames['levels'],cfg['silence_db'])
    keep=[];silence=[]
    for a,b in units:
        bounded,removed=limit_keeps([[a,b]],frames['levels'],dt,cfg,quiet)
        # A shortened pause does not turn one accepted scene into new short
        # scenes that should fail the strict mode's minimum-length rule.
        active=sum(int(np.sum(db[x:y]>active_db)) for x,y in bounded)
        length=sum(y-x for x,y in bounded)
        if active*dt>=2 and active/max(1,length)>=.25:
            keep.extend(bounded);silence.extend(removed)
    keep=merge(keep)
    keep,across_joins=limit_keeps(keep,frames['levels'],dt,cfg,quiet)
    silence=merge(silence+across_joins)
    for a,b in keep:
        if not 0<=a<b<=n:
            raise AssertionError('非法剪辑区间')
        if any(min(b*dt,d)>max(a*dt,c)+1e-7 for c,d in spoken+excluded):
            raise AssertionError('剪辑计划与已识别话语或排除声音重叠')
    return {'mode':cfg['mode'],'keep_frames':keep,'duration':sum(b-a for a,b in keep)*dt,
            'frame_seconds':dt,'spoken':spoken,'music':music,'dense_conversations':dense,
            'boundaries':review,'rejected':rejected,'silence_removed':silence,'acoustic_exclusions':exclusions or {},
            'max_pause_seconds':cfg.get('max_pause_seconds',1.5),
            'extraction':extraction if cfg['mode']=='extract' else {}}
