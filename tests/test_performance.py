"""Performance changes must preserve inputs, coverage and cache boundaries."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings

import numpy as np
from scipy.io import wavfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.acoustic_features import ast_features
from asmrclip.classifier import Classifier
from asmrclip.common import read_json, save_json
from asmrclip.recognition import WhisperRecognizer, QwenRecognizer
from asmrclip.reviewer import Reviewer, review_windows


class AcousticPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from transformers import ASTFeatureExtractor
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            cls.feature=ASTFeatureExtractor()

    def test_vectorized_features_match_reference_and_do_not_change_pcm(self):
        from transformers.audio_utils import spectrogram
        f=self.feature
        rng=np.random.default_rng(2026)
        samples=[rng.normal(.2,.1,n).astype(np.float32)
                 for n in (400,401,559,560,6399,32000,96000,160000,200000)]
        samples += [np.zeros(16000,np.float32),np.full(16000,.3,np.float32),
                    np.sin(np.arange(16000)*.07).astype(np.float32)]
        for audio in samples:
            with self.subTest(length=len(audio),dc=float(audio.mean())):
                original=audio.copy()
                fb=spectrogram(audio,f.window,frame_length=400,hop_length=160,fft_length=512,
                    power=2.,center=False,preemphasis=.97,mel_filters=f.mel_filters,
                    log_mel='log',mel_floor=1.192092955078125e-7,remove_dc_offset=True).T
                reference=f.normalize(np.pad(fb,((0,max(0,1024-len(fb))),(0,0)))[:1024]).astype(np.float32)
                np.testing.assert_allclose(ast_features(audio,f),reference,rtol=0,atol=2e-6)
                np.testing.assert_array_equal(audio,original)

    def classifier(self,folder):
        classifier=Classifier.__new__(Classifier)
        classifier.pcm=np.full(16000*10,4096,np.int16)
        classifier.feature=self.feature
        classifier.cached={}
        classifier.path=Path(folder)/'acoustic-cache.json'
        classifier.identity='test-model'
        classifier.labels={'0':'Speech'}
        classifier.session=Mock()
        classifier.session.run.side_effect=lambda _,inputs:[np.full((len(inputs['input_values']),1),-6,np.float32)]
        return classifier

    def test_cache_hits_do_not_rewrite_or_infer_and_requests_keep_order(self):
        with tempfile.TemporaryDirectory() as folder:
            c=self.classifier(folder)
            windows=[(0,2),(1,3),(0,2)]
            expected=c.windows(windows)
            self.assertEqual(c.session.run.call_count,1)
            self.assertEqual(len(c.cached),2)
            c.path.chmod(0o444)
            with patch('asmrclip.classifier.save_json',side_effect=AssertionError('unnecessary write')):
                self.assertEqual(c.windows(windows),expected)
                self.assertEqual(c.windows([]),[])
            c.path.chmod(0o666)
            self.assertEqual(c.session.run.call_count,1)

    def test_completed_acoustic_windows_survive_later_batch_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            c=self.classifier(folder)
            windows=[(i*.02,i*.02+2) for i in range(168)]
            count=0
            def run(_,inputs):
                nonlocal count
                count+=1
                if count==21:raise RuntimeError('interrupted')
                return [np.full((len(inputs['input_values']),1),-6,np.float32)]
            c.session.run.side_effect=run
            with self.assertRaisesRegex(RuntimeError,'interrupted'):c.windows(windows)
            checkpoint=read_json(c.path)
            self.assertEqual(len(checkpoint['windows']),160)
            resumed=self.classifier(folder)
            resumed.cached=checkpoint['windows']
            rows=resumed.windows(windows)
            self.assertEqual(len(rows),168)
            self.assertEqual(resumed.session.run.call_count,1)
            self.assertEqual(len(read_json(c.path)['windows']),168)


class RecognizerReuseTests(unittest.TestCase):
    def test_completed_scan_reuses_cache_without_loading_either_model(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            wavfile.write(folder/'analysis.wav',16000,np.zeros(32000,np.int16))
            (folder/'model.bin').write_text('model')
            cfg={'device':'cpu','language':'auto','whisper_model':str(folder),'speech_model':'whisper-large-v3'}
            for cls in (WhisperRecognizer,QwenRecognizer):
                with self.subTest(backend=cls.__name__), \
                     patch('faster_whisper.WhisperModel',side_effect=AssertionError('unnecessary load')), \
                     patch('asmrclip.neural_client.NeuralClient',side_effect=AssertionError('unnecessary worker')):
                    model=cls(cfg)
                    # Qwen's model catalog is independent of the tiny fixture.
                    with patch.object(model,'cache_identity',return_value=['test',cls.__name__]):
                        token=hashlib.sha256(json.dumps(model.cache_identity(cfg)).encode()).hexdigest()[:12]
                        segment={'start':.1,'end':1.,'text':'hello there','avg_logprob':-.2,
                                 'words':[{'word':'hello there','start':.1,'end':1.,'probability':.9}]}
                        row={'offset':0.,'language':'en','vad':[[0,2]],'vad_segments':[segment],'continuous_segments':[]}
                        (folder/f'speech-{token}.jsonl').write_text(json.dumps(row)+'\n',encoding='utf8')
                        result=model.scan(folder/'analysis.wav',cfg,folder)
                    self.assertEqual(result['spoken'],[[.1,1.]])
                    self.assertEqual(result['language'],'en')
                    model.close()

    def test_whisper_loads_on_demand_reuses_model_and_reloads_after_close(self):
        cfg={'device':'cpu','language':'en','whisper_model':'test','speech_model':'whisper-large-v3'}
        pipe=Mock();pipe.transcribe.return_value=([],None)
        with patch('faster_whisper.WhisperModel') as factory,patch('faster_whisper.BatchedInferencePipeline',return_value=pipe):
            r=WhisperRecognizer(cfg)
            self.assertEqual(r.transcribe(np.zeros(16000,np.float32),[]),[])
            factory.assert_not_called()
            for _ in range(2):r.transcribe(np.zeros(16000,np.float32),[{'start':0,'end':1}])
            factory.assert_called_once()
            self.assertEqual(pipe.transcribe.call_args.kwargs['beam_size'],5)
            self.assertEqual(pipe.transcribe.call_args.kwargs['batch_size'],4)
            r.close()
            r.transcribe(np.zeros(16000,np.float32),[{'start':0,'end':1}])
            self.assertEqual(factory.call_count,2)

    def test_qwen_starts_worker_only_for_uncached_inference(self):
        with patch('asmrclip.neural_client.NeuralClient') as factory:
            factory.return_value.request.return_value=[]
            r=QwenRecognizer({'device':'cpu','language':'ko'})
            r.transcribe(np.zeros(16000,np.float32),[])
            factory.assert_not_called()
            for _ in range(2):r.transcribe(np.zeros(16000,np.float32),[{'start':0,'end':1}])
            factory.assert_called_once()
            self.assertEqual(factory.return_value.request.call_args.args[0]['language'],'Korean')
            r.close()
            factory.return_value.close.assert_called_once()


class ReviewChunkReuseTests(unittest.TestCase):
    def reviewer(self,folder,recognizer):
        r=Reviewer.__new__(Reviewer)
        r.cache=Path(folder)/'reviews';r.path=str(Path(folder)/'model')
        r.cfg={'device':'cpu','language':'en','speech_model':'whisper-large-v3'}
        r.language='en';r.recognizer=recognizer
        return r

    def test_changed_pcm_or_model_identity_requires_full_recheck(self):
        with tempfile.TemporaryDirectory() as folder:
            model=Mock();model.transcribe.return_value=[]
            r=self.reviewer(folder,model)
            pcm=np.arange(40*16000,dtype=np.int16)
            first=r.inspect_chunk(pcm,'model-language-device-A')
            self.assertFalse(first[2])
            self.assertEqual(model.transcribe.call_count,2)
            second=r.inspect_chunk(pcm,'model-language-device-A')
            self.assertEqual(first[:2],second[:2]);self.assertTrue(second[2])
            self.assertEqual(model.transcribe.call_count,2)
            pcm[32000]+=1
            self.assertFalse(r.inspect_chunk(pcm,'model-language-device-A')[2])
            self.assertFalse(r.inspect_chunk(pcm,'model-language-device-B')[2])
            self.assertEqual(model.transcribe.call_count,6)

    def test_failed_boundary_pass_is_not_cached_as_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            model=Mock();model.transcribe.side_effect=[[],RuntimeError('boundary failed')]
            r=self.reviewer(folder,model)
            with self.assertRaisesRegex(RuntimeError,'boundary failed'):
                r.inspect_chunk(np.zeros(40*16000,np.int16),'model')
            self.assertEqual(list(r.cache.rglob('*.json')),[])

    def test_resumed_review_preserves_findings_times_and_full_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);(folder/'model').mkdir();(folder/'model/model.bin').write_text('model')
            pcm=np.random.default_rng(42).integers(-1000,1000,330*16000,dtype=np.int16)
            segment={'start':1,'end':2,'text':'hello there','avg_logprob':-.2,
                     'words':[{'word':'hello','start':1,'end':2,'probability':.9}]}
            model=Mock();model.transcribe.side_effect=[[segment],[],RuntimeError('cancelled')]
            r=self.reviewer(folder,model)
            report={'payload_sha256':'candidate-original'}
            with patch('asmrclip.reviewer.decode_review_audio',return_value=pcm):
                with self.assertRaisesRegex(RuntimeError,'cancelled'):r.inspect('candidate',report)
            self.assertEqual(len(list((r.cache/'chunks').glob('*.json'))),1)
            model=Mock();model.transcribe.side_effect=[[segment],[]]
            r=self.reviewer(folder,model)
            with patch('asmrclip.reviewer.decode_review_audio',return_value=pcm):
                resumed=r.inspect('candidate',report)
            self.assertEqual(model.transcribe.call_count,2)
            self.assertEqual(resumed['chunks_reused'],1)
            self.assertEqual([(s['start'],s['end']) for s in resumed['findings']],[(1,2),(301,302)])
            self.assertEqual(resumed['windows_checked'],sum(len(g) for seconds in (302,30) for g in review_windows(seconds)))
            # A new candidate can reuse unchanged chunks, without trusting an
            # old whole-result cache. A single changed sample forces its chunk.
            pcm[-1]+=1
            model=Mock();model.transcribe.side_effect=[[segment],[]]
            r=self.reviewer(folder,model)
            with patch('asmrclip.reviewer.decode_review_audio',return_value=pcm):
                changed=r.inspect('candidate',{'payload_sha256':'candidate-changed'})
            self.assertEqual(changed['findings'],resumed['findings'])
            self.assertEqual(changed['chunks_reused'],1)
            self.assertEqual(model.transcribe.call_count,2)


if __name__=='__main__':unittest.main()
