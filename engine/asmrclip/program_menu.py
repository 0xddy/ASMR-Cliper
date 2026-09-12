"""Describe the finished audio in playback order; never plan or edit media."""
import csv
import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path

import av
import numpy as np

from .common import ROOT, event, fingerprint, read_json, save_json, settings
from .model_catalog import model_signature
from .progress import advance, phase, tracked

RATE = 16000
BLOCK = 300 * RATE
STEP = 5.
LABELS = {
    'wet_mouth':'舔耳 / 湿润口腔音', 'mouth_clicks':'唇音 / 口腔轻响',
    'brushing':'刷麦 / 轻刷', 'rubbing':'摩擦 / 抓挠', 'tapping':'道具敲击',
    'crinkling':'揉纸 / 细碎沙沙声', 'heartbeat':'心跳', 'airflow':'呼气 / 气流',
    'water':'水声 / 饮水', 'laughter':'笑声', 'speech':'疑似说话',
    'music':'背景音乐', 'background':'安静 / 背景声', 'unknown':'待确认', 'mixed':'混合 ASMR'}
# These are candidate sound descriptions, not a requested sequence of activities.
# Separate prompts/cache from V4: menu naming must never alter clipping evidence.
PROMPTS = {
    'wet_mouth':[
        'The sound of wet tongue licking an ear shaped microphone for ASMR.',
        'Close up wet mouth sounds, tongue movements and gentle saliva sounds.'],
    'mouth_clicks':[
        'The sound of dry mouth clicking, lip popping and lip smacking for ASMR.',
        'The sound of gentle tongue clicks close to a microphone.'],
    'brushing':[
        'The sound of a soft brush brushing a microphone for ASMR.',
        'The sound of soft bristles sweeping gently over a surface.'],
    'rubbing':[
        'The sound of rubbing and scratching a microphone with fingers.',
        'Close up gentle friction, scratching and rubbing on a textured surface.'],
    'tapping':[
        'The sound of fingernails rhythmically tapping on wooden or plastic objects for ASMR.',
        'The sound of gentle repeated taps on a small object close to a microphone.'],
    'crinkling':[
        'The sound of paper crinkling and rustling softly near a microphone.',
        'The sound of a plastic wrapper crackling and crumpling for ASMR.'],
    'heartbeat':[
        'The sound of a steady human heartbeat recorded close to the chest.',
        'Soft rhythmic heartbeats making a repeated lub dub sound.'],
    'airflow':[
        'The sound of gentle breathing and blowing air into a microphone.',
        'The sound of exhaling vapor, a soft hiss and a long breath.'],
    'water':[
        'The sound of pouring and splashing water.',
        'The sound of drinking from a cup and swallowing water.'],
    'laughter':['The sound of a person giggling and laughing.'],
    'speech':[
        'The sound of a woman speaking intelligible words and having a conversation.',
        'The sound of a woman whispering intelligible words.'],
    'music':['The sound of music playing.'],
    'background':[
        'The sound of quiet room noise, hum and silence.',
        'The sound of electronic buzzing, static noise or a steady electronic tone.']}
ASMR = frozenset(('wet_mouth','mouth_clicks','brushing','rubbing','tapping','crinkling','heartbeat'))


def available():
    assets=[a for a in read_json(ROOT/'config/environment.json')['assets'] if a['component']=='clap']
    return bool(assets) and (ROOT/'runtime/neural/python.exe').is_file() and all(
        (ROOT/a['path']).is_file() and (ROOT/a['path']).stat().st_size==a['size'] for a in assets)


def sound_scores(values):
    if len(values)!=sum(map(len,PROMPTS.values())) or not all(math.isfinite(v) for v in values):
        raise RuntimeError('节目单声音模型返回的数据不完整。')
    result={};cursor=0
    for key,items in PROMPTS.items():
        result[key]=float(max(values[cursor:cursor+len(items)]));cursor+=len(items)
    return result


def identify(scores):
    ranked=sorted(scores,key=scores.get,reverse=True)
    if not ranked:return 'unknown',[]
    first=ranked[0];score=scores[first];runner=scores[ranked[1]] if len(ranked)>1 else 0.
    # Similarities are not calibrated probabilities. Close competing ASMR
    # descriptions get a broad label; weak/contradictory evidence stays unknown.
    if score<.20:return 'unknown',[]
    close=[k for k in ranked if score-scores[k]<.025]
    if len(close)>1:
        if set(close)<=ASMR:return 'mixed',close[:3]
        return 'unknown',[]
    return (first,[]) if score-runner>=.025 else ('unknown',[])


def decoded_blocks(path,duration,timeline=False):
    """Stream only the final audio track with its playback clock, bounded RAM."""
    limit=round(duration*RATE);cursor=0;written=0;parts=[];count=0
    resampler=av.AudioResampler(format='s16',layout='mono',rate=RATE)
    def add(data):
        nonlocal count,written,parts
        pos=0
        while pos<len(data):
            take=min(BLOCK-count,len(data)-pos);parts.append(data[pos:pos+take]);pos+=take;count+=take
            if count==BLOCK:
                yield written,np.concatenate(parts)
                written+=count;parts=[];count=0
    def append(frame):
        nonlocal cursor
        data=frame.to_ndarray().reshape(-1)
        start=round(float(frame.pts*frame.time_base)*RATE) if timeline and frame.pts is not None else cursor
        while cursor<min(start,limit):
            gap=min(BLOCK,min(start,limit)-cursor)
            yield from add(np.zeros(gap,np.int16));cursor+=gap
        if start<cursor:data=data[min(len(data),cursor-start):]
        data=data[:max(0,limit-cursor)]
        yield from add(data);cursor+=len(data)
    with av.open(str(path)) as container:
        # Demux/decode audio only, including for video inputs.
        for frame in container.decode(audio=0):
            if not timeline:frame.pts=None
            for mono in resampler.resample(frame):yield from append(mono)
            if cursor>=limit:break
        for mono in resampler.resample(None):yield from append(mono)
    # Preserve small codec/video boundary gaps, but do not invent a long silent
    # tail if the selected media no longer matches its saved export report.
    if limit-cursor>RATE:raise RuntimeError('成片音轨长度与校验报告不一致，未生成节目单。')
    while cursor<limit:
        gap=min(BLOCK,limit-cursor);yield from add(np.zeros(gap,np.int16));cursor+=gap
    if count:yield written,np.concatenate(parts)


def windows(start,end,mapping):
    cuts={start,end}
    for row in mapping:
        for key in ('output_start','output_end'):
            value=float(row[key])
            if start<value<end:cuts.add(value)
    boundaries=sorted(cuts);rows=[]
    for a,b in zip(boundaries,boundaries[1:]):
        t=a
        while t<b-1e-8:
            finish=min(b,t+STEP);center=(t+finish)/2
            lo=max(a,min(center-5,b-10));hi=min(b,lo+10)
            rows.append({'start':t,'end':finish,'probe_start':lo,'probe_end':hi})
            t=finish
    return rows


class MenuMatcher:
    def __init__(self,cfg,cache):
        self.cfg=cfg;self.client=None;self.path=cache/'menu-windows.json'
        identity=[model_signature(ROOT/'models/clap'),PROMPTS,cfg.get('device','auto'),'finished-menu-1']
        self.identity=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        previous=read_json(self.path) if self.path.exists() else {}
        self.cached=previous.get('windows',{}) if previous.get('model')==self.identity else {}

    def score(self,pcm,offset,rows):
        from .neural_client import NeuralClient
        results=[]
        for base in range(0,len(rows),8):
            batch=rows[base:base+8];wanted=[];pending={}
            for row in batch:
                lo=max(0,round(row['probe_start']*RATE)-offset);hi=min(len(pcm),round(row['probe_end']*RATE)-offset)
                part=pcm[lo:hi];key=hashlib.sha256(part.tobytes()).hexdigest()
                wanted.append((row,key))
                if key not in self.cached:
                    if len(part)<1600:self.cached[key]={}
                    elif np.sqrt(np.mean(np.square(part.astype(np.float32)/32768)))<10**(-60/20):
                        self.cached[key]={'background':1.}
                    else:pending[key]=part
            if pending:
                if self.client is None:self.client=NeuralClient('clap',self.cfg)
                parts=[];clips=[];cursor=0
                for part in pending.values():
                    parts.append(part.astype(np.float32)/32768);clips.append([cursor/RATE,(cursor+len(part))/RATE]);cursor+=len(part)
                values=self.client.request({'op':'classify','clips':clips,'prompts':sum(PROMPTS.values(),[])},np.concatenate(parts))
                if len(values)!=len(pending):raise RuntimeError('节目单模型未返回全部窗口。')
                for key,value in zip(pending,values):self.cached[key]=sound_scores(value)
            for row,key in wanted:
                scores=self.cached[key];category,alternatives=identify(scores)
                results.append({**row,'category':category,'alternatives':alternatives,'scores':scores})
            if pending and (base%40==0 or base+8>=len(rows)):save_json(self.path,{'model':self.identity,'windows':self.cached})
            yield results[-len(batch):]

    def close(self):
        if self.client:self.client.close();self.client=None


def chapters(rows,findings=()):
    result=[]
    for row in rows:
        category=row['category'];alternatives=sorted(row.get('alternatives',[]))
        same=result and result[-1]['category']==category and result[-1]['alternatives']==alternatives and abs(result[-1]['end']-row['start'])<1e-5
        if same:result[-1]['end']=row['end'];result[-1]['windows']+=1
        else:result.append({'start':row['start'],'end':row['end'],'category':category,'title':LABELS[category],
                            'alternatives':alternatives,'windows':1})
    for index,row in enumerate(result,1):
        row['index']=index
        row['review_findings']=[f for f in findings if f['start']<row['end'] and f['end']>row['start']]
        row['duration']=row['end']-row['start']
        row['needs_confirmation']=row['category'] in ('unknown','mixed')
    return result


def clock(seconds):
    seconds=max(0,int(seconds));h,seconds=divmod(seconds,3600);m,s=divmod(seconds,60)
    return f'{h:02}:{m:02}:{s:02}'


def menu_text(menu):
    lines=['ASMR 成片节目单','时间对应最终成片；项目由本地 CLAP 声音语义模型识别。','']
    for row in menu.get('chapters',[]):
        title=row['title']
        if row.get('alternatives'):title+='（'+'、'.join(LABELS[k] for k in row['alternatives'])+'）'
        if row.get('review_findings'):title+=' · 含待复听位置'
        lines.append(f'{clock(row["start"])} — {clock(row["end"])}  {title}')
    lines+=['','相似声音可能混淆，项目切换时间约为 5 秒精度；待确认表示证据不足。']
    return '\n'.join(lines)+'\n'


def write_menu(folder,menu):
    # Sidecars only. No remuxing, chapters injection, or media metadata writes.
    folder=Path(folder)
    save_json(folder/'节目单.json',menu)
    temp=folder/'节目单.txt.tmp';temp.write_text(menu_text(menu),encoding='utf-8-sig');temp.replace(folder/'节目单.txt')
    temp=folder/'节目单.csv.tmp'
    with temp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['序号','成片开始','成片结束','ASMR 项目','待确认','待复听位置数'])
        for row in menu['chapters']:
            writer.writerow([row['index'],clock(row['start']),clock(row['end']),row['title'],'是' if row['needs_confirmation'] else '否',len(row['review_findings'])])
    temp.replace(folder/'节目单.csv')


def generate(path,report,cfg):
    started=time.monotonic();path=Path(path);identity=fingerprint(path)
    if not available():return {'status':'unavailable','chapters':[],'note':'请在运行环境安装 ASMR 声音识别 · CLAP 及其依赖后补生成节目单。'}
    cache=Path(cfg['cache_dir'])/'program-menus'/identity;cache.mkdir(parents=True,exist_ok=True)
    matcher=MenuMatcher(cfg,cache)
    duration=float(report['duration']);rows=[]
    total_samples=round(duration*RATE)
    plans=[windows(offset/RATE,min(offset+BLOCK,total_samples)/RATE,report.get('mapping',[]))
           for offset in range(0,total_samples,BLOCK)]
    total_windows=sum(map(len,plans))
    try:
        for index,(offset,pcm) in enumerate(decoded_blocks(path,duration,report.get('timeline_review',False))):
            planned=plans[index]
            for batch in matcher.score(pcm,offset,planned):
                rows.extend(batch);advance(len(rows),total_windows,'声音窗口')
    finally:matcher.close()
    if fingerprint(path)!=identity:raise RuntimeError('节目单识别期间成片发生变化，请重新生成。')
    menu={'version':1,'status':'ready','model':'CLAP HTSAT unfused','timeline':'final_output',
          'source_fingerprint':identity,'duration':duration,'windows_checked':len(rows),
          'created_at':datetime.now().isoformat(timespec='seconds'),'elapsed_seconds':round(time.monotonic()-started,3),
          'chapters':chapters(rows,report.get('speech_review',{}).get('findings',[]))}
    save_json(cache/'evidence.json',{'model':matcher.identity,'windows':rows})
    write_menu(path.parent,menu)
    return menu


def attach(path,report,cfg):
    """Menu failure must not turn an already validated export into a failure."""
    try:menu=generate(path,report,cfg)
    except Exception as exc:
        menu={'status':'failed','chapters':[],'note':str(exc)}
        event('log','成片已保存，节目单暂未生成：'+str(exc))
    report['program_menu']=menu
    if menu['status']=='unavailable':event('log',menu['note'])
    try:save_json(Path(path).parent/'校验报告.json',report)
    except OSError as exc:event('log','成片已保存，节目单状态未能写入校验报告：'+str(exc))
    return menu


@tracked
def run(data):
    cfg=settings(data);path=Path(data['input']).resolve()
    if not path.is_file():raise FileNotFoundError('成片文件不存在。')
    report=read_json(path.parent/'校验报告.json')
    if not report.get('decode_verified'):raise RuntimeError('这份成片没有完成导出校验。')
    if Path(report.get('output','')).name.casefold()!=path.name.casefold():
        raise RuntimeError('校验报告对应的成片文件名不匹配。')
    phase(7,'读取成片音轨，识别实际项目',total=1,position=1)
    menu=attach(path,report,cfg)
    event('menu_complete','节目单已生成' if menu['status']=='ready' else '成片保留，节目单暂未生成',
          output=str(path),program_menu=menu)
    return menu
