import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
import numpy as np
import av
from asmrclip.common import settings
from asmrclip.planner import make_plan
from asmrclip.recognition import plausible,recover_jsonl
from asmrclip.analysis import analyze,inspect_audio
from asmrclip.exporter import copy_packets,export


class StableTexture:
    def windows(self,windows):
        return [{'start':a,'end':b,'texture':.85,'breath':.01,'speech':.01,'quiet':False} for a,b in windows]


class PlannerRequirements(unittest.TestCase):
    def setUp(self):
        self.dt=.1
        self.meta={'sample_rate':10240,'frame_samples':1024}
        self.rms=np.full(1400,.05,dtype=np.float32)
        for start in range(0,1400,20):self.rms[start:start+6]=.0003
        self.frames={'levels':np.c_[self.rms,self.rms]}
        self.spoken=[[20,23],[42,45],[64,67],[86,90]]
        self.speech={'spoken':self.spoken,'accepted':[{'start':a,'end':b} for a,b in self.spoken]}

    def test_dense_speech_does_not_blanket_delete_relaxed_gaps(self):
        v2=make_plan(self.meta,self.frames,self.speech,[],settings({'mode':'strict'}),StableTexture())
        v3=make_plan(self.meta,self.frames,self.speech,[],settings({'mode':'relaxed'}),StableTexture())
        self.assertTrue(v2['dense_conversations'])
        self.assertFalse(v3['dense_conversations'])
        self.assertGreater(v3['duration'],v2['duration']+30)
        self.assertTrue(any(23<=a*.1<b*.1<=42 for a,b in v3['keep_frames']))

    def test_relaxed_has_no_mandatory_five_seven_second_padding(self):
        plan=make_plan(self.meta,self.frames,self.speech,[],settings({'mode':'relaxed'}),StableTexture())
        self.assertTrue(any(23<=a*.1<30 for a,b in plan['keep_frames']))
        self.assertTrue(any(15<b*.1<=20 for a,b in plan['keep_frames']))
        for a,b in plan['keep_frames']:
            self.assertFalse(any(min(b*.1,d)>max(a*.1,c)+1e-7 for c,d in self.spoken))

    def test_search_expands_before_discarding_long_asmr(self):
        rms=np.full(1800,.05,np.float32)
        rms[460:475]=.0003
        rms[1300:1320]=.0003
        speech={'spoken':[[10,12],[140,145]],'accepted':[]}
        plan=make_plan(self.meta,{'levels':np.c_[rms,rms]},speech,[],settings({'mode':'relaxed'}),StableTexture())
        self.assertTrue(any(a*.1>=46 and b*.1>=130 for a,b in plan['keep_frames']))

    def test_long_silence_shortened_short_pauses_retained(self):
        rms=self.rms.copy();rms[300:450]=.00001
        plan=make_plan(self.meta,{'levels':np.c_[rms,rms]},{'spoken':[],'accepted':[]},[],settings({'mode':'relaxed'}),StableTexture())
        self.assertEqual(len(plan['silence_removed']),1)
        self.assertGreater(plan['duration'],120)

    def test_hallucinated_outro_and_nonlexical_breath_not_speech(self):
        base={'start':1,'end':2,'compression_ratio':1,'avg_logprob':-.5}
        self.assertFalse(plausible({**base,'text':'시청해주셔서 감사합니다','words':[{'probability':.05},{'probability':.99}]},[[0,4]]))
        self.assertFalse(plausible({**base,'text':'으으으 음','words':[{'probability':.9}]},[[0,4]]))
        self.assertTrue(plausible({**base,'text':'이제 시작할게요','words':[{'probability':.9},{'probability':.9}]},[[0,4]]))

    def test_cancelled_jsonl_recovers_completed_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'partial.jsonl'
            path.write_text('{"offset":0}\n{"offset":600,"text":"unfinished',encoding='utf-8')
            self.assertEqual(recover_jsonl(path),[{'offset':0}])
            self.assertEqual(json.loads(path.read_text()),{'offset':0})


class PacketCopyIntegration(unittest.TestCase):
    def test_stereo_and_mono_different_rates_preserve_payload_and_alignment(self):
        ffmpeg=ROOT/'runtime/tools/ffmpeg.exe'
        if not ffmpeg.exists():self.skipTest('FFmpeg is not installed')
        for rate,channels in [(44100,2),(48000,1)]:
            with self.subTest(rate=rate,channels=channels),tempfile.TemporaryDirectory(prefix='ASMR 中文 ') as folder:
                folder=Path(folder);source=folder/'源 音频.m4a'
                subprocess.run([str(ffmpeg),'-v','error','-f','lavfi','-i',f'sine=frequency=337:sample_rate={rate}:duration=8','-ac',str(channels),'-c:a','aac','-aac_pns','0','-b:a','192k',str(source)],check=True)
                cache=folder/'cache';meta,frames=analyze(source,cache)
                n=meta['frames'];keep=[[5,n//3],[n//2,n-5]]
                target=folder/'输出.m4a'
                report=copy_packets(source,target,meta,frames,{'keep_frames':keep})
                self.assertTrue(report['payload_unchanged'])
                self.assertEqual(report['sample_rate'],rate)
                self.assertEqual(report['channels'],channels)
                with av.open(str(source)) as c:original=[f.to_ndarray() for f in c.decode(audio=0) if f.samples==1024]
                with av.open(str(target)) as c:edited=[f.to_ndarray() for f in c.decode(audio=0)]
                self.assertEqual(len(edited),sum(b-a for a,b in keep))
                offset=0
                for a,b in keep:
                    for i in range(3,b-a):np.testing.assert_allclose(edited[offset+i],original[a+i],atol=1e-6,rtol=0)
                    offset+=b-a

    def test_no_empty_output_is_created(self):
        with tempfile.TemporaryDirectory() as folder:
            out=Path(folder)/'output'
            with self.assertRaises(ValueError):export(Path(folder)/'source.m4a',out,{},None,{'keep_frames':[]},{},'unused')
            self.assertFalse(out.exists())


if __name__=='__main__':unittest.main()
