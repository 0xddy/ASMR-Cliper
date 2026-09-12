from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.analysis import analyze
from asmrclip.common import settings,fingerprint
from asmrclip.exporter import export
from asmrclip.pauses import limit_keeps,quiet_spans,joined_spans,QuietMeter,limit_video,fade_seconds
from asmrclip.planner import make_plan

FFMPEG=ROOT/'runtime/tools/ffmpeg.exe'


class Texture:
    def windows(self,windows):
        return [{'start':a,'end':b,'texture':.85,'breath':.01,'speech':.01} for a,b in windows]


class PausePlanningTests(unittest.TestCase):
    def test_two_to_three_second_blank_is_shortened_in_all_modes_without_losing_active_audio(self):
        dt=.02;levels=np.full((6000,2),.05,np.float32);levels[500:640]=0
        for mode in ('strict','relaxed','extract'):
            cfg=settings({'mode':mode});meta={'sample_rate':51200,'frame_samples':1024}
            plan=make_plan(meta,{'levels':levels},{'spoken':[],'accepted':[]},[],cfg,Texture(),
                           {'extraction':{'intervals':[[0,120]]}})
            kept=np.zeros(len(levels),bool)
            for a,b in plan['keep_frames']:kept[a:b]=True
            self.assertTrue(np.all(kept[levels[:,0]>.01]),mode)
            pauses=joined_spans(plan['keep_frames'],quiet_spans(levels,cfg['silence_db']))
            self.assertLessEqual(max(b-a for a,b in pauses)*dt,1.5)
            self.assertLess(plan['duration'],119.)

    def test_quiet_tails_add_together_across_removed_speech(self):
        levels=np.full((400,2),.03,np.float32);levels[100:110]=0;levels[200:210]=0
        keeps=[[0,110],[200,400]];cfg=settings({'max_pause_seconds':1.2})
        bounded,removed=limit_keeps(keeps,levels,.1,cfg)
        self.assertTrue(removed)
        self.assertLessEqual(max(b-a for a,b in joined_spans(bounded,quiet_spans(levels,-58)))*.1,1.2)
        self.assertEqual(sum(np.count_nonzero(levels[a:b,0]) for a,b in bounded),sum(np.count_nonzero(levels[a:b,0]) for a,b in keeps))
        self.assertFalse(any(a<200 and b>110 for a,b in bounded))

    def test_short_pause_and_faint_peaks_are_retained(self):
        levels=np.full((400,2),.03,np.float32);levels[30:38]=0
        levels[100:200]=[.0003,.003]  # Low RMS, but audible small impulses.
        cfg=settings({});bounded,removed=limit_keeps([[0,400]],levels,.1,cfg)
        self.assertEqual(bounded,[[0,400]]);self.assertEqual(removed,[])

    def test_binaural_phase_cancellation_does_not_look_like_silence(self):
        meter=QuietMeter(1000,2,-58)
        signal=np.sin(np.arange(3000)*.3)*.02
        for a in range(0,3000,113):meter.add(np.array([signal[a:a+113],-signal[a:a+113]],np.float32))
        self.assertEqual(meter.finish(1.5)['max_detected_seconds'],0)

    def test_legacy_threshold_does_not_override_new_pause_setting(self):
        cfg=settings({'silence_seconds':7})
        self.assertEqual(cfg['max_pause_seconds'],1.5);self.assertNotIn('silence_seconds',cfg)
        self.assertEqual(settings({'silence_seconds':7,'max_pause_seconds':2})['max_pause_seconds'],2)
        for value in (0,.1,11,float('nan')):
            with self.subTest(value=value),self.assertRaises(ValueError):settings({'max_pause_seconds':value})
        self.assertLess(2*fade_seconds(settings({'join_fade_seconds':2})),1.5)

    def test_video_rechecks_joined_quiet_after_gop_alignment(self):
        levels=np.full((800,2),.03,np.float32);levels[200:300]=0;levels[500:600]=0
        groups=[{'start':i,'end':i+1,'first':i*25,'stop':(i+1)*25} for i in range(8)]
        intervals=[{'source_start':0,'source_end':3,'analysis_start':0,'analysis_end':3,'first':0,'stop':75},
                   {'source_start':5,'source_end':8,'analysis_start':5,'analysis_end':8,'first':125,'stop':200}]
        trimmed=limit_video(intervals,groups,levels,.01,settings({}))
        self.assertTrue(all(p['source_end']<=3 or p['source_start']>=5 for p in trimmed))
        spans=joined_spans([[p['analysis_start']*100,p['analysis_end']*100] for p in trimmed],quiet_spans(levels,-58))
        self.assertLessEqual(max((b-a for a,b in spans),default=0)*.01,1.5)


class PauseExportTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG.exists():self.skipTest('FFmpeg required')
        self.temp=tempfile.TemporaryDirectory(prefix='ASMR 空窗 ');self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def source(self,video=False):
        path=self.folder/('source.mp4' if video else 'source.m4a')
        command=[str(FFMPEG),'-v','error','-f','lavfi','-i',
                 "aevalsrc='if(between(t,7,9.8),0,0.06*sin(2*PI*357*t))':s=48000:d=18"]
        if video:command+=['-f','lavfi','-i','testsrc2=size=160x96:rate=25:duration=18','-map','1:v:0','-map','0:a:0','-c:v','libx264','-g','25','-bf','3','-sc_threshold','0']
        subprocess.run(command+['-ac','2','-c:a','aac',str(path)],check=True,stderr=subprocess.PIPE)
        return path

    def independent_silence_check(self,path):
        run=subprocess.run([str(FFMPEG),'-hide_banner','-nostdin','-i',str(path),'-map','0:a:0','-af',
                            'silencedetect=n=-58dB:d=0.1','-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True)
        return max([float(s) for s in re.findall(r'silence_duration: ([0-9.]+)',run.stderr.decode(errors='replace'))] or [0.])

    def test_actual_audio_and_video_stay_within_configured_pause_limit_with_fades_on_or_off(self):
        for video in (False,True):
            source=self.source(video);meta,frames=analyze(source,self.folder/('video-cache' if video else 'audio-cache'))
            for fade,edge,limit,fade_time in ((False,False,1.5,.3),(False,True,1.5,.5),(True,True,1.5,.3),(True,True,.8,2)):
                with self.subTest(video=video,fade=fade,limit=limit):
                    cfg=settings({'mode':'relaxed','review_enabled':False,'max_pause_seconds':limit,
                                  'join_fade_enabled':fade,'join_fade_seconds':fade_time,'edge_fade_enabled':edge,'edge_fade_seconds':fade_time})
                    plan=make_plan(meta,frames,{'spoken':[],'accepted':[]},[],cfg,Texture())
                    report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
                    self.assertTrue(report['pause_check']['within_limit'],report['pause_check'])
                    self.assertLessEqual(self.independent_silence_check(report['output']),limit)
                    self.assertEqual(report['payload_unchanged'],not (fade or edge))
                    if video:self.assertTrue(report['video_payload_unchanged'])
                    self.assertTrue(report['decode_verified'])
                    self.assertEqual(report['settings']['max_pause_seconds'],limit)


if __name__=='__main__':unittest.main()
