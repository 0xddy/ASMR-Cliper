import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.model_catalog import required_components
from asmrclip.common import settings
from asmrclip.semantic import semantic_regions,positive


def row(a,target=.30,other=.12,**extra):
    return {'start':a,'end':a+10,'texture':.08,'semantic':{'mouth':target,'surface':.05,'other':other},**extra}


class ModelSemanticTests(unittest.TestCase):
    def test_model_install_repairs_dependencies_without_reinstalling_healthy_runtime(self):
        sys.path.insert(0,str(ROOT/'scripts'))
        from environment_manager import installation_components
        status={'components':[{'id':key,'required':key!='clap','status':state} for key,state in [('qwen','missing'),('aligner','missing'),('neural','ready'),('clap','missing')]]}
        self.assertEqual(installation_components(status,'qwen'),{'qwen','aligner'})
        status['components'][0]['status']='ready'
        self.assertEqual(installation_components(status,'qwen'),{'aligner'})
        self.assertEqual(installation_components(status,'clap'),{'clap'})
        self.assertEqual(installation_components(status,'all'),{'aligner'})

    def test_model_selection_changes_paths_and_requirements(self):
        full=settings({});self.assertTrue(full['whisper_model'].endswith('whisper-review'))
        turbo=settings({'speech_model':'whisper-turbo','review_enabled':False})
        self.assertTrue(turbo['whisper_model'].endswith('whisper-turbo'))
        self.assertNotIn('review',required_components(turbo))
        qwen=settings({'speech_model':'qwen3-asr'})
        self.assertTrue(qwen['whisper_model'].endswith('qwen-asr'))
        self.assertTrue({'qwen','aligner','neural','review'}<=required_components(qwen))
        self.assertTrue({'clap','neural'}<=required_components(settings({'mode':'extract'})))
        with self.assertRaises(ValueError):settings({'speech_model':'does-not-exist'})
        with self.assertRaises(ValueError):settings({'review_model_id':'whisper-turbo'})

    def test_v4_retains_continuous_semantic_support_without_high_ast_gate(self):
        rows=[row(a) for a in range(0,55,5)];rows[4]=row(20,target=.19,other=.17)
        result=semantic_regions(rows)
        self.assertEqual(result['intervals'],[[2.5,57.5]])
        self.assertFalse(positive(row(0,target=.1,other=.08)))
        self.assertFalse(positive(row(0,speech=.8)))
        self.assertFalse(positive(row(0,quiet=True)))
        self.assertTrue(positive({**row(0,target=.45,other=.2),'texture':.002}))
        self.assertFalse(positive({**row(0,target=.3,other=.28),'texture':.002}))
        self.assertFalse(positive({**row(0,target=.65,other=.2),'texture':.002,'speech':.85}))

    def test_qwen_review_requires_sound_model_independent_of_retention(self):
        for mode in ('strict','relaxed'):
            for speech in ('whisper-large-v3','qwen3-asr'):
                for review in ('whisper-large-v3','qwen3-asr'):
                    for enabled in (False,True):
                        with self.subTest(mode=mode,speech=speech,review=review,enabled=enabled):
                            cfg={'mode':mode,'speech_model':speech,'review_model_id':review,
                                 'review_enabled':enabled,'keep_drinking':True,'keep_whisper':False}
                            self.assertEqual('clap' in required_components(cfg),enabled and review=='qwen3-asr')
        self.assertIn('clap',required_components({**cfg,'mode':'extract','review_enabled':False}))

    def test_v4_does_not_bridge_rejected_or_unknown_audio(self):
        rows=[row(a) for a in range(0,45,5)];rows[4]=row(20,target=.1,other=.4)
        intervals=semantic_regions(rows)['intervals']
        self.assertEqual(len(intervals),2)
        self.assertLess(intervals[0][1],intervals[1][0])
        self.assertFalse(semantic_regions([row(0)])['intervals'])
        self.assertFalse(semantic_regions([row(0,target=.19),row(5,target=.19)])['intervals'])

    def test_v4_heartbeat_and_tapping_require_positive_evidence_and_honor_opt_out(self):
        for category in ('heartbeat','tapping'):
            rows=[{**row(a),category:.7,'texture':.7,'semantic':{category:.65,'mouth':.12,'surface':.1,'other':.2}} for a in (0,5,10)]
            self.assertTrue(semantic_regions(rows)['intervals'])
            self.assertFalse(semantic_regions(rows,{'keep_'+category:False})['intervals'])
            self.assertFalse(positive({**rows[0],'lexical':.9}))
            self.assertFalse(positive({**rows[0],'impact':.8}))

if __name__=='__main__':unittest.main()
