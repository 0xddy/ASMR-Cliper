"""Positive ASMR evidence from audio/text matching, with acoustic vetoes."""
import hashlib,json
from pathlib import Path
import numpy as np
from .common import ROOT,event,read_json,save_json,complement,merge
from .model_catalog import model_signature

PROMPTS={
 'mouth':[
  'The sound of ASMR ear licking with wet tongue and mouth sounds close to a microphone.',
  'The sound of ASMR lip smacking and gentle mouth clicking.',
  'The sound of soft wet squishing and repetitive mouth movements for ASMR.'],
 'surface':[
  'The sound of ASMR brushing, rubbing and scratching a microphone.',
  'The sound of close up soft rustling and crinkling for ASMR.'],
 'tapping':[
  'The sound of gentle repetitive tapping on an object for ASMR.',
  'The sound of fingernails rhythmically tapping on wooden or plastic objects close to a microphone.'],
 'heartbeat':[
  'The sound of a steady human heartbeat, a soft rhythmic lub dub heard close up.',
  'The sound of ASMR heartbeat sounds recorded with a microphone on the chest.'],
 'speech':[
  'The sound of a woman speaking and having a conversation.',
  'The sound of a woman whispering intelligible words into a microphone.',
  'The sound of emotional talking, shouting or exclaiming.'],
 'whisper_asmr':[
  'The sound of soft breathy ASMR whispering very close to the ears.',
  'The sound of a gentle quiet whispered voice speaking close to a microphone for ASMR.',
  'The sound of intimate soft spoken ASMR, hushed whispering with audible breath.'],
 'normal_speech':[
  'The sound of a woman talking in a normal conversational voice.',
  'The sound of a livestream host chatting aloud with viewers.',
  'The sound of emotional talking, shouting or calling out.'],
 'break':[
  'The sound of a person taking a sip of water from a cup and swallowing.',
  'The sound of inhaling an electronic cigarette, a sizzling hiss, and exhaling vapor.',
  'The sound of someone laughing loudly.',
  'The sound of an object falling and crashing on the floor.'],
 'other':[
  'The sound of breathing, sighing and exhaling air.',
  'The sound of music playing in the background.',
  'The sound of quiet room noise, hum and silence.',
  'The sound of an electronic buzzing or static noise.'],
 # Independent descriptions for drink-break verification. They do not turn
 # bottle handling, water or mouth sounds alone into exclusion evidence.
 'bottle_cap':[
  'The sound of unscrewing a plastic cap from a water bottle.',
  'The sound of opening a bottle, twisting and cracking the screw cap.',
  'The sound of screwing a lid back onto a plastic water bottle.'],
 'drinking':[
  'The sound of a person drinking water from a bottle and swallowing.',
  'The sound of taking a sip of water from a cup, gulping and swallowing.',
  'The sound of someone gulping a drink with wet liquid sounds.'],
 'swallow':[
  'The sound of swallowing water, a wet gulp in the throat.',
  'The sound of a person slurping and drinking water.'],
 'handling':[
  'The sound of rubbing and squeezing plastic objects.',
  'The sound of crinkling a plastic bag or wrapper.'],
 'water':[
  'The sound of water pouring, bubbling and splashing.',
  'The sound of gentle water movements for ASMR.']}


def semantic_scores(scores):
    result={};cursor=0
    for key,items in PROMPTS.items():
        result[key]=float(max(scores[cursor:cursor+len(items)]))
        if key=='other':
            # Only room noise/hum/silence and electronic static. Breathing and
            # music are separate activities, not proof of background residue.
            result['background']=float(max(scores[cursor+2:cursor+len(items)]))
        cursor+=len(items)
    return result


class SoundMatcher:
    """Share one lazy CLAP worker/cache between transition checks and V4."""
    def __init__(self,cfg,pcm,cache):
        self.cfg,self.pcm,self.path=cfg,pcm,cache/'semantic-cache.json'
        self.client=None;self.identity=None;self.cached={}

    def available(self):
        assets=[a for a in read_json(ROOT/'config/environment.json')['assets'] if a['component']=='clap']
        return (bool(assets) and (ROOT/'runtime/neural/python.exe').is_file()
                and all((ROOT/a['path']).is_file() and (ROOT/a['path']).stat().st_size==a['size'] for a in assets))

    def score(self,records,progress=None,required_keys=()):
        if not records:return []
        if self.identity is None:
            self.identity=hashlib.sha256(json.dumps([model_signature(ROOT/'models/clap'),PROMPTS,'clap-1']).encode()).hexdigest()
            previous=read_json(self.path) if self.path.exists() else {}
            self.cached=previous.get('windows',{}) if previous.get('model')==self.identity else {}
        wanted=[(r,f'{r["start"]:.5f}:{r["end"]:.5f}') for r in records]
        pending=list({k:r for r,k in wanted if k not in self.cached or any(name not in self.cached[k] for name in required_keys)}.items())
        if pending and self.client is None:
            from .neural_client import NeuralClient
            self.client=NeuralClient('clap',self.cfg)
        for offset in range(0,len(pending),8):
            batch=pending[offset:offset+8];a=min(r['start'] for k,r in batch);b=max(r['end'] for k,r in batch)
            # Pack sparse probes without materializing all the audio between
            # distant transitions. Retain the previous batch-relative rounding.
            parts=[];clips=[];cursor=0;origin=round(a*16000);limit=round(b*16000)
            for key,r in batch:
                lo=min(limit,origin+round((r['start']-a)*16000))
                hi=min(limit,origin+round((r['end']-a)*16000))
                part=np.asarray(self.pcm[lo:hi],np.float32)/32768
                parts.append(part);clips.append([cursor/16000,(cursor+len(part))/16000]);cursor+=len(part)
            audio=np.concatenate(parts)
            scores=self.client.request({'op':'classify','clips':clips,
                'prompts':sum(PROMPTS.values(),[])},audio)
            if len(scores)!=len(batch):raise RuntimeError('声音模型返回的窗口数量不完整。')
            for (key,r),score in zip(batch,scores):self.cached[key]=semantic_scores(score)
            save_json(self.path,{'model':self.identity,'windows':self.cached})
            if progress:progress(min(offset+8,len(pending)),len(pending))
        return [{**r,'semantic':self.cached[k]} for r,k in wanted]

    def close(self):
        if self.client:self.client.close();self.client=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def positive(record,seed=False,cfg=None):
    from .exclusions import strong_voice
    if record.get('quiet') or strong_voice(record):return False
    if record.get('retained_whisper') and (cfg or {}).get('keep_whisper',True):return True
    cfg=cfg or {};s=record.get('semantic',{})
    targets=['mouth','surface'];competitors=['speech','break','other']
    for category in ('heartbeat','tapping'):
        (targets if cfg.get('keep_'+category,True) else competitors).append(category)
    target=max(s.get(k,0) for k in targets)
    competitor=max(s.get(k,0) for k in competitors)
    # Cosine similarities and margins are matching evidence, not probabilities.
    threshold,margin=(.20,.025) if seed else (.17,.005)
    if target<threshold or target-competitor<margin:return False
    texture=record.get('texture',0)
    return texture>=.035 and max(record.get(k,0) for k in ('expressive','impact','loud_laugh'))<.30


def semantic_regions(records,cfg=None):
    rows=sorted(records,key=lambda r:r['start']);runs=[];run=[]
    for r in rows:
        supported=positive(r,cfg=cfg)
        if run and (not supported or r['start']>run[-1]['end']+1e-6):runs.append(run);run=[]
        if supported:run.append(r)
    if run:runs.append(run)
    evidence=[]
    for run in runs:
        seeds=sum(positive(r,True,cfg) for r in run)
        if len(run)<2 or seeds<2:continue
        start=run[0]['start']+min(2.5,(run[0]['end']-run[0]['start'])/4)
        end=run[-1]['end']-min(2.5,(run[-1]['end']-run[-1]['start'])/4)
        evidence.append({'start':start,'end':end,'windows':len(run),'strong_windows':seeds,
                         'basis':'CLAP ASMR descriptions + acoustic texture; continuous supporting evidence'})
    return {'intervals':merge([[r['start'],r['end']] for r in evidence]),'evidence':evidence,
            'policy':'ASMR audio/text matching with continuous supporting evidence and independent speech/break vetoes.',
            'windows_checked':len(rows),'positive_windows':sum(positive(r,cfg=cfg) for r in rows),'model':'CLAP HTSAT unfused'}


def confirm(cfg,pcm,cache,classifier,speech,music,exclusions,duration,matcher=None):
    from .exclusions import selected_exclusions
    blocked=merge(speech['spoken']+music+exclusions.get('voice',[])+selected_exclusions(exclusions,cfg)+sum((exclusions.get(k,[]) for k in ('airflow','drinking','impacts','loud_laugh')),[]))
    windows=[]
    for a,b in complement(blocked,duration):
        for t in np.arange(a,b,5.):
            if b-t>=3:windows.append((float(t),float(min(t+10,b))))
    records=classifier.windows(windows)
    classifier.close()
    owned=matcher is None
    matcher=matcher or SoundMatcher(cfg,pcm,cache)
    try:
        from .progress import advance
        scored=matcher.score([r for r in records if not r.get('quiet')],
            lambda done,total:advance(done,total,'ASMR 窗口'))
    finally:
        if owned:matcher.close()
    by_span={(r['start'],r['end']):r for r in scored}
    rows=[by_span.get((r['start'],r['end']),{**r,'semantic':{}}) for r in records]
    report=semantic_regions(rows,cfg);report['records']=rows
    from .whispering import allowed,subtract
    report['intervals']=merge(report['intervals']+subtract(allowed(cfg,exclusions),blocked))
    save_json(cache/'extraction-evidence.json',report)
    return report
