"""Drink gestures need temporal evidence independent of generic liquid labels."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import numpy as np

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.common import settings
from asmrclip.drinking import candidate,drink,probe_rows,review_drinking,sequences
from asmrclip.exclusions import drinking_events
from asmrclip.model_catalog import required_components


def row(start,kind='drink',**scores):
    kinds={'drink':{'drinking':.64,'swallow':.61,'mouth':.43},
           'cap':{'bottle_cap':.62,'handling':.38,'mouth':.25},
           'mouth':{'mouth':.67,'drinking':.48,'swallow':.46},
           'weak':{'mouth':.44,'drinking':.47,'swallow':.44},
           'tapping':{'tapping':.64,'bottle_cap':.56,'drinking':.28,'swallow':.22},
           'water':{'water':.7,'drinking':.61,'swallow':.51},
           'unknown':{}}
    return {'start':float(start),'end':float(start+3),'texture':.5,'liquid':.01,'container':.01,
            'semantic':{**kinds[kind],**scores}}


class DrinkingTests(unittest.TestCase):
    def test_listener_confirmed_break_with_weak_liquid_labels(self):
        rows=json.loads((ROOT/'tests/fixtures/drinking-scores.json').read_text('utf8'))['rows']
        self.assertEqual(drinking_events(rows,[]),[])
        intervals,decisions=sequences(rows)
        self.assertEqual(len(intervals),1)
        a,b=intervals[0]
        self.assertLessEqual(a,11);self.assertGreaterEqual(b,18)
        self.assertGreaterEqual(a,9);self.assertLessEqual(b,21)
        self.assertEqual(decisions[0]['decision'],'remove')

    def test_continuous_drinking_does_not_require_glass_or_generic_liquid_score(self):
        rows=[row(t) for t in range(3,7)]
        self.assertEqual(drinking_events(rows,[]),[])
        found,report=sequences(rows)
        self.assertEqual(found,[[3.,9.]])
        self.assertEqual(report[0]['decision'],'remove')
        self.assertFalse(report[0]['cap_before'])

    def test_cap_followed_by_swallow_can_confirm_single_drink_window(self):
        found,report=sequences([row(0,'cap'),row(1,'cap'),row(2,'weak'),row(3)])
        self.assertEqual(found,[[0.,6.]])
        self.assertTrue(report[0]['cap_before'])

    def test_normal_asmr_water_and_isolated_cap_or_swallow_are_not_removed(self):
        for kind in ('mouth','water','cap','tapping','weak','unknown'):
            with self.subTest(kind=kind):self.assertEqual(sequences([row(t,kind) for t in range(10)])[0],[])
        found,report=sequences([row(0,'mouth'),row(1),row(2,'mouth')])
        self.assertEqual(found,[]);self.assertEqual(report[0]['decision'],'uncertain')

    def test_discovery_with_mixed_context_cannot_itself_delete(self):
        mixed=row(0,'weak',mouth=.6,drinking=.55,swallow=.50)
        self.assertTrue(candidate(mixed));self.assertFalse(drink(mixed))
        self.assertEqual(sequences([mixed])[0],[])

    def test_separate_swallow_hits_do_not_bridge_restored_asmr_or_unknown(self):
        for middle in ('mouth','unknown'):
            self.assertEqual(sequences([row(0),row(1,middle),row(2)])[0],[])

    def test_unknown_gap_wrong_order_distant_cap_and_asmr_recovery_do_not_pair(self):
        cases=[[row(0,'cap'),row(20)], [row(0),row(1,'cap')],
               [row(0,'cap'),row(4)],
               [row(0,'cap'),row(1,'unknown'),row(2,'weak'),row(3)],
               [row(0,'cap'),row(1,'mouth'),row(2,'weak'),row(3)]]
        for rows in cases:self.assertEqual(sequences(rows)[0],[])

    def test_boundaries_follow_related_actions_and_stop_on_asmr_or_unknown(self):
        rows=[row(0,'mouth'),row(1,'cap'),row(2,'weak'),row(3),row(4),row(5,'cap'),row(6,'mouth')]
        self.assertEqual(sequences(rows)[0],[[1.,8.]])
        rows[-1]=row(6,'unknown')
        self.assertEqual(sequences(rows)[0],[[1.,8.]])

    def test_quiet_and_short_tail_never_become_model_evidence(self):
        pcm=np.zeros(16000*5,np.int16)
        rows=probe_rows(pcm,[[0,5]],3,1)
        self.assertTrue(all(r['quiet'] for r in rows))
        self.assertTrue(all(r['end']-r['start']>=2 for r in rows))
        with tempfile.TemporaryDirectory() as folder:
            matcher=Mock();matcher.available.side_effect=AssertionError('quiet input queried model')
            found,report=review_drinking({},pcm,Path(folder),matcher,{'spoken':[]},[],{},5)
            self.assertEqual(found,[]);self.assertEqual(report['coarse_windows'],0)
            matcher.score.assert_not_called()

    def test_keep_setting_and_environment_requirements_agree_for_all_modes(self):
        for mode in ('strict','relaxed','extract'):
            for retain in (True,False):
                cfg=settings({'mode':mode,'keep_drinking':retain,'keep_whisper':False,'speech_model':'whisper-turbo','review_model_id':'whisper-large-v3'})
                needs=mode=='extract' or not retain
                self.assertEqual('clap' in required_components(cfg),needs)
                self.assertEqual('neural' in required_components(cfg),needs)
                with tempfile.TemporaryDirectory() as folder:
                    matcher=Mock();matcher.available.return_value=False
                    args=(cfg,np.ones(16000*4,np.int16)*2000,Path(folder),matcher,{'spoken':[]},[],{},4)
                    if needs:
                        with self.assertRaisesRegex(RuntimeError,'CLAP'):review_drinking(*args)
                    else:
                        self.assertEqual(review_drinking(*args)[1]['status'],'retained_by_setting')
                        matcher.score.assert_not_called()

    def test_review_isolates_speech_and_music_and_saves_evidence(self):
        pcm=np.ones(16000*50,np.int16)*2000
        def score(records,progress=None,required_keys=()):
            self.assertTrue({'drinking','swallow','bottle_cap'}<=set(required_keys))
            return [{**r,'semantic':row(r['start'],'drink' if r['start']>=30 else 'cap')['semantic']} for r in records]
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
            folder=Path(folder);matcher=Mock();matcher.available.return_value=True;matcher.score.side_effect=score
            found,report=review_drinking({},pcm,folder,matcher,{'spoken':[[15,25]]},[[25,30]],{},50)
            self.assertTrue(found)
            self.assertTrue(all(a>=30 for a,b in found))
            self.assertTrue(all(r['start']>=30 for r in report['candidates']))
            for call in matcher.score.call_args_list:
                self.assertTrue(all(r['end']<=15 or r['start']>=30 for r in call.args[0]))
            saved=json.loads((folder/'drinking-review.json').read_text('utf8'))
            self.assertEqual(saved['removed'],found)

if __name__=='__main__':unittest.main()
