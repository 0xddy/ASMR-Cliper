"""Do not turn spectral changes or unknown low-frequency sounds into deletions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from scipy.io import wavfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.common import settings
from asmrclip.exclusions import selected_exclusions
from asmrclip.planner import make_plan
from asmrclip.semantic import SoundMatcher, PROMPTS, confirm
from asmrclip.transitions import spectral_trace, cached_trace, candidates, probe_windows, decision, review_transitions, acoustic_veto


def audio(drop=True,seconds=4):
    rate=16000;n=rate*36
    rng=np.random.default_rng(17)
    low=.045*np.sin(np.arange(n)*2*np.pi*150/rate)
    hiss=rng.normal(0,.045,n)
    if drop:hiss[12*rate:round((12+seconds)*rate)]*=.0005
    return np.round((low+hiss)*32768).astype(np.int16)


def asmr(a,b,category='mouth'):
    return {'start':a,'end':b,'texture':.6,'breath':0.,'speech':0.,category:.6,'semantic':{category:.38,'other':.12}}


def residue(a,b):
    return {'start':a,'end':b,'texture':.01,'semantic':{'mouth':.12,'other':.36,'background':.36}}


class TransitionDetectionTests(unittest.TestCase):
    def test_relative_spectral_drop_and_recovery_propose_only_the_phase(self):
        result=candidates(spectral_trace(audio()))
        self.assertEqual(len(result),1)
        self.assertAlmostEqual(result[0]['start'],12,delta=.25)
        self.assertAlmostEqual(result[0]['end'],16,delta=.25)
        self.assertGreater(result[0]['high_band_drop_db'],7)
        self.assertLess(result[0]['centroid_inside_hz'],result[0]['centroid_before_hz']*.6)

    def test_volume_change_constant_bass_and_short_dip_do_not_prove_interruption(self):
        gain=audio(False).astype(np.float64)
        gain[12*16000:16*16000]*=.08
        self.assertEqual(candidates(spectral_trace(gain.astype(np.int16))),[])
        bass=(np.sin(np.arange(36*16000)*2*np.pi*130/16000)*3000).astype(np.int16)
        self.assertEqual(candidates(spectral_trace(bass)),[])
        self.assertEqual(candidates(spectral_trace(audio(seconds=.5))),[])
        self.assertEqual(candidates(spectral_trace(np.zeros(16000*36,np.int16))),[])

    def test_no_recovery_no_deletion_candidate(self):
        self.assertEqual(candidates(spectral_trace(audio(seconds=24))),[])

    def test_internal_probes_cover_phase_without_borrowing_neighboring_asmr(self):
        for length in (1.25,2,2.25,4,7.75,20):
            r={'start':12.,'end':12+length}
            before,after,*body=probe_windows(r,40)
            self.assertEqual(before[1],12)
            self.assertEqual(after[0],12+length)
            self.assertEqual(body[0][0],12)
            self.assertEqual(body[-1][1],12+length)
            self.assertTrue(all(12<=a<b<=12+length for a,b in body))
            self.assertTrue(all(right[0]<=left[1] for left,right in zip(body,body[1:])))

    def test_trace_cache_reused_and_pcm_file_change_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);pcm=audio();wavfile.write(folder/'analysis.wav',16000,pcm)
            expected=cached_trace(pcm,folder)
            with patch('asmrclip.transitions.spectral_trace',side_effect=AssertionError('recomputed')):
                np.testing.assert_array_equal(cached_trace(pcm,folder),expected)
            pcm=pcm[:-4000];wavfile.write(folder/'analysis.wav',16000,pcm)
            self.assertEqual(len(cached_trace(pcm,folder)),len(expected)-1)


class TransitionPolicyTests(unittest.TestCase):
    def test_confirmed_residue_requires_positive_context_and_negative_body(self):
        probes=[asmr(8,12),asmr(16,20),residue(12,14),residue(13,15),residue(14,16)]
        self.assertEqual(decision(probes,{})[0],'remove')
        for index in (0,1,2,3,4):
            changed=list(probes);changed[index]={'start':probes[index]['start'],'end':probes[index]['end']}
            self.assertEqual(decision(changed,{})[0],'uncertain')
        changed=list(probes);changed[2]=residue(12,14)
        changed[2]['semantic']={'mouth':.24,'other':.4}
        self.assertEqual(decision(changed,{})[0],'uncertain')

    def test_intentional_low_frequency_asmr_and_soft_laugh_are_protected(self):
        for kind in ('mouth','surface','heartbeat','tapping','soft_laugh'):
            probes=[asmr(8,12),asmr(16,20),asmr(12,14,kind),residue(14,16)]
            self.assertEqual(decision(probes,{})[0],'keep_asmr')

    def test_unknown_body_or_unknown_surroundings_are_not_deleted(self):
        probes=[asmr(8,12),asmr(16,20),{'start':12,'end':16,'quiet':True}]
        self.assertEqual(decision(probes,{})[0],'uncertain')
        probes=[residue(8,12),residue(16,20),residue(12,16)]
        self.assertEqual(decision(probes,{})[0],'uncertain')

    def test_all_modes_and_replanning_respect_only_confirmed_intervals(self):
        class Texture:
            def windows(self,windows):return [asmr(a,b) for a,b in windows]
        rms=np.full(1600,.05,np.float32)
        for i in range(0,1600,20):rms[i:i+6]=.0003
        report={'transitions':[[80,85]],'extraction':{'intervals':[[0,160]]},
                'transition_review':{'candidates':[{'start':50,'end':55,'decision':'uncertain'}]}}
        for mode in ('strict','relaxed','extract'):
            cfg=settings({'mode':mode,'strict_min_section':5,'strict_dense_gap':0})
            self.assertEqual(selected_exclusions(report,cfg),[[80,85]])
            for speech in ([],[[110,113]]):
                plan=make_plan({'frame_samples':1024,'sample_rate':10240},{'levels':np.c_[rms,rms]},
                    {'spoken':speech,'accepted':[]},[],cfg,Texture(),report)
                self.assertTrue(plan['keep_frames'])
                self.assertFalse(any(min(b*.1,85)>max(a*.1,80) for a,b in plan['keep_frames']))
                self.assertTrue(any(a*.1<=50 and b*.1>=55 for a,b in plan['keep_frames']))

    def test_missing_model_preserves_candidate_and_reports_unconfirmed(self):
        with tempfile.TemporaryDirectory() as folder:
            matcher=Mock();matcher.available.return_value=False
            classifier=Mock()
            removed,report=review_transitions({},audio(),Path(folder),classifier,matcher,{'spoken':[]},[],{},36)
            self.assertEqual(removed,[]);self.assertEqual(report['status'],'needs_model')
            self.assertEqual(len(report['candidates']),1)
            matcher.score.assert_not_called();classifier.windows.assert_not_called()

    def test_integration_only_removes_model_confirmed_phase(self):
        with tempfile.TemporaryDirectory() as folder:
            classifier=Mock();classifier.windows.side_effect=lambda windows:[{k:v for k,v in (asmr(a,b) if a<12 or a>=16 else residue(a,b)).items() if k!='semantic'} for a,b in windows]
            matcher=Mock();matcher.available.return_value=True
            matcher.score.side_effect=lambda records,progress,**kwargs:[asmr(r['start'],r['end']) if i<2 else residue(r['start'],r['end']) for i,r in enumerate(records)]
            removed,report=review_transitions({},audio(),Path(folder),classifier,matcher,{'spoken':[]},[],{},36)
            self.assertEqual(removed,[[12.,16.]])
            self.assertEqual(report['candidates'][0]['decision'],'remove')
            classifier.close.assert_called_once()
            saved=json.loads((Path(folder)/'transition-review.json').read_text(encoding='utf8'))
            self.assertEqual(saved,report)
            removed,report=review_transitions({},audio(),Path(folder),classifier,matcher,{'spoken':[[12,16]]},[],{},36)
            self.assertEqual(removed,[]);self.assertEqual(report['candidates'],[])

    def test_acoustic_prefilter_only_skips_candidates_that_cannot_be_removed(self):
        for which in ('mouth','heartbeat','tapping','texture','soft_laugh','speech'):
            records=[asmr(8,12),asmr(16,20),residue(12,16)]
            if which=='speech':records[0]['speech']=.8
            else:records[2][which]=.4
            self.assertIsNotNone(acoustic_veto(records))
            self.assertNotEqual(decision(records,{})[0],'remove')
        self.assertIsNone(acoustic_veto([asmr(8,12),asmr(16,20),residue(12,16)]))

    def test_breathing_or_music_matching_is_not_background_confirmation(self):
        for semantic in ({'mouth':.1,'other':.5}, {'mouth':.1,'other':.6,'background':.3},
                         {'mouth':.1,'other':.35,'background':.35,'speech':.5}):
            records=[asmr(8,12),asmr(16,20),{**residue(12,16),'semantic':semantic}]
            self.assertEqual(decision(records,{})[0],'uncertain')


class SharedSoundMatcherTests(unittest.TestCase):
    def test_legacy_semantic_cache_backfills_only_requested_background_probes(self):
        pcm=np.zeros(10*16000,np.int16)
        records=[{'start':0.,'end':2.},{'start':4.,'end':6.}]
        scores=[.15]*sum(map(len,PROMPTS.values()))
        with tempfile.TemporaryDirectory() as folder, \
             patch('asmrclip.semantic.model_signature',return_value=[['model',1,1]]), \
             patch('asmrclip.neural_client.NeuralClient') as client:
            client.return_value.request.side_effect=lambda request,packed:[scores for _ in request['clips']]
            with SoundMatcher({},pcm,Path(folder)) as matcher:matcher.score(records)
            path=Path(folder)/'semantic-cache.json';cached=json.loads(path.read_text(encoding='utf8'))
            for value in cached['windows'].values():value.pop('background')
            path.write_text(json.dumps(cached),encoding='utf8')
            client.reset_mock()
            with SoundMatcher({},pcm,Path(folder)) as matcher:
                matcher.score(records)
                client.assert_not_called()
                updated=matcher.score(records[:1],required_keys=('background',))
            self.assertIn('background',updated[0]['semantic'])
            self.assertEqual(len(client.return_value.request.call_args.args[0]['clips']),1)
            saved=json.loads(path.read_text(encoding='utf8'))['windows']
            self.assertIn('background',saved['0.00000:2.00000'])
            self.assertNotIn('background',saved['4.00000:6.00000'])

    def test_sparse_packing_matches_original_samples_and_reuses_worker_and_cache(self):
        pcm=np.arange(16000*1000,dtype=np.int16)
        # Fractional-sample edges exercise rounding of both the original batch
        # bounds and the per-clip bounds inside it, including an end clamp.
        records=[{'start':1.00003125,'end':3.237},{'start':903.273,'end':906.37203125}]
        scores=[.15]*sum(map(len,PROMPTS.values()))
        with tempfile.TemporaryDirectory() as folder, \
             patch('asmrclip.semantic.model_signature',return_value=[['model',1,1]]), \
             patch('asmrclip.neural_client.NeuralClient') as client:
            def classify(request,packed):
                self.assertLess(len(packed),16000*6)
                base=min(r['start'] for r in records)
                original=pcm[round(base*16000):round(max(r['end'] for r in records)*16000)].astype(np.float32)/32768
                for r,(a,b) in zip(records,request['clips']):
                    expected=original[round((r['start']-base)*16000):round((r['end']-base)*16000)]
                    np.testing.assert_array_equal(packed[round(a*16000):round(b*16000)],expected)
                return [scores,scores]
            client.return_value.request.side_effect=classify
            with SoundMatcher({},pcm,Path(folder)) as matcher:
                first=matcher.score(records)
                self.assertEqual(first,matcher.score(records))
                self.assertEqual(client.return_value.request.call_count,1)
            client.return_value.close.assert_called_once()
            with SoundMatcher({},pcm,Path(folder)) as matcher:
                self.assertEqual(first,matcher.score(records))
            client.assert_called_once()

    def test_v4_uses_shared_matcher_but_still_excludes_quiet_and_blocked_windows(self):
        classifier=Mock();classifier.windows.side_effect=lambda spans:[{**asmr(a,b),'quiet':a==0} for a,b in spans]
        matcher=Mock();matcher.score.side_effect=lambda records,progress:records
        with tempfile.TemporaryDirectory() as folder:
            report=confirm({},np.zeros(40*16000,np.int16),Path(folder),classifier,{'spoken':[]},[],
                {'transitions':[[20,25]]},40,matcher)
        matcher.close.assert_not_called()
        scored=matcher.score.call_args.args[0]
        self.assertTrue(scored)
        self.assertFalse(any(r.get('quiet') for r in scored))
        self.assertFalse(any(min(r['end'],25)>max(r['start'],20) for r in scored))
        self.assertFalse(any(min(b,25)>max(a,20) for a,b in report['intervals']))


if __name__=='__main__':unittest.main()
