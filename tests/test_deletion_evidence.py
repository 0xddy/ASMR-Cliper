import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from asmrclip.recognition import qwen_speech,qwen_alignment_issue,word_intervals
from asmrclip.planner import dense_conversations
from asmrclip.reviewer import mapped_output_to_source,output_to_source
from asmrclip.output_paths import export_names,windows_length


class DeletionEvidenceTests(unittest.TestCase):
    def segment(self,text,a,b,**extra):
        return {'backend':'qwen3-asr','text':text,'start':a,'end':b,
                'words':[{'word':text,'start':a,'end':b}],**extra}

    def test_collapsed_japanese_phrase_is_not_deletion_evidence(self):
        row=self.segment('今日は静かに話します',20,20.08)
        self.assertFalse(qwen_speech(row));self.assertTrue(qwen_alignment_issue(row))
        self.assertTrue(qwen_speech(self.segment('今日は静かに話します',20,22)))

    def test_missing_alignment_words_are_not_hidden(self):
        row=self.segment('お水を飲みます',10,12,alignment_total_words=8)
        self.assertFalse(qwen_speech(row))

    def test_single_syllable_cannot_delete_a_long_audio_window(self):
        row=self.segment('静かに話します',0,25)
        row['words']=[{'word':'た','start':0,'end':18},{'word':'話します','start':24,'end':25}]
        self.assertFalse(qwen_speech(row))

    def test_short_valid_words_still_count_as_speech(self):
        self.assertTrue(qwen_speech(self.segment('はい',10,10.4)))
        self.assertFalse(qwen_speech(self.segment('はい',10,10.08)))

    def test_sparse_interjections_cannot_chain_into_dense_chat(self):
        self.assertEqual(dense_conversations([[t,t+.3] for t in range(0,1200,28)]),[])
        self.assertTrue(dense_conversations([[t,t+5] for t in range(0,100,20)]))

    def test_word_timing_does_not_remove_whole_inference_window(self):
        row={'start':0,'end':28,'words':[{'start':1,'end':2},{'start':25,'end':26}]}
        self.assertEqual(word_intervals([row]),[[1,2],[25,26]])

    def test_confirmed_continuous_asmr_can_use_enabled_fades(self):
        import numpy as np
        from asmrclip.common import settings
        from asmrclip.planner import make_plan
        class Texture:
            def windows(self,spans):return [{'start':a,'end':b,'texture':.8,'breath':0.,'speech':0.} for a,b in spans]
        meta={'frame_samples':1024,'sample_rate':10240};frames={'levels':np.full((400,2),.08,np.float32)}
        exclusions={'extraction':{'intervals':[[5,35]]}}
        cfg=settings({'mode':'extract','join_fade_enabled':False})
        run=lambda c:make_plan(meta,frames,{'spoken':[],'accepted':[]},[],c,Texture(),exclusions)
        self.assertEqual(run(cfg)['duration'],0)
        self.assertGreater(run({**cfg,'join_fade_enabled':True})['duration'],25)
        self.assertEqual(run({**cfg,'join_fade_enabled':True,'edge_fade_enabled':False})['duration'],0)
        relaxed={**cfg,'mode':'relaxed','join_fade_enabled':True}
        self.assertGreater(run(relaxed)['duration'],35)
        # Both boundaries used to select this same central quiet point.
        frames['levels'][195:210]=.0001
        self.assertGreater(run({**cfg,'join_fade_enabled':True})['duration'],25)

    def test_unreliable_review_positions_are_kept_for_listening_only(self):
        rows=[{'start':1,'end':1.08,'review_only':True},{'start':2,'end':3}]
        mapping=[{'output_start':0,'output_end':10,'analysis_start':30}]
        self.assertEqual(mapped_output_to_source(rows,mapping),[[32,33]])
        self.assertEqual(output_to_source(rows,[[300,400]],.1),[[32,33]])

    def test_final_review_does_not_cover_silence_between_distant_words(self):
        from unittest.mock import Mock
        import numpy as np
        from asmrclip.reviewer import Reviewer
        reviewer=Reviewer.__new__(Reviewer);reviewer.cache=None;reviewer.cfg={}
        reviewer.recognizer=Mock()
        reviewer.recognizer.transcribe.return_value=[{'backend':'qwen3-asr','text':'hello again',
            'start':1,'end':20,'words':[{'word':'hello','start':1,'end':2},{'word':'again','start':19,'end':20}]}]
        rows,_,_=reviewer.inspect_chunk(np.zeros(28*16000,np.int16),'model')
        self.assertEqual([(r['start'],r['end']) for r in rows],[(1,2),(19,20)])

    def test_published_path_fits_windows_shell_with_unicode(self):
        parent=Path('C:/exports')/('a'*95)
        folder,name=export_names(parent,'日本語😀'*40,'v2','20260101_120000','01234567','.mp4')
        self.assertLessEqual(windows_length(folder/name),240)
        self.assertTrue(name.endswith('_ASMR_v2.mp4'))
        self.assertEqual((folder,name),export_names(parent,'日本語😀'*40,'v2','20260101_120000','01234567','.mp4'))

    def test_long_directory_is_rejected_before_export(self):
        with self.assertRaisesRegex(ValueError,'目录'):
            export_names(Path('C:/')/('a'*220),'input','v2','20260101_120000','01234567','.mp4')


if __name__=='__main__':unittest.main()
