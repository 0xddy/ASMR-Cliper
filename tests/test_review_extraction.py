import sys
import tempfile
import unittest
import subprocess
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.common import settings
from asmrclip.exclusions import summarize
from asmrclip.extraction import positive_asmr,extraction_regions
from asmrclip.planner import make_plan
from asmrclip.reviewer import output_to_source,review_speech,SpeechRemaining
from asmrclip.reviewer import decode_review_audio,review_windows,review_cache_key
from asmrclip.analysis import analyze
from asmrclip.exporter import export
from asmrclip.common import fingerprint


def row(a,**scores):return {'start':a,'end':a+6,**summarize(scores)}


class ReviewExtractionTests(unittest.TestCase):
    def test_review_cache_separates_content_model_language_and_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            for name in ('model.bin','config.json','preprocessor_config.json','tokenizer.json','vocabulary.json'):(path/name).write_text('test')
            original=review_cache_key(path,'ko','abc',10.,44100,2)
            self.assertNotEqual(original,review_cache_key(path,'ja','abc',10.,44100,2))
            self.assertNotEqual(original,review_cache_key(path,'ko','def',10.,44100,2))
            self.assertNotEqual(original,review_cache_key(path,'ko','abc',10.,48000,2))
            (path/'model.bin').write_text('changed model')
            self.assertNotEqual(original,review_cache_key(path,'ko','abc',10.,44100,2))
    def test_review_covers_full_timeline_without_overlapping_batch_clips(self):
        for duration in (.125,27.3,28.,28.01,65.2,302):
            groups=review_windows(duration)
            self.assertEqual(groups[0][0]['start'],0)
            self.assertEqual(groups[0][-1]['end'],duration)
            for clips in groups:
                self.assertLessEqual(sum(c['end']-c['start'] for c in clips),duration+1e-6)
                self.assertTrue(all(a['end']<=b['start'] for a,b in zip(clips,clips[1:])))
            for left,right in zip(groups[0],groups[0][1:]):
                self.assertTrue(any(c['start']<left['end']<c['end'] for c in groups[1]))
    def test_positive_evidence_not_just_absence_of_voice(self):
        self.assertFalse(positive_asmr(row(0)))
        self.assertFalse(positive_asmr(row(0,Breathing=.8)))
        self.assertFalse(positive_asmr(row(0,Water=.8)))
        self.assertTrue(positive_asmr(row(0,**{'Chewing, mastication':.7})))
        self.assertFalse(positive_asmr(row(0,**{'Chewing, mastication':.7,'Speech':.3})))
        self.assertFalse(positive_asmr(row(0,**{'Chewing, mastication':.7,'Spray':.3})))

    def test_single_hit_and_unknown_gaps_not_extracted(self):
        sound={'Chewing, mastication':.7}
        self.assertFalse(extraction_regions([row(0,**sound)])['intervals'])
        result=extraction_regions([row(0,**sound),row(2,**sound),row(4),row(6,**sound),row(8,**sound)])
        self.assertEqual(result['intervals'],[[2,6],[8,12]])

    def test_v4_never_admits_unknown_and_respects_speech_priority(self):
        class Texture:
            def windows(self,windows):return [{'start':a,'end':b,**summarize({'Chewing, mastication':.8})} for a,b in windows]
        rms=np.full(1600,.05,np.float32)
        for i in range(0,1600,20):rms[i:i+6]=.0003
        cfg=settings({'mode':'extract','keep_vaping':True})
        meta={'frame_samples':1024,'sample_rate':10240};frames={'levels':np.c_[rms,rms]}
        speech={'spoken':[[70,73]],'accepted':[]}
        guard={'extraction':{'intervals':[[30,120]]},'airflow':[[85,90]]}
        plan=make_plan(meta,frames,speech,[],cfg,Texture(),guard)
        self.assertGreater(plan['duration'],15)
        for a,b in plan['keep_frames']:
            self.assertGreaterEqual(a*.1,30);self.assertLessEqual(b*.1,120)
            self.assertFalse(any(min(b*.1,d)>max(a*.1,c) for c,d in [[70,73],[85,90]]))
        empty=make_plan(meta,frames,speech,[],cfg,Texture(),{})
        self.assertFalse(empty['keep_frames'])

    def test_review_word_crossing_join_maps_to_both_original_sections(self):
        findings=[{'start':8,'end':12}]
        self.assertEqual(output_to_source(findings,[[10,20],[40,50]],1),[[18,20],[40,42]])
        self.assertEqual(output_to_source([{'start':30,'end':40}],[[10,20]],1),[])

    def test_v4_retention_settings_apply_to_detected_heartbeat_and_tapping(self):
        class Texture:
            def windows(self,windows):return [{'start':a,'end':b,**summarize({'Heart sounds, heartbeat':.8})} for a,b in windows]
        rms=np.full(1600,.05,np.float32)
        for i in range(0,1600,20):rms[i:i+6]=.0003
        for category in ('heartbeat','tapping'):
            for retain in (True,False):
                cfg=settings({'mode':'extract','keep_'+category:retain})
                guard={'extraction':{'intervals':[[20,140]]},category:[[80,85]],'voice':[[40,43]]}
                plan=make_plan({'frame_samples':1024,'sample_rate':10240},{'levels':np.c_[rms,rms]},
                               {'spoken':[[110,113]],'accepted':[]},[],cfg,Texture(),guard)
                kept=[[a*.1,b*.1] for a,b in plan['keep_frames']]
                overlap=lambda c,d:sum(max(0,min(b,d)-max(a,c)) for a,b in kept)
                self.assertEqual(overlap(80,85),5 if retain else 0)
                self.assertEqual(overlap(40,43),0)
                self.assertEqual(overlap(110,113),0)

    def test_model_review_rejects_nonlexical_and_weak_hallucinations(self):
        base={'start':1,'end':3,'avg_logprob':-.5,'no_speech_prob':.1,'words':[{'probability':.7}]}
        for text in ('哈哈哈','ㅎㅎㅎ','hahaha','Thanks for watching','오늘도 영상 봐주셔서 감사합니다😊'):
            self.assertFalse(review_speech({**base,'text':text}))
        self.assertTrue(review_speech({**base,'text':'물 좀 마실게요'}))
        self.assertFalse(review_speech({**base,'text':'hello there','no_speech_prob':.95}))

    def test_review_config_validation(self):
        self.assertTrue(settings({})['review_enabled'])
        with self.assertRaises(ValueError):settings({'review_enabled':'true'})
        with self.assertRaises(ValueError):settings({'review_max_passes':0})
        with self.assertRaises(ValueError):settings({'review_max_passes':True})

    def test_only_actual_reviewed_candidate_can_be_published(self):
        ffmpeg=ROOT/'runtime/tools/ffmpeg.exe'
        if not ffmpeg.exists():self.skipTest('FFmpeg is not installed')
        with tempfile.TemporaryDirectory(prefix='ASMR review ') as folder:
            folder=Path(folder);source=folder/'source.m4a';out=folder/'output'
            subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','sine=frequency=337:sample_rate=44100:duration=5','-c:a','aac',str(source)],check=True)
            meta,frames=analyze(source,folder/'cache')
            plan={'keep_frames':[[5,meta['frames']//3],[meta['frames']//2,meta['frames']-5]]}
            cfg=settings({'mode':'extract','review_enabled':False})
            with self.assertRaises(RuntimeError):export(source,out,meta,frames,plan,cfg,fingerprint(source))
            self.assertEqual(list(out.iterdir()),[])
            class Reject:
                def inspect(self,path,report):
                    assert len(decode_review_audio(path))>1000
                    return {'status':'speech_found','findings':[{'start':1,'end':2,'text':'hello'}]}
            with self.assertRaises(SpeechRemaining):export(source,out,meta,frames,plan,cfg,fingerprint(source),Reject())
            self.assertEqual(list(out.iterdir()),[])
            flagged=export(source,out,meta,frames,plan,cfg,fingerprint(source),Reject(),allow_review_findings=True)
            self.assertEqual(flagged['speech_review']['status'],'needs_review')
            self.assertTrue(Path(flagged['output']).is_file())
            self.assertTrue(flagged['payload_unchanged'])
            self.assertTrue(flagged['decode_verified'])
            self.assertIn('hello',(Path(flagged['output']).parent/'人声复核.csv').read_text(encoding='utf-8-sig'))
            class Broken:
                def inspect(self,path,report):raise RuntimeError('model inference failed')
            before=set(out.iterdir())
            with self.assertRaisesRegex(RuntimeError,'model inference failed'):
                export(source,out,meta,frames,plan,cfg,fingerprint(source),Broken(),allow_review_findings=True)
            self.assertEqual(set(out.iterdir()),before)
            class Approve:
                def inspect(self,path,report):
                    assert len(decode_review_audio(path))>1000
                    return {'status':'passed','findings':[],'candidate_payload_sha256':report['payload_sha256']}
            report=export(source,out,meta,frames,plan,cfg,fingerprint(source),Approve())
            self.assertTrue(report['payload_unchanged'])
            self.assertEqual(report['speech_review']['candidate_payload_sha256'],report['payload_sha256'])
            self.assertTrue(Path(report['output']).name.endswith('_v4.m4a'))
            cfg=settings({'mode':'relaxed'})
            flagged=export(source,out,meta,frames,plan,cfg,fingerprint(source),Reject(),allow_review_findings=True)
            self.assertEqual(flagged['speech_review']['status'],'needs_review')
            self.assertTrue((Path(flagged['output']).parent/'人声复核.csv').exists())


    def test_pipeline_publishes_remaining_speech_in_every_mode_and_keeps_last_valid_candidate(self):
        import contextlib,io
        from unittest.mock import MagicMock,patch
        from asmrclip.pipeline import run
        ffmpeg=ROOT/'runtime/tools/ffmpeg.exe'
        if not ffmpeg.exists():self.skipTest('FFmpeg is not installed')
        with tempfile.TemporaryDirectory(prefix='ASMR flagged ') as folder:
            folder=Path(folder);source=folder/'source.m4a'
            subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i','sine=frequency=337:sample_rate=44100:duration=5','-c:a','aac',str(source)],check=True)
            with contextlib.redirect_stdout(io.StringIO()):meta,frames=analyze(source,folder/'prepared')
            dt=1024/meta['sample_rate'];base={'keep_frames':[[5,meta['frames']-5]],'frame_seconds':dt,'duration':(meta['frames']-10)*dt}
            for mode,empty in [('strict',False),('relaxed',False),('extract',False),('extract',True)]:
                with self.subTest(mode=mode,replan_empty=empty):
                    cfg=settings({'input':str(source),'output_dir':str(folder/'out'),'cache_dir':str(folder/'cache'),
                                  'mode':mode,'review_max_passes':2,'audit':False})
                    recognizer=MagicMock();recognizer.scan.return_value={'spoken':[],'language':'en'}
                    classifier=MagicMock();classifier.music_intervals.return_value=[]
                    classifier.exclusions.return_value={key:[] for key in ('voice','soft_laugh','heartbeat','tapping','loud_laugh','airflow','drinking','impacts')}
                    reviewer=MagicMock()
                    reviewer.inspect.side_effect=lambda path,report:{'status':'speech_found',
                        'findings':[{'start':.2,'end':.4,'text':'possible speech'}],
                        'candidate_payload_sha256':report['payload_sha256']}
                    plans=[dict(base),{**base,'keep_frames':[]} if empty else dict(base)]
                    with patch('asmrclip.model_catalog.validate_models'),patch('asmrclip.reviewer.validate_review_model'), \
                         patch('asmrclip.recognition.Recognizer',return_value=recognizer),patch('asmrclip.classifier.Classifier',return_value=classifier), \
                         patch('asmrclip.semantic.confirm',return_value={}),patch('asmrclip.reviewer.Reviewer',return_value=reviewer), \
                         patch('asmrclip.transitions.review_transitions',return_value=([],{'candidates':[]})), \
                         patch('asmrclip.planner.make_plan',side_effect=plans),contextlib.redirect_stdout(io.StringIO()) as output:
                        report=run(cfg)
                    self.assertTrue(Path(report['output']).is_file())
                    self.assertTrue(report['payload_unchanged']);self.assertTrue(report['decode_verified'])
                    self.assertEqual(report['speech_review']['status'],'needs_review')
                    self.assertEqual(report['speech_review']['candidate_payload_sha256'],report['payload_sha256'])
                    import json
                    events=[json.loads(line) for line in output.getvalue().splitlines()]
                    self.assertEqual(events[-1]['type'],'complete')
                    self.assertFalse(any(e['type']=='error' for e in events))
                    rounds=[e['task_progress']['round'] for e in events if e.get('task_progress',{}).get('stage')==5]
                    self.assertEqual(max(rounds),1 if empty else 2)
                    self.assertEqual(reviewer.inspect.call_count,2)
            # Constructor mocks retain mmap arguments in cyclic call records.
            # Release those before Windows removes the temporary cache files.
            import gc
            gc.collect()


if __name__=='__main__':unittest.main()
