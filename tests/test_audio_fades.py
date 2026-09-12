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
from asmrclip.audio_fades import ramps_for,edges_for,envelope,pcm_blocks,apply_fades,verify_stream_copy
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

    def test_edges_default_on_and_both_switches_off_preserve_packets(self):
        self.assertFalse(settings({})['join_fade_enabled'])
        self.assertTrue(settings({})['edge_fade_enabled'])
        self.assertEqual(settings({})['edge_fade_seconds'],.5)
        report={'mapping':[{},{}]}
        self.assertIs(apply_fades(None,report,{'join_fade_enabled':False,'edge_fade_enabled':False},None),report)
        for cfg in ({'join_fade_enabled':'true'},{'join_fade_seconds':float('nan')},{'join_fade_seconds':0},{'join_fade_seconds':3},{'edge_fade_enabled':'true'},{'edge_fade_seconds':float('nan')},{'edge_fade_seconds':0},{'edge_fade_seconds':4},{'audio_output_codec':'invalid'}):
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):settings(cfg)

    def test_edge_ramps_reach_zero_keep_middle_and_do_not_overlap_short_joins(self):
        for mapping in ([{'output_start':0,'output_end':4}],
                        [{'output_start':0,'output_end':.08},{'output_start':.08,'output_end':.14}]):
            count=round(mapping[-1]['output_end']*48000)
            joins,_=ramps_for(mapping,48000,.3)
            edges,details=edges_for(mapping,48000,.5,count/48000)
            ramps=sorted(joins+edges,key=lambda r:r['start'])
            self.assertTrue(all(a['end']<=b['start'] for a,b in zip(ramps,ramps[1:])))
            pcm=np.ones((2,count),np.float32)
            output=np.concatenate([x for _,x in envelope(((a,pcm[:,a:a+113]) for a in range(0,count,113)),ramps)],axis=1)
            np.testing.assert_array_equal(output[:,0],0);np.testing.assert_array_equal(output[:,-1],0)
            covered=np.zeros(count,bool)
            for r in ramps:covered[r['start']:r['end']]=True
            np.testing.assert_array_equal(output[:,~covered],pcm[:,~covered])
            self.assertTrue(np.all(np.diff(output[0,:edges[0]['end']])>=0))
            self.assertTrue(np.all(np.diff(output[0,edges[-1]['start']:])<=0))
            self.assertEqual(len(details),2)


class FadeExportTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG.exists():self.skipTest('FFmpeg required')
        self.temp=tempfile.TemporaryDirectory(prefix='ASMR 淡化 ');self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def source(self,suffix='m4a',audio='aac',video=None,offset=False):
        self.source_count=getattr(self,'source_count',0)+1
        path=self.folder/(f'源 文件 {self.source_count}.'+suffix)
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
        meta,frames=analyze(source,self.folder/('cache-'+source.name));dt=1024/meta['sample_rate']
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

    def test_single_section_skips_join_encoding_when_edge_fades_are_off(self):
        source=self.source();meta,frames,plan=self.prepared(source);plan['keep_frames']=plan['keep_frames'][:1]
        cfg=settings({'edge_fade_enabled':False,'join_fade_enabled':True,'review_enabled':False})
        faded=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
        plain=export(source,self.folder/'out',meta,frames,plan,{**cfg,'join_fade_enabled':False},fingerprint(source))
        self.assertTrue(faded['payload_unchanged']);self.assertFalse(faded['audio_fades']['applied'])
        self.assertEqual(faded['payload_sha256'],plain['payload_sha256'])

    def test_video_without_speech_review_still_applies_fades(self):
        source=self.source('mp4','aac','libx264');meta,frames,plan=self.prepared(source)
        plan['keep_frames']=plan['keep_frames'][:1]
        cfg=settings({'join_fade_enabled':False,'review_enabled':False})
        report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
        self.assertTrue(report['audio_fades']['applied']);self.assertFalse(report['audio_review_before_video_export'])
        self.assertEqual(report['speech_review']['status'],'disabled');self.assertTrue(report['decode_verified'])
        self.assertEqual(len(report['audio_fades']['edges']),2);self.assertEqual(report['audio_fades']['joins'],[])
        self.assertTrue(report['video_payload_unchanged']);self.assertTrue(report['reviewed_audio_payload_verified'])

    def test_single_section_output_has_default_edges_in_every_mode_before_review(self):
        source=self.source('flac','flac');meta,frames,plan=self.prepared(source)
        plan['keep_frames']=plan['keep_frames'][:1]
        plain=export(source,self.folder/'out',meta,frames,plan,settings({'edge_fade_enabled':False,'review_enabled':False}),fingerprint(source))
        for mode in ('strict','relaxed','extract'):
            seen=[]
            class Review:
                def inspect(self,path,report):
                    seen.append(report['payload_sha256']);return {'status':'passed','findings':[]}
            cfg=settings({'mode':mode})
            result=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source),Review())
            self.assertEqual(result['mapping'],plain['mapping']);self.assertEqual(result['duration'],plain['duration'])
            self.assertEqual(seen,[result['payload_sha256']]);self.assertFalse(result['payload_unchanged'])
            self.assertEqual(result['audio_fades']['joins'],[]);self.assertEqual(len(result['audio_fades']['edges']),2)
            faded=np.concatenate([x for _,x in pcm_blocks(result['output'],result)],axis=1)
            original=np.concatenate([x for _,x in pcm_blocks(plain['output'],plain)],axis=1)
            rms=lambda x:np.sqrt(np.mean(x*x))
            self.assertLess(rms(faded[:,:2400]),rms(original[:,:2400])*.15)
            self.assertLess(rms(faded[:,-2400:]),rms(original[:,-2400:])*.15)
            np.testing.assert_array_equal(faded[:,24000:-24000],original[:,24000:-24000])
            self.assertTrue((Path(result['output']).parent/'首尾淡化.csv').is_file())

    def test_selected_audio_formats_preserve_rate_channels_and_convert_without_fades(self):
        source=self.source();meta,frames,plan=self.prepared(source)
        plain=export(source,self.folder/'out',meta,frames,plan,settings({'edge_fade_enabled':False,'review_enabled':False}),fingerprint(source))
        original=np.concatenate([x for _,x in pcm_blocks(plain['output'],plain)],axis=1)
        with av.open(str(source)) as container:source_bitrate=container.streams.audio[0].codec_context.bit_rate
        for selected,extension,codec in [('flac','.flac','flac'),('pcm','.wav','pcm_f32le'),('aac','.m4a','aac')]:
            for fade in (False,True):
                with self.subTest(selected=selected,fade=fade):
                    cfg=settings({'audio_output_codec':selected,'edge_fade_enabled':fade,'join_fade_enabled':fade,'review_enabled':False})
                    report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source))
                    self.assertTrue(report['output'].endswith(extension));self.assertEqual(report['codec'],codec)
                    self.assertEqual(report['sample_rate'],48000);self.assertEqual(report['channels'],2)
                    self.assertEqual(report['mapping'],plain['mapping']);self.assertEqual(report['duration'],plain['duration'])
                    self.assertFalse(report['payload_unchanged']);self.assertFalse(report['audio_encoding']['resampled'])
                    self.assertEqual(report['audio_fades']['applied'],fade)
                    if selected=='aac':self.assertEqual(report['audio_encoding']['target_bitrate'],source_bitrate)
                    elif not fade:
                        output=np.concatenate([x for _,x in pcm_blocks(report['output'],report)],axis=1)
                        np.testing.assert_allclose(output,original,atol=2e-7,rtol=0)
                    self.assertTrue(report['decode_verified'])

    def test_selected_video_audio_formats_keep_every_picture_and_reviewed_audio_packet(self):
        for suffix,video,audio,selected in [('mp4','libx264','aac','flac'),('mp4','libx264','aac','pcm'),('webm','libvpx-vp9','libopus','aac')]:
            with self.subTest(selected=selected):
                source=self.source(suffix,audio,video,offset=True);meta,frames,plan=self.prepared(source)
                reference=self.folder/('reference-'+selected+'.'+suffix)
                original=copy_media_packets(source,reference,meta,frames,plan,inspect_media(source))
                seen=[]
                class Review:
                    def inspect(self,path,report):
                        seen.append(report['payload_sha256']);return {'status':'passed','findings':[]}
                cfg=settings({'audio_output_codec':selected,'edge_fade_enabled':False})
                report=export(source,self.folder/'out',meta,frames,plan,cfg,fingerprint(source),Review())
                self.assertTrue(report['output'].endswith('.mkv'));self.assertTrue(report['video_payload_unchanged'])
                self.assertEqual(report['mapping'],original['mapping']);self.assertEqual(seen,[report['payload_sha256']])
                self.assertTrue(report['reviewed_audio_payload_verified']);verify_stream_copy(reference,report['output'],'video')
                self.assertFalse(report['audio_fades']['applied']);self.assertFalse(report['payload_unchanged'])
                self.assertTrue(report['decode_verified'])


if __name__=='__main__':unittest.main()
