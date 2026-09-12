import hashlib
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import av
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.audio_fades import ramps_for,envelope,pcm_blocks,apply_fades,verify_stream_copy
from asmrclip.analysis import analyze
from asmrclip.common import settings,fingerprint
from asmrclip.exporter import export
from asmrclip.media import copy_media_packets,inspect_media

FFMPEG=ROOT/'runtime/tools/ffmpeg.exe'


class FadeEnvelopeTests(unittest.TestCase):
    def test_sample_envelope_reduces_large_step_without_changing_other_samples(self):
        mapping=[{'output_start':0,'output_end':2},{'output_start':2,'output_end':4}]
        ramps,joins=ramps_for(mapping,1000,.3)
        pcm=np.ones((2,4000),np.float32)*.05;pcm[:,2000:]=.8
        blocks=((a,pcm[:,a:a+127]) for a in range(0,4000,127))
        result=np.concatenate([x for _,x in envelope(blocks,ramps)],axis=1)
        self.assertEqual(result.shape,pcm.shape)
        np.testing.assert_array_equal(result[:,:1700],pcm[:,:1700])
        np.testing.assert_array_equal(result[:,2300:],pcm[:,2300:])
        self.assertLess(abs(result[0,2000]-result[0,1999]),.001)
        self.assertTrue(np.all(np.diff(result[0,1700:2001])<=0))
        self.assertTrue(np.all(np.diff(result[0,2000:2301])>=0))
        self.assertEqual(joins[0]['time'],2.)

    def test_short_sections_keep_middle_half_and_do_not_overlap_fades(self):
        mapping=[{'output_start':0,'output_end':.1},{'output_start':.1,'output_end':.18},{'output_start':.18,'output_end':.3}]
        ramps,_=ramps_for(mapping,48000,.3)
        self.assertLess(ramps[1]['end'],ramps[2]['start'])
        self.assertAlmostEqual((ramps[1]['end']-ramps[1]['start'])/48000,.02)

    def test_default_is_packet_copy_and_invalid_values_are_rejected(self):
        self.assertFalse(settings({})['join_fade_enabled'])
        report={'mapping':[{},{}]}
        self.assertIs(apply_fades(None,report,{'join_fade_enabled':False},None),report)
        for cfg in ({'join_fade_enabled':'true'},{'join_fade_seconds':float('nan')},{'join_fade_seconds':0},{'join_fade_seconds':3}):
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):settings(cfg)


class FadeExportTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG.exists():self.skipTest('FFmpeg required')
        self.temp=tempfile.TemporaryDirectory(prefix='ASMR 淡化 ');self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def source(self,suffix='m4a',audio='aac',video=None,offset=False):
        path=self.folder/('源 文件.'+suffix)
        command=[str(FFMPEG),'-v','error','-f','lavfi','-i',
                 "aevalsrc='if(lt(t,6),0.03,0.5)*sin(2*PI*337*t)':s=48000:d=12"]
        if video:
            command+=['-f','lavfi','-i','testsrc2=size=160x96:rate=25:duration=12','-map','1:v:0','-map','0:a:0',
                      '-c:v',video,'-g','25']
            if video=='libx264':command+=['-bf','3','-sc_threshold','0']
        command+=['-c:a',audio,'-ac','2']
        if offset:command+=['-output_ts_offset','3']
        subprocess.run(command+[str(path)],check=True,stderr=subprocess.PIPE)
        return path

    def prepared(self,source):
        meta,frames=analyze(source,self.folder/'cache');dt=1024/meta['sample_rate']
        plan={'keep_frames':[[math.ceil(1.3/dt),math.floor(5.6/dt)],[math.ceil(7.2/dt),math.floor(10.7/dt)]]}
        return meta,frames,plan

    def test_audio_native_format_levels_and_review_use_filtered_candidate(self):
        for suffix,codec in [('m4a','aac'),('flac','flac'),('mp3','libmp3lame')]:
            with self.subTest(codec=codec):
                # Each codec needs its own analysis cache.
                source=self.source(suffix,codec);meta,frames=analyze(source,self.folder/('cache-'+suffix));dt=1024/meta['sample_rate']
                plan={'keep_frames':[[math.ceil(1/dt),math.floor(5/dt)],[math.ceil(7/dt),math.floor(11/dt)]]}
                original=hashlib.sha256(source.read_bytes()).digest();seen=[]
                class Review:
                    def inspect(self,path,report):
                        seen.append((report['audio_fades']['applied'],hashlib.sha256(Path(path).read_bytes()).digest()))
                        return {'status':'passed','findings':[]}
                cfg=settings({'join_fade_enabled':True,'review_enabled':True})
                report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source),Review())
                self.assertEqual(seen,[(True,hashlib.sha256(Path(report['output']).read_bytes()).digest())])
                self.assertFalse(report['payload_unchanged']);self.assertTrue(report['decode_verified'])
                self.assertEqual(report['sample_rate'],48000);self.assertEqual(report['channels'],2)
                self.assertEqual(original,hashlib.sha256(source.read_bytes()).digest())
                decoded=np.concatenate([x for _,x in pcm_blocks(report['output'],report)],axis=1)
                cut=round(report['mapping'][1]['output_start']*48000)
                rms=lambda x:np.sqrt(np.mean(x*x))
                self.assertLess(rms(decoded[:,cut-240:cut+240]),rms(decoded[:,cut+24000:cut+30000])*.05)
                self.assertGreater(report['audio_fades']['joins'][0]['level_difference_db'],18)
                self.assertEqual(decoded.shape[1],round(report['duration']*48000))
                self.assertTrue((Path(report['output']).parent/'接缝淡化.csv').is_file())

    def test_video_frames_clocks_and_reviewed_audio_survive_final_mux(self):
        for suffix,video,audio in [('mp4','libx264','aac'),('webm','libvpx-vp9','libopus')]:
            with self.subTest(suffix=suffix):
                source=self.source(suffix,audio,video,offset=True)
                meta,frames=analyze(source,self.folder/('cache-'+suffix));dt=1024/meta['sample_rate']
                plan={'keep_frames':[[math.ceil(1.3/dt),math.floor(5.6/dt)],[math.ceil(7.2/dt),math.floor(10.7/dt)]]}
                reference=self.folder/('reference.'+suffix)
                original_report=copy_media_packets(source,reference,meta,frames,plan,inspect_media(source))
                seen=[]
                class Review:
                    def inspect(self,path,report):
                        seen.append(report['payload_sha256']);return {'status':'passed','findings':[]}
                cfg=settings({'join_fade_enabled':True,'mode':'extract','review_enabled':True})
                report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source),Review())
                self.assertEqual(report['mapping'],original_report['mapping'])
                self.assertEqual(seen,[report['payload_sha256']]);self.assertTrue(report['reviewed_audio_payload_verified'])
                self.assertTrue(report['video_payload_unchanged']);self.assertFalse(report['payload_unchanged'])
                verify_stream_copy(reference,report['output'],'video')
                self.assertTrue(report['decode_verified'])

    def test_encoder_failure_does_not_publish_or_modify_source(self):
        source=self.source();meta,frames,plan=self.prepared(source);original=source.read_bytes()
        cfg=settings({'join_fade_enabled':True,'review_enabled':False,'ffmpeg':str(self.folder/'missing.exe')})
        with self.assertRaises(FileNotFoundError):export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
        self.assertEqual(list((self.folder/'out').iterdir()),[]);self.assertEqual(original,source.read_bytes())

    def test_single_section_skips_encoding_even_when_enabled(self):
        source=self.source();meta,frames,plan=self.prepared(source);plan['keep_frames']=plan['keep_frames'][:1]
        cfg=settings({'join_fade_enabled':True,'review_enabled':False})
        faded=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
        plain=export(source,self.folder/'out',meta,frames,plan,{**cfg,'join_fade_enabled':False},fingerprint(source))
        self.assertTrue(faded['payload_unchanged']);self.assertFalse(faded['audio_fades']['applied'])
        self.assertEqual(faded['payload_sha256'],plain['payload_sha256'])

    def test_video_without_speech_review_still_applies_fades(self):
        source=self.source('mp4','aac','libx264');meta,frames,plan=self.prepared(source)
        cfg=settings({'join_fade_enabled':True,'review_enabled':False})
        report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
        self.assertTrue(report['audio_fades']['applied']);self.assertFalse(report['audio_review_before_video_export'])
        self.assertEqual(report['speech_review']['status'],'disabled');self.assertTrue(report['decode_verified'])


if __name__=='__main__':unittest.main()
