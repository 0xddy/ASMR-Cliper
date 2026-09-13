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

    def test_normal_or_mixed_speech_cannot_be_cancelled_by_asmr_context(self):
        normal={'semantic':{'normal_speech':.7,'whisper_asmr':.4,'mouth':.3}}
        self.assertEqual(decision(self.whisper,[self.whisper,normal],True),'speech')
        self.assertEqual(decision({**self.whisper,'voiced':.6},[self.whisper],True),'speech')
        self.assertEqual(decision(self.mouth,[self.mouth,normal],True),'speech')
        self.assertEqual(decision({**self.mouth,'voiced':.6},[self.mouth],True),'speech')

    def test_sound_windows_cache_actual_pcm_and_reuse_identical_audio(self):
        from asmrclip.semantic import PROMPTS
        raw={'model':'Qwen3-ASR-1.7B','status':'speech_found','candidate_payload_sha256':'a'*64,
             'findings':[{'start':5,'end':5.5,'text':'possible words'}]}
        original=copy.deepcopy(raw)
        # Identical payloads can have different PTS and therefore move samples
        # inside the same window. Keep length and total signal energy equal.
        first=np.zeros(20*16000,np.int16);first[:8*16000]=1000
        shifted=np.zeros_like(first);shifted[8*16000:16*16000]=1000
        classifier=Mock()
        classifier.windows.side_effect=lambda spans:[{'start':a,'end':b,'whisper':.02,'voiced':.02} for a,b in spans]
        client=Mock()
        def classify(req,audio):
            scores=[]
            for a,b in req['clips']:
                category='mouth' if audio[round(a*16000):round(b*16000)].mean()>.01 else 'normal_speech'
                scores.append([.65 if key==category else .1 for key,items in PROMPTS.items() for _ in items])
            return scores
        client.request.side_effect=classify
        with tempfile.TemporaryDirectory() as directory, \
                patch('asmrclip.reviewer.decode_review_audio',side_effect=[first,shifted,shifted.copy()]), \
                patch('asmrclip.classifier.Classifier',return_value=classifier) as acoustic, \
                patch('asmrclip.semantic.SoundMatcher.available',return_value=True), \
                patch('asmrclip.semantic.model_signature',return_value=['test-model']), \
                patch('asmrclip.neural_client.NeuralClient',return_value=client):
            cfg={'cache_dir':directory,'_task_cache':directory,'keep_whisper':False}
            old=verify('first.mka',raw,cfg)
            after_first=client.request.call_count
            changed=verify('shifted.mka',raw,cfg)
            after_changed=client.request.call_count
            reused=verify('shifted-again.mka',raw,cfg)
            paths=[call.args[2] for call in acoustic.call_args_list]
            self.assertNotEqual(paths[0],paths[1])
            self.assertEqual(paths[1],paths[2])
            self.assertGreater(after_changed,after_first)
            self.assertEqual(client.request.call_count,after_changed)
        self.assertEqual(raw,original)
        self.assertTrue(old['findings'][0]['review_only'])
        self.assertEqual(old['findings'][0]['start'],5)
        self.assertEqual(old['allowed_whisper'],[])
        self.assertFalse(changed['findings'][0].get('review_only',False))
        self.assertEqual(reused['findings'],changed['findings'])


if __name__=='__main__':unittest.main()
