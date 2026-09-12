import contextlib
import dataclasses
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from scipy.io import wavfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from asmrclip import progress
from asmrclip.common import event
from asmrclip.interaction import confirm_language
from asmrclip.recognition import WhisperRecognizer, QwenRecognizer
from asmrclip.reviewer import Reviewer, review_windows


class ProgressTests(unittest.TestCase):
    def capture(self, function):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=progress.tracked(function)()
        return result,[json.loads(line) for line in output.getvalue().splitlines()]

    def test_review_rounds_coverage_and_logs_are_separate(self):
        def work():
            progress.phase(5,round_index=2,round_limit=3)
            with progress.scope('音频块 2/4',.25,.5):
                with progress.scope('边界检查 2/2',.5,1):
                    progress.advance(2,4)
                    event('log','Qwen 定位窗口',99)
            progress.phase(5,round_index=3,round_limit=3)
            progress.phase(6,'复核通过')
        _,rows=self.capture(work)
        values=[r['task_progress'] for r in rows if 'task_progress' in r]
        counted=next(r for r in values if '窗口 2/4' in r['detail'])
        self.assertEqual(counted['percent'],43.8)
        self.assertEqual((counted['round'],counted['round_limit']),(2,3))
        self.assertNotIn('task_progress',next(r for r in rows if r['type']=='log'))
        self.assertIsNone(values[-2]['percent'])
        self.assertEqual(values[-1]['stage'],6)
        self.assertEqual(values[-1]['round'],0)
        self.assertIsNone(progress._current.get())

    def test_failed_scope_does_not_report_completion(self):
        def work():
            progress.phase(5,round_index=1,round_limit=3)
            try:
                with progress.scope('边界检查'):
                    progress.advance(1,10)
                    raise RuntimeError('interrupted')
            except RuntimeError:
                event('error','interrupted')
        _,rows=self.capture(work)
        self.assertEqual([r['task_progress']['percent'] for r in rows if 'task_progress' in r][-1],10.)

    def test_legacy_ffmpeg_progress_is_bounded_to_its_phase(self):
        def work():
            progress.phase(1,legacy=(0,18))
            event('progress','读取音轨',9)
            progress.phase(5,round_index=1,round_limit=3)
            event('progress','旧版复核百分比',98)
        _,rows=self.capture(work)
        self.assertEqual(rows[1]['task_progress']['percent'],50.)
        self.assertNotIn('task_progress',rows[-1])

    def test_review_chunk_cache_preserves_round_and_full_boundary_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Reviewer.__new__(Reviewer);r.cache=Path(temp);r.cfg={}
            r.recognizer=Mock();r.recognizer.transcribe.return_value=[]
            pcm=np.zeros(40*16000,np.int16)
            def work():
                progress.phase(5,round_index=1,round_limit=3)
                with progress.scope('音频块 1/1'):
                    first=r.inspect_chunk(pcm,'test-model')
                progress.phase(5,round_index=2,round_limit=3)
                with progress.scope('音频块 1/1'):
                    second=r.inspect_chunk(pcm,'test-model')
                return first,second
            (first,second),rows=self.capture(work)
            self.assertFalse(first[2]);self.assertTrue(second[2])
            self.assertEqual(r.recognizer.transcribe.call_count,2)
            self.assertEqual([c.args[1] for c in r.recognizer.transcribe.call_args_list],review_windows(40))
            values=[r['task_progress'] for r in rows if 'task_progress' in r]
            self.assertTrue(any('全段检查 1/2' in r['detail'] for r in values))
            self.assertTrue(any('边界检查 2/2' in r['detail'] for r in values))
            self.assertEqual(values[-1]['percent'],100)
            self.assertEqual(values[-1]['round'],2)

    def test_neural_progress_keeps_parent_round_context(self):
        from asmrclip.neural_client import NeuralClient
        client=NeuralClient.__new__(NeuralClient)
        client.process=Mock(stdin=io.StringIO(),stdout=iter([
            '{"type":"work_progress","done":3,"total":10}\n',
            '{"type":"response","ok":true,"result":[]}\n']))
        def work():
            progress.phase(5,round_index=2,round_limit=4)
            with progress.scope('音频块 3/8',.25,.375):
                return client.request({'op':'transcribe'})
        try:
            result,rows=self.capture(work)
        finally:
            client.process=None
        self.assertEqual(result,[])
        counted=next(r['task_progress'] for r in rows if '窗口 3/10' in r.get('task_progress',{}).get('detail',''))
        self.assertEqual(counted['round'],2)
        self.assertEqual(counted['percent'],28.7)

    def test_whisper_counts_past_windows_not_transcript_segments(self):
        @dataclasses.dataclass
        class Segment:
            start: float
            end: float
            words: list
        model=WhisperRecognizer({'language':'en','speech_model':'whisper-large-v3'})
        model.pipe=Mock();model.pipe.transcribe.return_value=([Segment(1,2,[]),Segment(2,3,[]),Segment(29,30,[])],None)
        clips=[{'start':0,'end':28},{'start':28,'end':40}]
        def work():
            progress.phase(2)
            return model.transcribe(np.zeros(640000,np.float32),clips,10)
        result,rows=self.capture(work)
        values=[r['task_progress'] for r in rows if 'task_progress' in r]
        self.assertEqual([r['percent'] for r in values],[None,0.,50.,100.])
        self.assertEqual([s['start'] for s in result],[11,12,39])
        self.assertEqual(model.pipe.transcribe.call_count,1)
        self.assertEqual(model.pipe.transcribe.call_args.kwargs['clip_timestamps'],clips)
        self.assertEqual(model.pipe.transcribe.call_args.kwargs['batch_size'],4)


class LanguageConfirmationTests(unittest.TestCase):
    def test_requires_an_explicit_valid_reply(self):
        for reply in ('','{}','not json','{"type":"language_confirmed","language":"auto"}'):
            with self.subTest(reply=reply),patch('sys.stdin',io.StringIO(reply)),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):confirm_language('ko')
        with patch('sys.stdin',io.StringIO('{"type":"language_confirmed","language":"ja"}\n')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(confirm_language('ko'),'ja')
        prompt=json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(prompt['type'],'language_confirmation')
        self.assertEqual(prompt['detected'],'ko')

    def test_language_correction_isolates_transcripts_and_resumes_same_task(self):
        for cls in (WhisperRecognizer,QwenRecognizer):
            with self.subTest(backend=cls.__name__),tempfile.TemporaryDirectory() as folder:
                folder=Path(folder);wavfile.write(folder/'analysis.wav',16000,np.zeros(32000,np.int16))
                cfg={'language':'auto','device':'cpu','confirm_detected_language':True}
                model=cls(cfg)
                identity=lambda cfg:['test-model',cfg['language']]
                token=hashlib.sha256(json.dumps(identity(cfg)).encode()).hexdigest()[:12]
                # This transcript must not leak into the corrected-language task.
                wrong={'start':0,'end':1,'text':'wrong old transcript','avg_logprob':0,
                       'words':[{'start':0,'end':1,'word':'wrong','probability':1.}]}
                (folder/f'speech-{token}.jsonl').write_text(json.dumps({'offset':0.,'language':'ko','vad':[[0,1]],'vad_segments':[wrong],'continuous_segments':[]})+'\n')
                with patch.object(model,'cache_identity',side_effect=identity),patch.object(model,'transcribe',return_value=[]) as transcribe, \
                     patch('faster_whisper.vad.get_speech_timestamps',return_value=[]), \
                     patch('sys.stdin',io.StringIO('{"type":"language_confirmed","language":"ja"}\n')),contextlib.redirect_stdout(io.StringIO()) as output:
                    result=model.scan(folder/'analysis.wav',cfg,folder)
                self.assertEqual(result['language'],'ja');self.assertEqual(result['spoken'],[])
                self.assertEqual(transcribe.call_count,2)
                self.assertEqual(sum(json.loads(line)['type']=='language_confirmation' for line in output.getvalue().splitlines()),1)
                # Manual Japanese reuses only the now-completed Japanese cache.
                manual={**cfg,'language':'ja'};model=cls(manual)
                with patch.object(model,'cache_identity',side_effect=identity),patch.object(model,'transcribe',side_effect=AssertionError('unnecessary inference')), \
                     patch('asmrclip.interaction.confirm_language',side_effect=AssertionError('manual language must not prompt')),contextlib.redirect_stdout(io.StringIO()):
                    again=model.scan(folder/'analysis.wav',manual,folder)
                self.assertEqual(again['language'],'ja');self.assertEqual(again['spoken'],[])


if __name__=='__main__':unittest.main()
