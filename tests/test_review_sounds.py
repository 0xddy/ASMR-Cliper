import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from asmrclip.review_sounds import decision,verify


class ReviewSoundTests(unittest.TestCase):
    whisper={'whisper':.3,'voiced':.02,'semantic':{'whisper_asmr':.65,'speech':.6,'normal_speech':.3,'mouth':.2}}
    mouth={'whisper':.02,'voiced':.02,'semantic':{'mouth':.65,'speech':.3,'normal_speech':.2,'other':.2}}

    def test_confirmed_whisper_honors_retention(self):
        self.assertEqual(decision(self.whisper,[self.whisper],True),'whisper')
        self.assertEqual(decision(self.whisper,[self.whisper],False),'speech')

    def test_mouth_conflict_is_listening_review_not_whisper_permission(self):
        self.assertEqual(decision(self.mouth,[self.mouth,self.mouth],True),'conflict')
        self.assertEqual(decision({**self.mouth,'voiced':.6},[self.mouth],True),'speech')

    def test_normal_or_mixed_speech_cannot_be_cancelled_by_asmr_context(self):
        normal={'semantic':{'normal_speech':.7,'whisper_asmr':.4,'mouth':.3}}
        self.assertEqual(decision(self.whisper,[self.whisper,normal],True),'speech')
        self.assertEqual(decision({**self.whisper,'voiced':.6},[self.whisper],True),'speech')
        self.assertEqual(decision(self.mouth,[self.mouth,normal],True),'speech')

    def test_candidate_verification_keeps_raw_asr_unchanged(self):
        raw={'model':'Qwen3-ASR-1.7B','status':'speech_found','candidate_payload_sha256':'a'*64,
             'findings':[{'start':5,'end':5.5,'text':'possible words'}]}
        original=copy.deepcopy(raw)
        classifier=Mock();classifier.windows.side_effect=lambda spans:[{'start':a,'end':b,**self.mouth} for a,b in spans]
        matcher=Mock();matcher.available.return_value=True
        matcher.score.side_effect=lambda rows,*args:[{**r,**self.mouth} for r in rows]
        with tempfile.TemporaryDirectory() as directory,patch('asmrclip.reviewer.decode_review_audio',return_value=np.ones(20*16000,np.int16)), \
                patch('asmrclip.classifier.Classifier',return_value=classifier),patch('asmrclip.semantic.SoundMatcher') as factory:
            factory.return_value.__enter__.return_value=matcher
            result=verify('candidate.m4a',raw,{'cache_dir':directory,'_task_cache':directory})
        self.assertEqual(raw,original)
        self.assertTrue(result['findings'][0]['review_only'])
        self.assertEqual(result['findings'][0]['start'],5)
        self.assertEqual(result['allowed_whisper'],[])


if __name__=='__main__':unittest.main()
