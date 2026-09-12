"""Independent exclusion evidence; ASMR texture must not override human speech."""
import re
from .common import merge

SPEECH = {'Speech','Male speech, man speaking','Female speech, woman speaking','Child speech, kid speaking',
          'Conversation','Narration, monologue','Babbling','Whispering'}
EXPRESSIVE = {'Shout','Bellow','Whoop','Yell','Battle cry','Children shouting','Screaming',
              'Crying, sobbing',
              'Whimper','Wail, moan','Groan','Grunt'}
SOFT_LAUGH = {'Giggle','Snicker','Chuckle, chortle'}
IMPACT = {'Slam','Coin (dropping)','Thump, thud','Bang','Smash, crash','Breaking','Clatter'}
KEEP_DEFAULTS = {'keep_soft_laugh':True,'keep_loud_laugh':False,'keep_vaping':False,
                 'keep_drinking':False,'keep_impacts':False,'keep_heartbeat':True,'keep_tapping':True}
RESPIRATORY = {'Breathing','Gasp','Sigh','Pant','Snort','Wheeze','Sneeze','Cough','Throat clearing'}
AIRFLOW = {'Spray','Steam','Hiss','Sizzle'}
MOUTH = {'Chewing, mastication','Biting','Crunch','Squish','Squelch'}
HEARTBEAT = {'Heart sounds, heartbeat'}
TAPPING = {'Tap','Knock'}
TEXTURE = MOUTH | HEARTBEAT | TAPPING | {'Crackle','Crushing','Stir','Rub','Scratch','Scrape','Rustle',
                   'Crumpling, crinkling','Whoosh, swoosh, swish','Water','Liquid','Stirring'}


def summarize(scores):
    def peak(labels): return max((scores.get(k,0.) for k in labels),default=0.)
    return {'speech':peak(SPEECH),'speech_base':scores.get('Speech',0.),'expressive':peak(EXPRESSIVE),
            'lexical':peak(SPEECH-{'Speech','Babbling'}),
            'soft_laugh':peak(SOFT_LAUGH),'loud_laugh':scores.get('Belly laugh',0.),
            'laughter':scores.get('Laughter',0.),'impact':peak(IMPACT),
            'heartbeat':peak(HEARTBEAT),'tapping':peak(TAPPING),
            'breath':peak(RESPIRATORY),'airflow':peak(AIRFLOW),'mouth':peak(MOUTH),'texture':peak(TEXTURE),
            'music':scores.get('Music',0.),'spray':scores.get('Spray',0.),'hiss':scores.get('Hiss',0.),
            'steam':scores.get('Steam',0.),'sizzle':scores.get('Sizzle',0.),
            'liquid':peak({'Water','Liquid','Pour','Fill (with liquid)','Gurgling'}),
            'container':peak({'Glass','Chink, clink'}),'gargle':scores.get('Gargling',0.)}


def strong_voice(record):
    # Independent sigmoid labels can simultaneously score speech and texture.
    # Positive texture is not evidence against an independently strong voice.
    # Generic Speech may co-activate with nonlexical laughter. Actual recognized
    # words and specific speech classes still have priority over keep options.
    nonlexical=laugh_kind(record) is not None
    return (record.get('lexical',0.) >= .62 or record.get('expressive',0.) >= .42
            or (record.get('speech',0.) >= .62 and not nonlexical))


def laugh_kind(record):
    soft=record.get('soft_laugh',0.)
    loud=record.get('loud_laugh',0.)
    generic=record.get('laughter',0.)
    if soft>=.22 and soft>loud*1.5:
        return 'soft_laugh'
    if loud>=.30 or generic>=.50:
        return 'loud_laugh'
    return None


def selected_exclusions(report, cfg):
    """Apply retention policy on every plan, including plans after ASR audit."""
    # Confirmed interruption residue is separate from intentional bass ASMR
    # and from optional sound categories such as heartbeat or exhalation.
    intervals=list(report.get('transitions',[]))
    for category,key in [('soft_laugh','keep_soft_laugh'),('loud_laugh','keep_loud_laugh'),
                         ('airflow','keep_vaping'),('drinking','keep_drinking'),('impacts','keep_impacts'),
                         ('heartbeat','keep_heartbeat'),('tapping','keep_tapping')]:
        if not cfg.get(key,KEEP_DEFAULTS[key]): intervals+=report.get(category,[])
    return merge(intervals)


def rhythmic_events(records,category):
    """Require repeated category evidence, not a single accidental knock."""
    threshold={'heartbeat':.20,'tapping':.25}[category]
    runs=[];run=[]
    for r in sorted(records,key=lambda r:r['start']):
        supported=not r.get('quiet') and r.get(category,0)>=threshold
        if category=='tapping':
            supported=supported and r.get('impact',0)<.35 and r.get('tapping',0)>=r.get('mouth',0)*.8
        if run and (not supported or r['start']>run[-1]['end']):runs.append(run);run=[]
        if supported:run.append(r)
    if run:runs.append(run)
    return merge([[run[0]['start'],run[-1]['end']] for run in runs if len(run)>=2])


def airflow_events(records):
    """Find repeated air-release + inhalation/exhalation cycles, not breath alone.

    AudioSet has no electronic-cigarette class. These are explicitly reported as
    suspected vaping/airflow events, rather than a semantic device identification.
    """
    records=sorted(records,key=lambda r:(r['start'],r['end']))
    seeds=[]
    for index,r in enumerate(records):
        nearby=[p for p in records[max(0,index-6):index+7]
                if p['start'] <= r['end']+6 and p['end'] >= r['start']-6]
        respiratory=max((p.get('breath',0.) for p in nearby),default=0.)
        air=r.get('airflow',0.)
        # A breath alone, mouth clicks, crackles, rubbing or an isolated hiss do
        # not qualify. Require an air-release class plus a respiratory context.
        if air < .16 or respiratory < .09 or air < r.get('mouth',0.)*1.7:
            continue
        if r.get('spray',0.) < .12 and r.get('steam',0.) < .10:
            if not (r.get('hiss',0.) >= .18 and r.get('sizzle',0.) >= .035):
                continue
        recurring=sum(p.get('airflow',0.) >= .10 for p in nearby) >= 2
        if not recurring:
            continue
        lo,hi=r['start'],r['end']
        # Follow the respiratory gesture outwards, stopping at established
        # mouth/surface activity or a gap. No mandatory before/after padding.
        for direction in (-1,1):
            for distance in range(1,7):
                i=index+direction*distance
                if not 0<=i<len(records): break
                p=records[i]
                if p['start']>hi+2 or p['end']<lo-2: break
                breath=p.get('breath',0.)
                texture=p.get('texture',0.)
                if texture>=.10 and texture>max(breath,p.get('airflow',0.))*1.5: break
                if breath<.055 or breath<texture*1.5: break
                lo=min(lo,p['start']);hi=max(hi,p['end'])
        seeds.append([lo,hi])
    return merge(seeds,2.)


def supported_utterance(segment, record):
    """Recover uncertain, emotional/short ASR candidates using acoustic evidence."""
    from .recognition import GENERIC
    if GENERIC.search(segment.get('text','')) or segment.get('compression_ratio',0)>2.5:
        return False
    # Recover emotional syllables, but do not turn recognizable laugh syllables
    # into mandatory speech removals when acoustic evidence says laughter.
    laugh_text=re.sub(r'[\s\W_]+','',segment.get('text','')).lower()
    if laugh_kind(record) and re.fullmatch(r'(?:ha|he|hi|ho|ah|哈|呵|嘻|嘿|하|허|히|호|흐|ㅎ|ㅋ)+',laugh_text):
        return False
    words=segment.get('words') or []
    probability=sum(w.get('probability',0.) for w in words)/max(1,len(words))
    return (segment['end']>segment['start'] and segment.get('avg_logprob',-9)>-1.85
            and probability>=.25 and (record.get('speech',0.)>=.24 or record.get('expressive',0.)>=.20))


DRINK_INTENT=re.compile(r'(?:喝|喝点|喝口|喝一口|喝点儿)\s*水|(?:饮水|喝茶)|'
    r'(?:물|차)(?:을|를)?\s*(?:좀\s*|한\s*(?:모금|잔)\s*)?(?:마시|마실|마셔)|'
    r'(?:水|お茶)(?:を)?(?:ちょっと|少し|一口|\s)*飲(?:み|む|も|ん)|(?:drink|sip)(?:ing)?\s+(?:some\s+|a\s+little\s+)?water',re.I)


def drinking_events(records, segments):
    # Water alone can be intentional ASMR. A drink break needs corroborating
    # cup/ingestion sounds or an explicit intention to drink plus liquid audio.
    intentions=[s['end'] for s in segments if DRINK_INTENT.search(s.get('text',''))]
    events=[]
    for index,r in enumerate(records):
        nearby=[p for p in records[max(0,index-6):index+7]
                if p['start']<=r['end']+6 and p['end']>=r['start']-6]
        container=max((p.get('container',0.) for p in nearby),default=0.)
        oral=max((max(p.get('gargle',0.),p.get('mouth',0.)) for p in nearby),default=0.)
        announced=any(-2<=r['start']-t<=25 for t in intentions)
        liquid=r.get('liquid',0.)
        ingestion=r.get('gargle',0.)
        confirmed=(container>=.13 and ((liquid>=.15 and oral>=.10) or ingestion>=.16))
        if confirmed or (announced and max(liquid,ingestion)>=.075):
            events.append([r['start'],r['end']])
    return merge(events,2.)


def extend_breaks(events, records):
    result=[]
    for a,b in events:
        lo,hi=a,b
        for side in ('left','right'):
            if side=='left': nearby=[r for r in records if a-24<=r['end']<=a+2]
            else: nearby=[r for r in records if b-2<=r['start']<=b+24]
            nearby.sort(key=lambda r:r['start'],reverse=side=='left')
            for r in nearby:
                if r['start']>hi+2 or r['end']<lo-2: break
                texture=r.get('texture',0.)
                competing=max(r.get(k,0.) for k in ('breath','airflow','liquid','gargle','expressive','laughter','soft_laugh','loud_laugh'))
                if texture>=.12 and texture>competing*1.5: break
                # Follow pauses and the rest activity; do not turn unrelated,
                # unclassified audio into a blanket break deletion.
                if not r.get('quiet',False) and competing<.055 and r.get('container',0.)<.10: break
                lo=min(lo,r['start']);hi=max(hi,r['end'])
        result.append([lo,hi])
    return merge(result)


def impact_events(records, pcm, sample_rate=16000):
    """Require a drop/crash class AND an isolated waveform burst.

    Neither volume alone nor ordinary tapping/clicking labels are exclusions.
    """
    import numpy as np
    events=[]
    for r in records:
        if r.get('impact',0.)<.25: continue
        a=max(0,r['start']-3); b=min(len(pcm)/sample_rate,r['end']+3)
        audio=np.asarray(pcm[round(a*sample_rate):round(b*sample_rate)],dtype=np.float32)/32768
        step=round(.05*sample_rate); count=len(audio)//step
        if count<10: continue
        blocks=audio[:count*step].reshape(count,step)
        rms=np.sqrt(np.mean(blocks*blocks,axis=1))
        baseline=max(float(np.quantile(rms,.65)),.0004)
        burst=rms>baseline*4
        spans=np.flatnonzero(np.diff(np.r_[False,burst,False])).reshape(-1,2)
        # Repeated deliberate percussion is not an isolated accident.
        if len(spans)>3: continue
        for lo,hi in spans:
            start=a+lo*.05; end=a+hi*.05
            if end<=r['start'] or start>=r['end'] or (hi-lo)*.05>.65: continue
            if np.max(np.abs(blocks[lo:hi]))<.035: continue
            events.append([max(0,start-.4),min(len(pcm)/sample_rate,end+.8)])
    return merge(events,.4)
