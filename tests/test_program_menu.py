import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

import numpy as np
from scipy.io import wavfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from asmrclip import program_menu as menu
from asmrclip.common import ROOT,read_json,save_json,settings
from asmrclip.reviewer import decode_review_audio


def vector(category):
    return [.42 if key==category else .06 for key,prompts in menu.PROMPTS.items() for _ in prompts]


class MenuRulesTests(unittest.TestCase):
    def test_uncertain_and_competing_sounds_are_not_invented_as_projects(self):
        self.assertEqual(menu.identify({'tapping':.16,'speech':.1})[0],'unknown')
        self.assertEqual(menu.identify({'tapping':.30,'speech':.29})[0],'unknown')
        self.assertEqual(menu.identify({'tapping':.30,'rubbing':.29})[0],'mixed')
        self.assertEqual(menu.identify({'tapping':.35,'speech':.10})[0],'tapping')
        with self.assertRaises(RuntimeError):menu.sound_scores([.4])
        with self.assertRaises(RuntimeError):menu.sound_scores([float('nan')]*sum(map(len,menu.PROMPTS.values())))

    def test_windows_cover_actual_output_without_probing_across_joins(self):
        mapping=[{'output_start':0,'output_end':7.3,'source_start':3600},
                 {'output_start':7.3,'output_end':21.2,'source_start':5000}]
        rows=menu.windows(0,21.2,mapping)
        self.assertEqual(rows[0]['start'],0);self.assertEqual(rows[-1]['end'],21.2)
        for a,b in zip(rows,rows[1:]):self.assertEqual(a['end'],b['start'])
        for r in rows:
            self.assertLessEqual(r['probe_start'],r['start']);self.assertGreaterEqual(r['probe_end'],r['end'])
            self.assertLessEqual(r['probe_end']-r['probe_start'],10.)
            self.assertFalse(r['probe_start']<7.3<r['probe_end'])

    def test_menu_order_is_from_audio_not_category_or_settings_order(self):
        rows=[{'start':a,'end':b,'category':c} for a,b,c in
              [(0,5,'heartbeat'),(5,10,'heartbeat'),(10,15,'tapping'),(15,20,'wet_mouth'),(20,25,'unknown')]]
        result=menu.chapters(rows,[{'start':12,'end':13,'text':'possible speech'}])
        self.assertEqual([r['category'] for r in result],['heartbeat','tapping','wet_mouth','unknown'])
        self.assertEqual(result[0]['end'],10)
        self.assertEqual(len(result[1]['review_findings']),1)
        self.assertTrue(result[-1]['needs_confirmation'])
        text=menu.menu_text({'chapters':result})
        self.assertLess(text.index('心跳'),text.index('道具敲击'))
        self.assertIn('00:00:10 — 00:00:15',text)

    def test_mixed_categories_merge_even_if_score_ranking_changes(self):
        rows=[{'start':0,'end':5,'category':'mixed','alternatives':['rubbing','tapping']},
              {'start':5,'end':10,'category':'mixed','alternatives':['tapping','rubbing']}]
        self.assertEqual(len(menu.chapters(rows)),1)

    def test_generation_failure_does_not_fail_or_delete_finished_media(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'finished.m4a';path.write_bytes(b'unchanged finished media')
            for failure in (None,RuntimeError('model unavailable')):
                report={'decode_verified':True,'output':str(path)}
                with patch.object(menu,'generate',side_effect=failure,return_value={'status':'unavailable','chapters':[],'note':'install CLAP'}),contextlib.redirect_stdout(io.StringIO()):
                    result=menu.attach(path,report,{})
                self.assertIn(result['status'],('unavailable','failed'))
                self.assertTrue(read_json(path.parent/'校验报告.json')['decode_verified'])
                self.assertEqual(path.read_bytes(),b'unchanged finished media')

    def test_menu_config_is_boolean(self):
        self.assertTrue(settings({})['generate_program_menu'])
        with self.assertRaises(ValueError):settings({'generate_program_menu':'true'})


class MenuAudioTests(unittest.TestCase):
    def test_streamed_pcm_matches_final_audio_clock_for_audio_and_video(self):
        ffmpeg=ROOT/'runtime/tools/ffmpeg.exe'
        if not ffmpeg.exists():self.skipTest('FFmpeg not installed')
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            for video in (False,True):
                path=folder/('video.mp4' if video else 'audio.m4a')
                command=[str(ffmpeg),'-v','error']
                if video:command+=['-f','lavfi','-i','color=black:s=64x64:r=10:d=4','-itsoffset','0.12']
                command+=['-f','lavfi','-i','sine=frequency=600:sample_rate=44100:duration=3.7','-c:a','aac']
                if video:command+=['-c:v','libx264','-threads:v','1']
                subprocess.run(command+[str(path)],check=True)
                reference=decode_review_audio(path,video)
                duration=4 if video else len(reference)/16000
                with patch.object(menu,'BLOCK',2*16000):parts=list(menu.decoded_blocks(path,duration,video))
                actual=np.concatenate([p for _,p in parts])
                expected=np.pad(reference,(0,max(0,round(duration*16000)-len(reference))))[:round(duration*16000)]
                np.testing.assert_array_equal(actual,expected)
                self.assertTrue(all(len(p)<=32000 for _,p in parts))
                self.assertEqual([a for a,_ in parts],list(range(0,len(actual),32000)))

    def test_inference_reads_finished_pcm_exports_sidecars_and_cleans_completed_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);path=folder/'finished.wav'
            pcm=(np.sin(np.arange(20*16000)*.07)*3000).astype(np.int16)
            wavfile.write(path,16000,pcm)
            original=hashlib.sha256(path.read_bytes()).hexdigest()
            report={'duration':20.,'output':str(path),'decode_verified':True,
                    'mapping':[{'output_start':0.,'output_end':10.,'source_start':1000.},
                               {'output_start':10.,'output_end':20.,'source_start':2000.}]}
            cfg={'cache_dir':str(folder/'cache'),'device':'cpu'}
            client=Mock()
            def classify(request,audio):
                self.assertEqual(request['op'],'classify')
                # The samples supplied to the sound model are decoded final
                # media, not source times 1000/2000 or a requested project list.
                self.assertLessEqual(len(audio),8*10*16000)
                self.assertAlmostEqual(float(audio[1]),float(pcm[1])/32768,places=6)
                return [vector('heartbeat') for _ in request['clips']]
            client.request.side_effect=classify
            with patch.object(menu,'available',return_value=True),patch.object(menu,'model_signature',return_value=['model']), \
                 patch('asmrclip.neural_client.NeuralClient',return_value=client) as factory:
                first=menu.generate(path,report,cfg);second=menu.generate(path,report,cfg)
            self.assertEqual(factory.call_count,2)
            self.assertEqual(first['chapters'],second['chapters'])
            self.assertEqual(first['chapters'][0]['title'],'心跳')
            self.assertEqual(first['chapters'][0]['start'],0.)
            self.assertEqual(first['chapters'][-1]['end'],20.)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),original)
            for name in ('节目单.txt','节目单.csv','节目单.json'):self.assertTrue((folder/name).is_file())
            self.assertEqual(read_json(folder/'节目单.json')['timeline'],'final_output')
            self.assertEqual(first['cache_cleanup']['status'],'cleaned')
            self.assertEqual(list((folder/'cache').rglob('menu-windows.json')),[])
            self.assertEqual(list((folder/'cache').rglob('evidence.json')),[])

    def test_annotation_command_rejects_unrelated_report(self):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);path=folder/'finished.m4a';path.write_bytes(b'media')
            save_json(folder/'校验报告.json',{'decode_verified':True,'output':str(folder/'other.m4a')})
            with patch.object(menu,'attach',side_effect=AssertionError('wrong report')),self.assertRaisesRegex(RuntimeError,'不匹配'):
                menu.run({'input':str(path)})


if __name__=='__main__':unittest.main()
