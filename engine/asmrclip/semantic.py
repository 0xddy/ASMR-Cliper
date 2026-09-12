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
 'break':[
  'The sound of a person taking a sip of water from a cup and swallowing.',
  'The sound of inhaling an electronic cigarette, a sizzling hiss, and exhaling vapor.',
  'The sound of someone laughing loudly.',
  'The sound of an object falling and crashing on the floor.'],
 'other':[
  'The sound of breathing, sighing and exhaling air.',
  'The sound of music playing in the background.',
  'The sound of quiet room noise, hum and silence.',
  'The sound of an electronic buzzing or static noise.']}


def semantic_scores(scores):
    result={};cursor=0
    for key,items in PROMPTS.items():
        result[key]=float(max(scores[cursor:cursor+len(items)]));cursor+=len(items)
    return result


def positive(record,seed=False,cfg=None):
    from .exclusions import strong_voice
    if record.get('quiet') or strong_voice(record):return False
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


def confirm(cfg,pcm,cache,classifier,speech,music,exclusions,duration):
    from .exclusions import selected_exclusions
    blocked=merge(speech['spoken']+music+exclusions.get('voice',[])+selected_exclusions(exclusions,cfg)+sum((exclusions.get(k,[]) for k in ('airflow','drinking','impacts','loud_laugh')),[]))
    windows=[]
    for a,b in complement(blocked,duration):
        for t in np.arange(a,b,5.):
            if b-t>=3:windows.append((float(t),float(min(t+10,b))))
    records=classifier.windows(windows)
    classifier.close()
    path=cache/'semantic-cache.json'
    identity=hashlib.sha256(json.dumps([model_signature(ROOT/'models/clap'),PROMPTS,'clap-1']).encode()).hexdigest()
    previous=read_json(path) if path.exists() else {}
    cached=previous.get('windows',{}) if previous.get('model')==identity else {}
    wanted=[(r,f'{r["start"]:.5f}:{r["end"]:.5f}') for r in records]
    pending=[(r,k) for r,k in wanted if not r.get('quiet') and k not in cached]
    client=None
    try:
        if pending:
            from .neural_client import NeuralClient
            client=NeuralClient('clap',cfg)
        for offset in range(0,len(pending),8):
            batch=pending[offset:offset+8];a=min(r['start'] for r,k in batch);b=max(r['end'] for r,k in batch)
            audio=np.asarray(pcm[round(a*16000):round(b*16000)],np.float32)/32768
            scores=client.request({'op':'classify','clips':[[r['start']-a,r['end']-a] for r,k in batch],
                'prompts':sum(PROMPTS.values(),[])},audio)
            for (r,k),score in zip(batch,scores):cached[k]=semantic_scores(score)
            save_json(path,{'model':identity,'windows':cached})
            event('progress',f'确认 ASMR 声音：{min(offset+8,len(pending))} / {len(pending)} 个窗口',66+3*min(1,(offset+8)/len(pending)))
    finally:
        if client:client.close()
    rows=[{**r,'semantic':cached.get(k,{})} for r,k in wanted]
    report=semantic_regions(rows,cfg);report['records']=rows
    save_json(cache/'extraction-evidence.json',report)
    return report
