"""Explicit, evidence-bounded permission for soft ASMR whispers."""
import copy
import numpy as np

from .common import complement,merge,save_json,event


def subtract(intervals,removed):
    result=[];removed=merge(removed)
    for a,b in intervals:
        cursor=a
        for c,d in removed:
            if d<=cursor:continue
            if c>=b:break
            if c>cursor:result.append([cursor,min(c,b)])
            cursor=max(cursor,d)
            if cursor>=b:break
        if cursor<b:result.append([cursor,b])
    return merge(result)


def covers(a,b,intervals):
    return b>a and any(c<=a+1e-6 and d>=b-1e-6 for c,d in intervals)


def acoustic_candidate(r):
    return (not r.get('quiet') and r.get('whisper',0)>=.12
            and r.get('whisper',0)>=r.get('voiced',0)*.8
            and r.get('expressive',0)<.3)


def positive(r):
    s=r.get('semantic',{});whisper=r.get('whisper',0)
    rival=max(s.get(k,0) for k in ('normal_speech','mouth','surface','tapping','heartbeat','break','other'))
    return (not r.get('quiet') and whisper>=.25 and whisper>=r.get('voiced',0)*1.2
            and max(r.get(k,0) for k in ('expressive','impact','loud_laugh'))<.25
            and s.get('whisper_asmr',0)>=.23 and s['whisper_asmr']-rival>=.025)


def confirmed_regions(rows,regions):
    result=[];evidence=[]
    for left,right in regions:
        run=[]
        def finish():
            if len(run)<2:return
            # Only assign window centers. Do not exempt neighboring normal
            # words just because a wide crop also contains some whispering.
            a=run[0]['start']+(0 if abs(run[0]['start']-left)<1e-6 else 1.)
            b=run[-1]['end']-(0 if abs(run[-1]['end']-right)<1e-6 else 1.)
            if b-a<1:return
            result.append([a,b]);evidence.append({'start':a,'end':b,'windows':len(run),'probes':list(run)})
        for r in rows:
            if r['start']<left or r['end']>right:continue
            if not positive(r) or (run and r['start']>run[-1]['start']+1.01):
                finish();run=[]
            if positive(r):run.append(r)
        finish()
    return merge(result),evidence


def detect(cfg,pcm,cache,classifier,matcher,music,duration):
    report={'version':1,'status':'disabled','intervals':[],'evidence':[]}
    if not cfg.get('keep_whisper',True):return [],report
    if not matcher.available():raise RuntimeError('保留轻语 / 耳语需要 CLAP 声音模型，请先补齐运行环境。')
    from .progress import scope,advance
    available=complement(music,duration)
    with scope('寻找轻语 / 耳语',0,.35):
        coarse=classifier.windows([(float(t),min(float(t+10),b)) for a,b in available
                                   for t in np.arange(a,b,5) if b-t>=2])
    seeds=[r for r in coarse if acoustic_candidate(r)]
    regions=merge([[max(a,r['start']-3),min(b,r['end']+3)] for r in seeds
                   for a,b in available if a<=r['start']<b])
    with scope('确认轻语与普通说话的边界',.35,.6):
        fine=classifier.windows([(float(t),float(t+3)) for a,b in regions
                                 for t in np.arange(a,b-3+1e-6,1)])
    classifier.close()
    with scope('核对轻语声音证据',.6,1):
        rows=matcher.score([r for r in fine if acoustic_candidate(r)],
                           lambda done,total:advance(done,total,'轻语窗口'),required_keys=('whisper_asmr','normal_speech'))
    scored={(r['start'],r['end']):r for r in rows}
    checked=[scored.get((r['start'],r['end']),r) for r in fine]
    kept,evidence=confirmed_regions(checked,regions)
    report.update(status='checked',intervals=kept,evidence=evidence,coarse_windows=len(coarse),fine_windows=len(fine))
    save_json(cache/'whisper-review.json',report)
    event('log',f'轻语 / 耳语保留检查：确认 {len(kept)} 段；普通说话及未确认话语继续排除。')
    return kept,report


def allowed(cfg,exclusions):
    return exclusions.get('whisper',[]) if cfg.get('keep_whisper',True) else []


def output_intervals(plan,report,cfg):
    source=allowed(cfg,plan.get('acoustic_exclusions',{}));result=[]
    for row in report.get('mapping',[]):
        if 'analysis_start' not in row or 'analysis_end' not in row:continue
        for a,b in source:
            lo=max(a,row['analysis_start']);hi=min(b,row['analysis_end'])
            start=row['output_start']+lo-row['analysis_start']
            end=min(row['output_end'],row['output_start']+hi-row['analysis_start'])
            if hi>lo and end>start:result.append([start,end])
    return merge(result)


def apply_review(review,plan,report,cfg):
    """ASR detects words, not vocal style. Apply verified style permission after
    raw ASR caching, using the ACTUAL audio/video export mapping on every run.
    Never hide a model failure, normal words outside the mask, or a mixed span.
    """
    result=copy.deepcopy(review)
    if review.get('status') not in ('passed','speech_found'):return result
    intervals=output_intervals(plan,report,cfg)
    remaining=[];permitted=[]
    for row in review.get('findings',[]):
        pieces=subtract([[row['start'],row['end']]],intervals)
        if pieces!=[[row['start'],row['end']]]:
            accepted=subtract([[row['start'],row['end']]],pieces)
            permitted.extend({**row,'start':a,'end':b,'reason':'已确认且勾选保留的轻语 / 耳语'} for a,b in accepted)
        remaining.extend({**row,'start':a,'end':b} for a,b in pieces)
    result.update(findings=remaining,allowed_whisper=permitted,whisper_intervals=intervals,
                  status='speech_found' if remaining else 'passed')
    if permitted and not remaining:result['note']='检出词句均处于已确认并允许保留的轻语 / 耳语内。'
    return result
