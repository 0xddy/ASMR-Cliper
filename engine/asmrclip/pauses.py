"""Limit quiet runs on the joined playback clock, including across source cuts."""
from bisect import bisect_right
import math

import numpy as np

from .common import complement,merge


def fade_seconds(cfg):
    # Leave room for a real pause; long user fades must not create a new blank.
    return min(cfg.get('join_fade_seconds',.3),cfg.get('max_pause_seconds',1.5)*.225)


def quiet_budget(cfg,dt):
    limit=cfg.get('max_pause_seconds',1.5)
    margin=min(limit*.1,max(.1,dt*2))
    fades=2*fade_seconds(cfg) if cfg.get('join_fade_enabled',False) else 0.
    return max(dt,limit-margin-fades)


def quiet_spans(levels,threshold):
    # Keep a quiet channel from hiding activity in the other ear. Do not close
    # holes: even a brief audible tap or mouth sound interrupts a quiet run.
    limit=10**(threshold/20)
    mask=(levels[:,0]<limit)&(levels[:,1]<limit*10**(3/20))
    return np.flatnonzero(np.diff(np.r_[False,mask,False])).reshape(-1,2).tolist()


def joined_spans(keeps,quiet):
    result=[];cursor=0.;starts=[a for a,_ in quiet]
    for a,b in keeps:
        index=max(0,bisect_right(starts,a)-1)
        while index<len(quiet) and quiet[index][0]<b:
            lo=max(a,quiet[index][0]);hi=min(b,quiet[index][1])
            if hi>lo:result.append([cursor+lo-a,cursor+hi-a])
            index+=1
        cursor+=b-a
    return merge(result,1e-8)


def trim_quiet(keeps,quiet,budget,integer=False):
    cuts=[]
    for a,b in joined_spans(keeps,quiet):
        if b-a>budget+1e-8:
            left=math.floor(budget/2) if integer else budget/2
            cuts.append([a+left,b-(budget-left)])
    result=[];removed=[];cursor=0.
    for a,b in keeps:
        local=[[max(0,c-cursor),min(b-a,d-cursor)] for c,d in cuts if d>cursor and c<cursor+b-a]
        removed.extend([[a+c,a+d] for c,d in local])
        result.extend([[a+c,a+d] for c,d in complement(local,b-a)])
        cursor+=b-a
    return result,removed


def limit_keeps(keeps,levels,dt,cfg,quiet=None):
    bounded,removed=trim_quiet(keeps,quiet if quiet is not None else quiet_spans(levels,cfg['silence_db']),
                              max(1,math.floor(quiet_budget(cfg,dt)/dt)),integer=True)
    return [[round(a),round(b)] for a,b in bounded],[[round(a),round(b)] for a,b in removed]


def limit_video(intervals,groups,levels,dt,cfg):
    """Remove complete GOPs touching excess quiet; never restore excluded media."""
    quiet=[[a*dt,b*dt] for a,b in quiet_spans(levels,cfg['silence_db'])]
    budget=quiet_budget(cfg,dt)
    _,excess=trim_quiet([[p['analysis_start'],p['analysis_end']] for p in intervals],quiet,budget)
    if not excess:return intervals
    # Expand once into complete, already-approved GOPs. The loop always removes
    # at least one GOP, so a new pause exposed by deletion is checked as well.
    pieces=[];firsts=[g['first'] for g in groups]
    for index,row in enumerate(intervals):
        pos=bisect_right(firsts,row['first'])-1
        while pos<len(groups) and groups[pos]['stop']<=row['stop']:
            g=groups[pos]
            if g['first']>=row['first']:
                start=row['analysis_start']+g['start']-row['source_start']
                pieces.append({'source_start':g['start'],'source_end':g['end'],'first':g['first'],'stop':g['stop'],
                               'analysis_start':start,'analysis_end':start+g['end']-g['start'],'unit':index})
            pos+=1
    while pieces:
        _,removed=trim_quiet([[p['analysis_start'],p['analysis_end']] for p in pieces],quiet,budget)
        if not removed:break
        remaining=[p for p in pieces if not any(min(p['analysis_end'],b)>max(p['analysis_start'],a)+1e-8 for a,b in removed)]
        if len(remaining)==len(pieces):raise AssertionError('视频空窗边界未能收紧。')
        pieces=remaining
    if not pieces:raise ValueError('按最长空窗期调整后，没有完整的视频关键帧片段可保留。请选仅音频输出或调大最长空窗期。')
    result=[]
    for p in pieces:
        if result and result[-1]['unit']==p['unit'] and result[-1]['stop']==p['first']:
            result[-1].update(source_end=p['source_end'],analysis_end=p['analysis_end'],stop=p['stop'])
        else:result.append(dict(p))
    for row in result:row.pop('unit')
    return result


class QuietMeter:
    """Streaming 20 ms native-rate RMS/peak windows and sparse pause diagnostics."""
    def __init__(self,rate,channels,threshold):
        self.rate=rate;self.window=max(1,round(rate*.02));self.threshold=10**(threshold/20)
        self.energy=np.zeros(channels,np.float64);self.peak=0.;self.count=0;self.cursor=0
        self.start=None;self.spans=[];self.longest=0.

    def _close_run(self):
        a,b=self.start/self.rate,self.cursor/self.rate
        self.longest=max(self.longest,b-a)
        # Configured limits start at 0.3 s; do not store microscopic quiet runs.
        if b-a>=.3:self.spans.append([a,b])
        self.start=None

    def _finish(self):
        silent=np.sqrt((self.energy/max(self.count,1)).max())<self.threshold and self.peak<self.threshold*10**(3/20)
        if silent:
            if self.start is None:self.start=self.cursor
        elif self.start is not None:
            self._close_run()
        self.cursor+=self.count;self.count=0;self.energy.fill(0);self.peak=0.

    def add(self,pcm):
        pos=0
        while pos<pcm.shape[1]:
            take=min(self.window-self.count,pcm.shape[1]-pos);part=pcm[:,pos:pos+take].astype(np.float64)
            self.energy+=np.square(part).sum(axis=1);self.peak=max(self.peak,float(np.abs(part).max()))
            self.count+=take;pos+=take
            if self.count==self.window:self._finish()

    def finish(self,limit):
        if self.count:self._finish()
        if self.start is not None:self._close_run()
        longest=self.longest
        return {'limit_seconds':limit,'max_detected_seconds':round(longest,4),'within_limit':longest<=limit+1e-6,
                'measurement_window_seconds':self.window/self.rate,
                'over_limit':[{'start':a,'end':b,'duration':b-a} for a,b in self.spans if b-a>limit+1e-6]}
