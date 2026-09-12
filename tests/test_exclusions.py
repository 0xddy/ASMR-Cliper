import sys
import json
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from asmrclip.exclusions import summarize,strong_voice,airflow_events,supported_utterance,drinking_events,extend_breaks,laugh_kind,selected_exclusions,impact_events,rhythmic_events,KEEP_DEFAULTS
from asmrclip.recognition import word_intervals,plausible
from asmrclip.planner import make_plan,scene_guard
from asmrclip.common import settings


def record(a,b,**scores): return {'start':a,'end':b,**summarize(scores)}


class ExclusionRequirements(unittest.TestCase):
    def test_emotional_voice_cannot_be_overridden_by_texture(self):
        r=record(10,12,Shout=.8,Crunch=.9)
        self.assertTrue(strong_voice(r))
        edge,reasons=scene_guard([r],10,'start')
        self.assertEqual(edge,12)
        self.assertTrue(reasons)

    def test_speech_subtypes_are_not_lost(self):
        for name in ('Whispering','Female speech, woman speaking','Conversation','Yell','Crying, sobbing'):
            with self.subTest(name=name): self.assertTrue(strong_voice(record(0,2,**{name:.8})))

    def test_laughter_is_separate_from_mandatory_speech(self):
        for name in ('Laughter','Giggle','Snicker','Belly laugh','Chuckle, chortle'):
            with self.subTest(name=name):
                r=record(0,2,**{name:.65,'Speech':.75})
                self.assertFalse(strong_voice(r))
                self.assertEqual(laugh_kind(r),'soft_laugh' if name in ('Giggle','Snicker','Chuckle, chortle') else 'loud_laugh')
        self.assertTrue(strong_voice(record(0,2,Giggle=.8,Conversation=.9)))
        rows=[record(0,6,**{'Chewing, mastication':.8}),record(4,10,Breathing=.3),
              record(8,14,Giggle=.7),record(12,18,Laughter=.6),record(16,22,Breathing=.4),
              {**record(20,26),'quiet':True},record(24,30,**{'Chewing, mastication':.8})]
        self.assertEqual(extend_breaks([[8,18]],rows),[[4,26]])

    def test_nonlexical_soft_laughter_is_not_promoted_by_asr_or_audit(self):
        for text in ('呵呵呵','哈哈哈','ㅎㅎㅎ','하하하','hahaha'):
            s={'start':1,'end':2,'text':text,'avg_logprob':-.3,'words':[{'probability':.8}]}
            self.assertFalse(plausible(s,[[0,4]],True))
            self.assertFalse(supported_utterance(s,record(1,2,Giggle=.7,Speech=.7)))
        s={'start':1,'end':2,'text':'That was funny','avg_logprob':-.3,'words':[{'probability':.8}]}
        self.assertTrue(supported_utterance(s,record(1,2,Giggle=.7,Speech=.7)))

    def test_each_keep_option_controls_both_modes_and_does_not_allow_speech(self):
        class Texture:
            def windows(self,windows): return [record(a,b,**{'Chewing, mastication':.8}) for a,b in windows]
        rms=np.full(1600,.05,np.float32)
        for i in range(0,1600,20):rms[i:i+6]=.0003
        for mode in ('strict','relaxed'):
            for category,key in [('soft_laugh','keep_soft_laugh'),('loud_laugh','keep_loud_laugh'),('airflow','keep_vaping'),('drinking','keep_drinking'),('impacts','keep_impacts'),('heartbeat','keep_heartbeat'),('tapping','keep_tapping')]:
                for retain in (False,True):
                    with self.subTest(mode=mode,category=category,retain=retain):
                        cfg=settings({'mode':mode,'strict_min_section':5,'strict_dense_gap':0,key:retain})
                        report={category:[[80,85]],'voice':[[40,43]]}
                        plan=make_plan({'frame_samples':1024,'sample_rate':10240},{'levels':np.c_[rms,rms]},
                                       {'spoken':[[120,123]],'accepted':[]},[],cfg,Texture(),report)
                        kept=[[a*.1,b*.1] for a,b in plan['keep_frames']]
                        overlap=lambda c,d:sum(max(0,min(b,d)-max(a,c)) for a,b in kept)
                        self.assertEqual(overlap(80,85),5 if retain else 0)
                        self.assertEqual(overlap(40,43),0)
                        self.assertEqual(overlap(120,123),0)

    def test_rhythmic_categories_need_repeated_evidence_and_do_not_relabel_accidents(self):
        for category,label in [('heartbeat','Heart sounds, heartbeat'),('tapping','Tap')]:
            rows=[record(t,t+6,**{label:.8}) for t in (0,2,4)]
            self.assertEqual(rhythmic_events(rows,category),[[0,10]])
            self.assertFalse(rhythmic_events(rows[:1],category))
            self.assertGreater(rows[0]['texture'],.5)
        crashes=[record(t,t+6,Tap=.6,**{'Smash, crash':.8}) for t in (0,2,4)]
        self.assertFalse(rhythmic_events(crashes,'tapping'))
        mouth=[record(t,t+6,Tap=.3,**{'Chewing, mastication':.9}) for t in (0,2,4)]
        self.assertFalse(rhythmic_events(mouth,'tapping'))

    def test_retention_defaults_and_type_validation(self):
        self.assertEqual({k:settings({})[k] for k in KEEP_DEFAULTS},KEEP_DEFAULTS)
        with self.assertRaises(ValueError):settings({'keep_loud_laugh':'false'})

    def test_impact_requires_class_evidence_and_isolated_burst(self):
        rate=16000; rng=np.random.default_rng(12)
        pcm=rng.normal(0,50,rate*12).astype(np.int16)
        burst=(np.sin(np.arange(1600)*.3)*12000).astype(np.int16)
        pcm[5*rate:5*rate+1600]=burst
        events=impact_events([record(3,9,**{'Thump, thud':.7})],pcm)
        self.assertEqual(len(events),1)
        self.assertLess(events[0][0],5);self.assertGreater(events[0][1],5.1)
        self.assertFalse(impact_events([record(3,9,Tap=.9,Crunch=.7)],pcm))
        self.assertFalse(impact_events([record(3,9,Bang=.8)],np.full(rate*12,800,np.int16)))
        for t in range(2,10):pcm[t*rate:t*rate+1600]=burst
        self.assertFalse(impact_events([record(3,9,Clatter=.8)],pcm))

    def test_low_confidence_words_inside_recognized_phrase_are_removed(self):
        utterance={'start':1,'end':4,'words':[{'start':1,'end':2,'probability':.03},
                   {'start':2,'end':3,'probability':.95},{'start':3,'end':4,'probability':.02}]}
        self.assertEqual(word_intervals([utterance]),[[1,4]])

    def test_short_emotional_utterance_requires_independent_voice_evidence(self):
        s={'start':1,'end':2,'text':'啊！','avg_logprob':-.9,'words':[{'probability':.45}]}
        self.assertFalse(plausible(s,[[0,4]],True))
        self.assertTrue(supported_utterance(s,record(1,2,Speech=.35)))
        self.assertFalse(supported_utterance(s,record(1,2,Gasp=.8,Crunch=.5)))

    def test_common_hallucination_is_not_promoted_by_weak_audio(self):
        s={'start':1,'end':2,'text':'Thanks for watching','avg_logprob':-.9,'words':[{'probability':.45}]}
        self.assertFalse(supported_utterance(s,record(1,2,Speech=.4)))

    def test_repeating_air_release_with_breath_is_excluded_as_a_gesture(self):
        records=[record(0,6,**{'Chewing, mastication':.7}),record(4,10,Breathing=.4),
                 record(8,14,Spray=.5,Steam=.08),record(10,16,Spray=.3,Hiss=.08),
                 record(14,20,Breathing=.3),record(18,24,**{'Chewing, mastication':.7})]
        events=airflow_events(records)
        self.assertEqual(events,[[4,20]])

    def test_breath_and_crackle_alone_are_not_vaping(self):
        for scores in ({'Breathing':.9},{'Gasp':.9},{'Crackle':.9},{'Hiss':.5},{'Spray':.5}):
            with self.subTest(scores=scores):
                self.assertFalse(airflow_events([record(t,t+6,**scores) for t in range(0,30,2)]))

    def test_mouth_actions_are_preserved_despite_weak_airflow(self):
        rows=[record(t,t+6,**{'Chewing, mastication':.7,'Gasp':.1,'Spray':.2}) for t in range(0,30,2)]
        self.assertFalse(airflow_events(rows))

    def test_reported_83_minute_airflow_scores_exclude_break_not_adjacent_mouth(self):
        # Actual AST scores from the reported edited-audio region. No recording
        # or speech transcript is bundled with this regression fixture.
        path=Path(__file__).parent/'fixtures/reported-airflow-scores.json'
        rows=[record(r['start'],r['end'],**r['top']) for r in json.loads(path.read_text(encoding='utf8'))]
        events=airflow_events(rows)
        removed=sum(max(0,min(b,4977)-max(a,4949)) for a,b in events)
        self.assertGreaterEqual(removed,26)
        self.assertFalse(any(min(b,5020)>max(a,5002) for a,b in events))

    def test_interior_exclusions_survive_planning_and_later_audit(self):
        class Texture:
            def windows(self,windows): return [record(a,b,**{'Chewing, mastication':.8}) for a,b in windows]
        rms=np.full(1400,.05,np.float32)
        for i in range(0,1400,20):rms[i:i+6]=.0003
        guard={'voice':[[42,46]],'airflow':[[70,80]],'drinking':[[90,95]]}
        for mode in ('strict','relaxed'):
            for extra_voice in ([],[[105,107]]):
                cfg=settings({'mode':mode,'strict_min_section':5,'strict_dense_gap':0})
                plan=make_plan({'frame_samples':1024,'sample_rate':10240},{'levels':np.c_[rms,rms]},
                               {'spoken':extra_voice,'accepted':[]},[],cfg,Texture(),guard)
                self.assertGreater(plan['duration'],55)
                for a,b in plan['keep_frames']:
                    self.assertFalse(any(min(b*.1,d)>max(a*.1,c)+1e-7 for c,d in guard['voice']+guard['airflow']+guard['drinking']+extra_voice))

    def test_drink_break_requires_combined_evidence(self):
        rows=[record(0,6,**{'Chink, clink':.35}),record(2,8,Liquid=.4,Gargling=.2),record(4,10,Water=.3)]
        self.assertTrue(drinking_events(rows,[]))
        self.assertFalse(drinking_events([record(t,t+6,Water=.8) for t in range(0,20,2)],[]))
        self.assertFalse(drinking_events([record(0,6,Gargling=.7)],[]))

    def test_announced_drink_requires_audio_and_stops_at_returning_asmr(self):
        announcement=[{'end':2,'text':'물 좀 마실게요'}]
        rows=[record(2,8,Liquid=.10),{**record(6,12),'quiet':True},record(10,16,**{'Chewing, mastication':.7})]
        self.assertTrue(drinking_events(rows,announcement))
        self.assertFalse(drinking_events([record(2,8,**{'Chewing, mastication':.8})],announcement))
        self.assertEqual(extend_breaks([[2,8]],rows),[[2,12]])


if __name__=='__main__': unittest.main()
