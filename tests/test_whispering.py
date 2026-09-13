"""Whisper permission is bounded by acoustic evidence and export coordinates."""
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.whispering import subtract,covers,positive,confirmed_regions,detect,apply_review,output_intervals
from asmrclip.common import settings,fingerprint
from asmrclip.exclusions import summarize,strong_voice
from asmrclip.classifier import Classifier
from asmrclip.model_catalog import required_components
from asmrclip.planner import make_plan,scene_guard
from asmrclip.semantic import confirm


def whisper(a,b):
    return {'start':a,'end':b,'whisper':.8,'voiced':.2,'speech':.85,'lexical':.85,
            'texture':.01,'breath':.1,'expressive':0.,'semantic':{'whisper_asmr':.6,'normal_speech':.25,'speech':.62,'mouth':.2}}


class WhisperTests(unittest.TestCase):
    def test_short_crops_use_confirmed_context_but_still_require_fine_semantics(self):
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            classifier=Mock();matcher=Mock();matcher.available.return_value=True
            def windows(spans):
                return [{**whisper(a,b),'whisper':.8 if b-a>=9 else .01} for a,b in spans]
            classifier.windows.side_effect=windows
            matcher.score.side_effect=lambda rows,*args,**kwargs:rows
            result,_=detect({},np.zeros(20*16000,np.int16),Path(directory),classifier,matcher,[],20)
            self.assertTrue(result)
            def normal_fine(rows,*args,**kwargs):
                return [{**r,'semantic':{'whisper_asmr':.3,'normal_speech':.7}} if r['end']-r['start']<4 else r for r in rows]
            matcher.score.side_effect=normal_fine
            result,_=detect({},np.zeros(20*16000,np.int16),Path(directory),classifier,matcher,[],20)
            self.assertEqual(result,[])
            self.assertFalse(positive({**whisper(0,3),'whisper':.01,'whisper_context':True,'voiced':.8}))

    def test_v4_keeps_full_classification_context_then_subtracts_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            classifier=Mock();matcher=Mock()
            classifier.windows.side_effect=lambda spans:[{'start':a,'end':b,'texture':.1 if b-a>=9 else 0.,
                'semantic':{'mouth':.6,'speech':.1}} for a,b in spans]
            matcher.score.side_effect=lambda rows,*args,**kwargs:rows
            result=confirm({},np.zeros(20*16000,np.int16),Path(directory),classifier,
                           {'spoken':[[6,8],[12,14]]},[],{},20,matcher)
            self.assertTrue(result['intervals'])
            self.assertTrue(any(b-a==10 for a,b in classifier.windows.call_args.args[0]))
            for a,b in result['intervals']:
                self.assertFalse(any(a<d and b>c for c,d in [[6,8],[12,14]]))

    def test_program_menu_can_name_whispers_without_calling_them_normal_speech(self):
        from asmrclip.program_menu import identify,chapters
        self.assertEqual(identify({'whisper':.6,'speech':.3})[0],'whisper')
        self.assertEqual(identify({'whisper':.3,'speech':.6})[0],'speech')
        self.assertEqual(chapters([{'start':0,'end':5,'category':'whisper'}])[0]['title'],'轻语 / 耳语')

    def test_default_validation_and_model_requirements(self):
        self.assertTrue(settings({})['keep_whisper'])
        with self.assertRaises(ValueError):settings({'keep_whisper':'true'})
        cfg={'mode':'relaxed','speech_model':'whisper-turbo','review_enabled':False,'keep_drinking':True}
        self.assertIn('clap',required_components(cfg))
        self.assertNotIn('clap',required_components({**cfg,'keep_whisper':False}))

    def test_whisper_score_separate_from_speech_and_permission_does_not_change_raw_cache(self):
        r=summarize({'Whispering':.9,'Speech':.92,'Female speech, woman speaking':.1})
        self.assertEqual(r['whisper'],.9);self.assertEqual(r['voiced'],.1);self.assertTrue(strong_voice(r))
        classifier=Classifier.__new__(Classifier);classifier.cached={'a':{'start':0,'end':3,**r}}
        classifier.retained_whispers=[[0,3]]
        self.assertFalse(strong_voice(classifier.records(['a'])[0]))
        self.assertNotIn('retained_whisper',classifier.cached['a'])
        classifier.retained_whispers=[[.2,3]]
        self.assertTrue(strong_voice(classifier.records(['a'])[0]))
        self.assertTrue(strong_voice({**r,'retained_whisper':True,'expressive':.6}))
        self.assertTrue(strong_voice({**r,'retained_whisper':True,'whisper':.1,'voiced':.9}))

    def test_normal_speech_and_unconfirmed_soft_sound_do_not_qualify(self):
        good=whisper(0,3);self.assertTrue(positive(good))
        for update in ({'whisper':.1},{'voiced':.9},{'expressive':.4},{'quiet':True},
                       {'semantic':{'whisper_asmr':.6,'normal_speech':.59}},
                       {'semantic':{'whisper_asmr':.6,'mouth':.7}}):
            self.assertFalse(positive({**good,**update}))

    def test_repeated_evidence_keeps_centers_and_does_not_bridge_normal_speech(self):
        rows=[whisper(t,t+3) for t in range(9)]
        rows[4]={**rows[4],'voiced':.95}
        intervals,_=confirmed_regions(rows,[[0,11]])
        self.assertEqual(intervals,[[0,5.],[6.,11]])
        self.assertFalse(confirmed_regions([whisper(3,6)],[[0,10]])[0])
        self.assertFalse(confirmed_regions([whisper(0,3),whisper(6,9)],[[0,9]])[0])

    def test_subtraction_and_coverage_handle_mixed_sentences(self):
        self.assertEqual(subtract([[0,20]],[[2,5],[4,8],[12,15]]),[[0,2],[8,12],[15,20]])
        self.assertFalse(covers(1,7,[[2,8]]));self.assertTrue(covers(2,8,[[2,8]]))

    def test_detector_off_has_no_model_side_effects_and_on_records_evidence(self):
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            cache=Path(directory);pcm=np.zeros(12*16000,np.int16);classifier=Mock();matcher=Mock()
            self.assertEqual(detect({'keep_whisper':False},pcm,cache,classifier,matcher,[],12)[0],[])
            classifier.windows.assert_not_called();matcher.available.assert_not_called()
            classifier.windows.side_effect=lambda spans:[whisper(a,b) for a,b in spans]
            matcher.available.return_value=True
            matcher.score.side_effect=lambda rows,*args,**kwargs:rows
            intervals,report=detect({},pcm,cache,classifier,matcher,[[5,7]],12)
            self.assertTrue(intervals)
            self.assertFalse(any(a<7 and b>5 for a,b in intervals))
            self.assertEqual(json.loads((cache/'whisper-review.json').read_text('utf8'))['intervals'],intervals)
            self.assertEqual(report['status'],'checked');self.assertEqual(classifier.close.call_count,2)

    def test_all_modes_keep_verified_whispers_and_still_delete_chat_after_replanning(self):
        class Texture:
            def windows(self,spans):return [{'start':a,'end':b,'texture':.8,'speech':0.,'breath':0.} for a,b in spans]
        levels=np.full(1600,.05,np.float32)
        for i in range(0,1600,20):levels[i:i+6]=.0003
        exclusions={'voice':[[40,60],[110,113]],'whisper':[[40,60]],'extraction':{'intervals':[[0,160]]}}
        speech={'spoken':[[40,60],[110,113]],'accepted':[{'start':40,'end':60},{'start':110,'end':113}]}
        for mode in ('strict','relaxed','extract'):
            for keep in (True,False):
                cfg=settings({'mode':mode,'keep_whisper':keep,'strict_min_section':5,'strict_dense_gap':0})
                plan=make_plan({'frame_samples':1024,'sample_rate':10240},{'levels':np.c_[levels,levels]},
                               speech,[],cfg,Texture(),exclusions)
                self.assertTrue(plan['keep_frames'])
                overlap=lambda c,d:sum(max(0,min(b*.1,d)-max(a*.1,c)) for a,b in plan['keep_frames'])
                self.assertEqual(overlap(45,55),10 if keep else 0)
                self.assertEqual(overlap(110,113),0)
        self.assertEqual(scene_guard([{**whisper(40,42),'retained_whisper':True}],40,'start')[0],40)

    def test_v4_adds_only_verified_whispers_outside_music_breaks_and_speech(self):
        classifier=Mock();classifier.windows.side_effect=lambda spans:[{'start':a,'end':b,'quiet':True} for a,b in spans]
        matcher=Mock();matcher.score.return_value=[]
        with tempfile.TemporaryDirectory() as directory:
            exclusions={'whisper':[[5,20]],'drinking':[[12,15]]}
            report=confirm({'keep_whisper':True},np.zeros(30*16000,np.int16),Path(directory),classifier,
                           {'spoken':[[5,6]]},[[19,20]],exclusions,30,matcher)
            self.assertEqual(report['intervals'],[[6,12],[15,19]])
            report=confirm({'keep_whisper':False},np.zeros(30*16000,np.int16),Path(directory),classifier,
                           {'spoken':[]},[],exclusions,30,matcher)
            self.assertEqual(report['intervals'],[])

    def test_review_uses_actual_mapping_and_only_exempts_verified_parts_without_mutating_cache(self):
        plan={'acoustic_exclusions':{'whisper':[[101,105],[200,204]]}}
        report={'mapping':[{'analysis_start':102,'analysis_end':106,'output_start':0,'output_end':4},
                           {'analysis_start':202,'analysis_end':205,'output_start':4,'output_end':7}]}
        raw={'status':'speech_found','findings':[{'start':0,'end':7,'text':'mixed utterance'}]}
        self.assertEqual(output_intervals(plan,report,{}),[[0,3],[4,6]])
        result=apply_review(raw,plan,report,{})
        self.assertEqual([[r['start'],r['end']] for r in result['findings']],[[3,4],[6,7]])
        self.assertEqual(raw['findings'][0]['end'],7)
        self.assertEqual(apply_review(raw,plan,report,{'keep_whisper':False})['findings'],raw['findings'])
        approved=apply_review({'status':'speech_found','findings':[{'start':.5,'end':2,'text':'soft words'}]},plan,report,{})
        self.assertEqual(approved['status'],'passed');self.assertEqual(len(approved['allowed_whisper']),1)
        broken={'status':'error','findings':[]}
        self.assertEqual(apply_review(broken,plan,report,{}),broken)
        self.assertEqual(apply_review(raw,{},report,{})['status'],'speech_found')

    def test_real_export_preserves_whispers_and_turning_option_off_rejects_same_review(self):
        from asmrclip.analysis import analyze
        from asmrclip.exporter import export
        from asmrclip.reviewer import SpeechRemaining
        ffmpeg=ROOT/'runtime/tools/ffmpeg.exe'
        if not ffmpeg.exists():self.skipTest('FFmpeg unavailable')
        with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
            folder=Path(directory);source=folder/'sample.m4a'
            subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','sine=duration=6','-c:a','aac',str(source)],check=True)
            meta,frames=analyze(source,folder/'cache')
            plan={'keep_frames':[[15,80],[110,200]],'acoustic_exclusions':{'whisper':[[0,6]]}}
            class RepeatedReview:
                def inspect(self,path,report):return {'status':'speech_found','findings':[{'start':.2,'end':.8,'text':'soft whisper'}]}
            cfg=settings({'mode':'relaxed','keep_whisper':True})
            report=export(source,folder/'out',meta,frames,plan,cfg,fingerprint(source),RepeatedReview())
            self.assertEqual(report['speech_review']['status'],'passed')
            self.assertTrue(report['whisper_retained']);self.assertTrue(report['decode_verified'])
            self.assertIn('轻语 / 耳语',(Path(report['output']).parent/'轻语保留.csv').read_text('utf-8-sig'))
            with self.assertRaises(SpeechRemaining):
                export(source,folder/'out',meta,frames,plan,{**cfg,'keep_whisper':False},fingerprint(source),RepeatedReview())


if __name__=='__main__':unittest.main()
