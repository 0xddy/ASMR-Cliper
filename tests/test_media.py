import hashlib
import gc
import math
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock,patch
from pathlib import Path

import av
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.analysis import analyze
from asmrclip.common import settings,fingerprint
from asmrclip.media import inspect_media,copy_media_packets,source_intervals,video_groups
from asmrclip.exporter import validate_decode
from asmrclip.exporter import export
from asmrclip.reviewer import decode_review_audio,mapped_output_to_source,SpeechRemaining

FFMPEG=ROOT/'runtime/tools/ffmpeg.exe'


class MediaExportTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG.exists():self.skipTest('FFmpeg is required')
        self.temp=tempfile.TemporaryDirectory(prefix='ASMR video 中文 ')
        self.folder=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def source(self,extension='mp4',video='libx264',audio='aac',extra=None):
        path=self.folder/('源 视频.'+extension)
        command=[str(FFMPEG),'-v','error','-f','lavfi','-i','testsrc2=size=160x96:rate=25:duration=12',
                 '-f','lavfi','-i','sine=frequency=337:sample_rate=48000:duration=12',
                 '-map','0:v:0','-map','1:a:0','-c:v',video,'-g','25','-pix_fmt','yuv420p','-c:a',audio]
        if video=='libx264':command+=['-bf','3','-sc_threshold','0']
        elif video=='libx265':command+=['-x265-params','log-level=error:open-gop=0:keyint=25:min-keyint=25:pools=1']
        if extra:command+=extra
        subprocess.run(command+[str(path)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        return path

    def plan(self,source):
        meta,frames=analyze(source,self.folder/'cache');dt=1024/meta['sample_rate']
        return meta,frames,{'keep_frames':[[math.ceil(1.3/dt),math.floor(5.6/dt)],
                                        [math.ceil(7.2/dt),math.floor(10.7/dt)]]}

    def verify_copy(self,source,meta,frames,plan):
        media=inspect_media(source);target=self.folder/('result'+media['extension'])
        report=copy_media_packets(source,target,meta,frames,plan,media)
        self.assertEqual(report['media_kind'],'video');self.assertTrue(report['video_payload_unchanged'])
        self.assertTrue(report['payload_unchanged']);self.assertEqual(report['segments'],2)
        accepted=source_intervals(meta,frames,plan)
        self.assertGreater(report['keyframe_trim_seconds'],0)
        for row in report['mapping']:
            self.assertTrue(any(a['source_start']<=row['source_start']+1e-6 and row['source_end']<=a['source_end']+1e-6 for a in accepted))
            self.assertLess(row['audio_start_gap'],.05);self.assertLess(row['audio_end_gap'],.05)
        run=subprocess.run([str(FFMPEG),'-v','error','-xerror','-i',str(target),'-f','null','-'],stderr=subprocess.PIPE)
        self.assertEqual(run.returncode,0,run.stderr.decode(errors='replace'));self.assertEqual(run.stderr,b'')
        # Decoded pictures must be the same pictures from the accepted source
        # intervals, with no images brought back from deleted gaps.
        def pictures(path,intervals=None):
            result=[]
            with av.open(str(path)) as container:
                for frame in container.decode(video=0):
                    t=float(frame.pts*frame.time_base)
                    if intervals is None or any(r['source_start']-1e-7<=t<r['source_end']-1e-7 for r in intervals):
                        result.append(hashlib.sha256(frame.to_ndarray(format='rgb24').tobytes()).hexdigest())
            return result
        self.assertEqual(pictures(target),pictures(source,report['mapping']))
        pcm=decode_review_audio(target,timeline=True)
        self.assertAlmostEqual(len(pcm)/16000,report['duration'],delta=.05)
        return report,target

    def test_h264_b_frames_and_aac_are_copied_without_deleted_pictures(self):
        source=self.source();self.verify_copy(source,*self.plan(source))

    def test_hevc_closed_gops(self):
        source=self.source(video='libx265');self.verify_copy(source,*self.plan(source))

    def test_webm_vp9_opus(self):
        source=self.source('webm','libvpx-vp9','libopus');self.verify_copy(source,*self.plan(source))

    def test_variable_frame_rate_and_matroska(self):
        source=self.source('mkv',extra=['-vf',"select='not(mod(n,2))+not(mod(n,5))'",'-fps_mode','vfr'])
        self.verify_copy(source,*self.plan(source))

    def test_mp3_audio_keeps_original_codec_in_matroska(self):
        source=self.folder/'audio.mp3'
        subprocess.run([str(FFMPEG),'-v','error','-f','lavfi','-i','sine=duration=12','-c:a','libmp3lame',str(source)],check=True)
        meta,frames,plan=self.plan(source)
        report=export(source,self.folder/'out',meta,frames,plan,settings({'mode':'relaxed','review_enabled':False,'edge_fade_enabled':False}),fingerprint(source))
        self.assertTrue(report['output'].endswith('.mka'));self.assertTrue(report['payload_unchanged'])
        self.assertEqual(report['codec'],'mp3float')

    def test_shifted_container_timestamps(self):
        source=self.source(extra=['-output_ts_offset','3'])
        self.verify_copy(source,*self.plan(source))

    def test_audio_output_from_video_preserves_old_aac_path(self):
        source=self.source();meta,frames,plan=self.plan(source)
        cfg=settings({'mode':'relaxed','output_kind':'audio','review_enabled':False,'edge_fade_enabled':False})
        report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
        self.assertTrue(report['output'].endswith('.m4a'));self.assertTrue(report['payload_unchanged'])
        with av.open(report['output']) as result:self.assertEqual(len(result.streams.video),0)

    def test_video_review_mapping_and_atomic_failure(self):
        source=self.source();meta,frames,plan=self.plan(source)
        class Reject:
            def inspect(self,path,report):
                self.report=report
                return {'status':'speech_found','findings':[{'start':.2,'end':.4,'text':'hello'}]}
        reviewer=Reject();cfg=settings({'mode':'extract','output_kind':'video'})
        with self.assertRaises(SpeechRemaining) as caught:
            export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source),reviewer)
        self.assertEqual(list((self.folder/'out').iterdir()),[])
        mapping=caught.exception.report['export_mapping']
        found=mapped_output_to_source(caught.exception.report['findings'],mapping)
        self.assertAlmostEqual(found[0][0],mapping[0]['analysis_start']+.2)
        self.assertNotAlmostEqual(found[0][0],plan['keep_frames'][0][0]*1024/meta['sample_rate']+.2)

    def test_no_keyframe_interval_is_not_published(self):
        source=self.source();meta,frames,_=self.plan(source)
        with self.assertRaisesRegex(ValueError,'关键帧'):
            export(source,self.folder/'out',meta,frames,{'keep_frames':[[70,80]]},
                   settings({'mode':'relaxed','output_kind':'video','review_enabled':False}),fingerprint(source))
        self.assertEqual(list((self.folder/'out').iterdir()),[])

    def test_audio_only_cannot_be_fabricated_into_video(self):
        source=self.folder/'audio.m4a'
        subprocess.run([str(FFMPEG),'-v','error','-f','lavfi','-i','sine=duration=2','-c:a','aac',str(source)],check=True)
        self.assertEqual(inspect_media(source)['kind'],'audio')
        with self.assertRaisesRegex(ValueError,'没有视频'):inspect_media(source,'video')

    def test_pipeline_rechecks_video_on_actual_export_timeline(self):
        self.addCleanup(gc.collect)
        from asmrclip.pipeline import run
        source=self.source();meta,frames,base=self.plan(source);dt=1024/meta['sample_rate']
        cfg=settings({'input':str(source),'output_dir':str(self.folder/'out'),'cache_dir':str(self.folder/'pipeline-cache'),
                      'mode':'extract','output_kind':'auto','review_enabled':True,'generate_program_menu':True})
        recognizer=MagicMock();recognizer.scan.return_value={'spoken':[],'language':'en'}
        classifier=MagicMock();classifier.music_intervals.return_value=[]
        classifier.exclusions.return_value={key:[] for key in ('voice','soft_laugh','heartbeat','tapping','loud_laugh','airflow','drinking','impacts')}
        class ReviewerStub:
            calls=0
            def inspect(self,path,report):
                self.calls+=1
                return {'status':'speech_found' if self.calls==1 else 'passed',
                        'findings':[{'start':.2,'end':.4,'text':'hello'}] if self.calls==1 else []}
            def close(self):pass
        reviewer=ReviewerStub();spoken=[]
        def annotate(path,report,cfg):
            self.assertTrue(Path(path).is_file())
            self.assertTrue(report['decode_verified'])
            self.assertEqual(reviewer.calls,2)
            self.assertEqual(report['cache_cleanup']['status'],'cleaned')
            self.assertFalse(list(Path(cfg['cache_dir']).rglob('analysis.wav')))
            report['program_menu']={'status':'ready','chapters':[]}
            return report['program_menu']
        def make_plan(meta,frames,speech,*args):
            spoken.append(list(speech['spoken']))
            return {**base,'frame_seconds':dt,'duration':sum(b-a for a,b in base['keep_frames'])*dt}
        with patch('asmrclip.model_catalog.validate_models'),patch('asmrclip.reviewer.validate_review_model'),\
             patch('asmrclip.recognition.Recognizer',return_value=recognizer),patch('asmrclip.classifier.Classifier',return_value=classifier),\
             patch('asmrclip.semantic.confirm',return_value={}),patch('asmrclip.reviewer.Reviewer',return_value=reviewer),\
             patch('asmrclip.drinking.review_drinking',return_value=([],{'candidates':[]})),\
             patch('asmrclip.planner.make_plan',side_effect=make_plan),\
             patch('asmrclip.media.video_groups',wraps=video_groups) as indexing,\
             patch('asmrclip.media.copy_media_packets',wraps=copy_media_packets) as copying,\
             patch('asmrclip.exporter.validate_decode',wraps=validate_decode) as validation,\
             patch('asmrclip.program_menu.attach',side_effect=annotate) as annotation:
            result=run(cfg)
        self.assertTrue(result['output'].endswith('.mp4'));self.assertEqual(result['speech_review']['status'],'passed')
        self.assertEqual(reviewer.calls,2);self.assertEqual(len(list((self.folder/'out').iterdir())),1)
        self.assertAlmostEqual(spoken[1][0][0],result['mapping'][0]['analysis_start']+.2)
        self.assertEqual([c.args[5]['kind'] for c in copying.call_args_list],['audio','audio','video'])
        self.assertEqual(indexing.call_count,1)
        self.assertEqual(validation.call_count,1)
        annotation.assert_called_once()
        self.assertEqual(result['program_menu']['status'],'ready')


if __name__=='__main__':unittest.main()
