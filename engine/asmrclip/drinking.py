"""Verify drinking and neighboring bottle actions against deliberate ASMR."""
import numpy as np

from .common import complement, event, merge, save_json

SCHEMA=1
KEYS=('drinking','swallow','bottle_cap','handling','water')
ASMR=('mouth','surface','tapping','heartbeat')


def strongest(record,keys):
    scores=record.get('semantic',{})
    return max((scores.get(k,0.) for k in keys),default=0.)


def drink(record,strong=False):
    if record.get('quiet'):return False
    scores=record.get('semantic',{})
    rival=strongest(record,ASMR+('speech','other','handling','water'))
    # Similarities are not probabilities. Require both descriptions to beat
    # competing mouth/water actions, then require temporal corroboration.
    return (scores.get('drinking',0.)>=.30 and scores['drinking']-rival>=(.06 if strong else .015)
            and scores.get('swallow',0.)>=.28
            and scores['swallow']-strongest(record,ASMR)>=(.035 if strong else -.015))


def candidate(record):
    # Long windows can contain both a short sip and surrounding ASMR. This
    # permissive discovery gate NEVER deletes audio; 3 s verification follows.
    return (not record.get('quiet') and strongest(record,('drinking',))>=.30
            and strongest(record,('swallow',))>=.28
            and strongest(record,('drinking',))>=strongest(record,ASMR+('speech','other','handling','water'))-.08
            and strongest(record,('swallow',))>=strongest(record,ASMR)-.12)


def cap(record,attached=False):
    if record.get('quiet'):return False
    value=strongest(record,('bottle_cap',))
    rival=strongest(record,ASMR+('speech','other','handling','water'))
    if not attached:return value>=.28 and value-rival>=.025
    # Once drinking is confirmed, weak adjacent lid/handling evidence may
    # complete the gesture. This weaker rule can never initiate a deletion.
    return (value>=.32 and value>=strongest(record,('handling','water'))+.025
            and value>=strongest(record,ASMR+('speech',))-.12)


def asmr_return(record):
    target=strongest(record,ASMR)
    return not record.get('quiet') and target>=.28 and target-strongest(record,('drinking','bottle_cap','swallow'))>=.06


def probe_rows(pcm,regions,width,step):
    result=[]
    for a,b in merge(regions):
        # Sub-second tails contain too little context for this model.
        for start in np.arange(a,b,step):
            end=min(float(start+width),b)
            if end-start<2:continue
            part=np.asarray(pcm[round(start*16000):round(end*16000)],np.float32)
            quiet=not len(part) or float(np.mean(part*part))<(32768*10**(-62/20))**2
            result.append({'start':float(start),'end':end,'quiet':quiet})
    return result


def sequences(records):
    rows=sorted(records,key=lambda r:(r['start'],r['end']))
    strong=[i for i,r in enumerate(rows) if drink(r,True)]
    runs=[]
    for index in strong:
        previous=runs[-1][-1] if runs else None
        connected=previous is not None and rows[index]['start']<=rows[previous]['end']+1e-6
        if connected:
            connected=all(not asmr_return(r) and (drink(r) or cap(r,True) or r.get('quiet'))
                          for r in rows[previous+1:index])
        if connected:runs[-1].append(index)
        else:runs.append([index])
    events=[];decisions=[]
    for run in runs:
        first,last=run[0],run[-1];a,b=rows[first]['start'],rows[last]['end']
        # Repeated overlapping windows need different starts and >=4 seconds
        # of coverage. One tiny swallowing-like mouth click is insufficient.
        repeated=len(run)>=2 and b-a>=4 and rows[last]['start']-a>=1
        preceding=[];quiet_span=0.
        for i in range(first-1,-1,-1):
            r=rows[i]
            if a-r['start']>18 or r['end']<rows[i+1]['start'] or asmr_return(r):break
            if r.get('quiet'):
                quiet_span+=rows[i+1]['start']-r['start']
                if quiet_span>2:break
            elif drink(r) or cap(r,True) or cap(r):quiet_span=0.
            else:break
            if cap(r) and r['end']<=a+1:preceding.append(i)
        if not repeated and not preceding:
            decisions.append({'start':a,'end':b,'decision':'uncertain','reason':'只有单个饮水样窗口，缺少连续吞咽或先前开瓶证据'})
            continue
        lo=min([first]+preceding);hi=last
        # Follow only the attached gesture or a short pause. Stop at positive
        # ASMR evidence or unknown sound, without mandatory padding.
        for direction in (-1,1):
            index=(lo-1 if direction<0 else hi+1);quiet_span=0.
            while 0<=index<len(rows):
                r=rows[index];left,right=rows[lo]['start'],rows[hi]['end']
                if r['start']>right+1e-6 or r['end']<left-1e-6:break
                if r['start']<a-18 or r['end']>b+18 or asmr_return(r):break
                if r.get('quiet'):
                    quiet_span+=1
                    if quiet_span>2:break
                elif drink(r) or cap(r,True):quiet_span=0.
                else:break
                lo=min(lo,index);hi=max(hi,index);index+=direction
        start,end=rows[lo]['start'],rows[hi]['end']
        has_cap=any(cap(r,True) or cap(r) for r in rows[lo:hi+1])
        events.append([start,end])
        decisions.append({'start':start,'end':end,'decision':'remove','reason':
            ('连续饮水与吞咽证据' if repeated else '开瓶后出现饮水与吞咽证据')+
            ('；前后按声音证据包含疑似相连瓶盖动作' if has_cap else '；前后按声音证据确定边界'),
            'strong_windows':len(run),'cap_before':bool(preceding),'adjacent_cap':has_cap,
            'probes':rows[lo:hi+1]})
    return merge(events),decisions


def review_drinking(cfg,pcm,cache,matcher,speech,music,exclusions,duration):
    report={'version':SCHEMA,'status':'checked','candidates':[],'removed':[],
        'note':'先用整段声音语义寻找候选，再以短窗口检查饮水、吞咽与瓶盖顺序；单独瓶盖、普通水声或未知声音不删除。'}
    if cfg.get('keep_drinking',False) and cfg.get('mode')!='extract':
        report['status']='retained_by_setting';return [],report
    blocked=merge(speech['spoken']+music+exclusions.get('voice',[]))
    # Same 10 s / 5 s grid as V4, so the matcher cache can be shared.
    available=complement(blocked,duration)
    coarse=probe_rows(pcm,available,10,5)
    live=[r for r in coarse if not r['quiet']]
    report['coarse_windows']=len(live)
    if not live:return [],report
    if not matcher.available():
        raise RuntimeError('饮水休息复核需要 CLAP 声音模型及其依赖，请在运行环境补齐。')
    from .progress import advance,scope
    with scope('寻找饮水与开瓶候选',0,.55):
        scored=matcher.score(live,lambda done,total:advance(done,total,'声音窗口'),required_keys=KEYS)
    seeds=[r for r in scored if candidate(r) or cap(r)]
    # Do not associate a bottle cap and a sip across removed speech/music.
    regions=merge([[max(a,r['start']-18),min(b,r['end']+24)]
        for r in seeds for a,b in available if a<=r['start']<b])
    fine=probe_rows(pcm,regions,3,1)
    with scope('核对吞咽与前后动作',.55,1.):
        checked=matcher.score([r for r in fine if not r['quiet']],lambda done,total:advance(done,total,'饮水窗口'),required_keys=KEYS)
    by_span={(r['start'],r['end']):r for r in checked}
    fine=[by_span.get((r['start'],r['end']),r) for r in fine]
    intervals=[]
    for a,b in regions:
        found,decisions=sequences([r for r in fine if a<=r['start'] and r['end']<=b])
        intervals+=found;report['candidates']+=decisions
    report.update(removed=merge(intervals),fine_windows=len(fine),coarse_candidates=len(seeds))
    save_json(cache/'drinking-review.json',report)
    event('log',f'饮水休息复核：{len(seeds)} 个候选窗口，确认 {len(report["removed"])} 处饮水动作。')
    return report['removed'],report
