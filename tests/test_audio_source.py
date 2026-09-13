import hashlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import av
import numpy as np
import test_media

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
from asmrclip.analysis import analyze
from asmrclip.audio_source import extract_audio, verify_audio_copy, analyze_input_audio
from asmrclip.common import settings, fingerprint
from asmrclip.exporter import export, validate_decode
from asmrclip.media import inspect_media, copy_media_packets
from asmrclip.reviewer import decode_review_audio, mapped_output_to_source, SpeechRemaining

FFMPEG = ROOT / 'runtime/tools/ffmpeg.exe'


class AudioFirstTests(test_media.MediaFixture,unittest.TestCase):
    def source(self, extension='mp4', audio='aac', extra=()):
        video = 'libvpx-vp9' if extension == 'webm' else 'libx264'
        command = [str(FFMPEG), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=128x96:rate=25:duration=8',
                   '-f', 'lavfi', '-i', 'sine=frequency=337:sample_rate=48000:duration=8',
                   '-map', '0:v:0', '-map', '1:a:0', '-c:v', video, '-threads:v', '2', '-g', '25', '-c:a', audio]
        if video == 'libx264':command += ['-bf', '3', '-sc_threshold', '0']
        return self.fixture(command + list(extra), extension)

    def plan(self, source):
        meta, frames = analyze(source, self.folder / 'direct-cache')
        dt = 1024 / meta['sample_rate']
        return meta, frames, {'keep_frames': [[int(1.3/dt), int(4.5/dt)], [int(5.2/dt), int(7.7/dt)]]}

    def test_ffmpeg_extraction_preserves_payload_pcm_and_source_clock(self):
        cases = [('mp4', 'aac', ()), ('mp4', 'aac', ('-output_ts_offset', '2.317')), ('mkv', 'aac', ()), ('webm', 'libopus', ())]
        for i, (extension, audio, extra) in enumerate(cases):
            with self.subTest(container=extension, offset=extra):
                source = self.source(extension, audio, extra)
                track, proof = extract_audio(source, self.folder / f'extract-{i}', FFMPEG)
                with av.open(str(track)) as container:
                    self.assertEqual(len(container.streams.audio), 1)
                    self.assertEqual(len(container.streams.video), 0)
                self.assertTrue(proof['payload_unchanged'])
                self.assertFalse(proof['video_decoded'])
                a, fa = analyze(source, self.folder / f'original-{i}')
                b, fb = analyze(track, self.folder / f'track-{i}')
                self.assertEqual(a['frames'], b['frames'])
                np.testing.assert_array_equal(fa['levels'], fb['levels'])
                np.testing.assert_array_equal(fa['packets'], fb['packets'])
                np.testing.assert_allclose(fa['source_times'], fb['source_times'], atol=1/a['sample_rate'], rtol=0)
                self.assertEqual((self.folder/f'original-{i}/analysis.wav').read_bytes(), (self.folder/f'track-{i}/analysis.wav').read_bytes())

    def test_cached_track_is_reused_and_corruption_is_rebuilt(self):
        source = self.source();cache = self.folder / 'cache'
        track, proof = extract_audio(source, cache, FFMPEG)
        with patch('asmrclip.audio_source.run_ffmpeg', side_effect=AssertionError('Must reuse valid track')):
            self.assertEqual(extract_audio(source, cache, FFMPEG)[1], proof)
        track.write_bytes(b'corrupt')
        self.assertEqual(extract_audio(source, cache, FFMPEG)[1]['payload_sha256'], proof['payload_sha256'])

    def test_audio_output_from_video_still_uses_an_audio_only_analysis_source(self):
        source = self.source();media = inspect_media(source, 'audio')
        self.assertEqual(media['kind'], 'audio');self.assertTrue(media['input_has_video'])
        result, _ = analyze_input_audio(source, self.folder / 'cache', {'ffmpeg': str(FFMPEG)}, media)
        self.assertEqual(result['audio_preparation']['method'], 'ffmpeg_stream_copy')
        self.assertTrue((self.folder / 'cache/source-audio.mp4').is_file())
        with patch('asmrclip.audio_source.extract_audio', side_effect=AssertionError('Analysis is already cached')):
            self.assertEqual(analyze_input_audio(source, self.folder / 'cache', {}, media)[0], result)

    def test_failed_extraction_does_not_publish_a_cache(self):
        source = self.source();cache = self.folder / 'cache'
        original = hashlib.sha256(source.read_bytes()).digest()
        with patch('asmrclip.audio_source.run_ffmpeg', side_effect=RuntimeError('cancelled')):
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):extract_audio(source, cache, FFMPEG)
        self.assertFalse((cache / 'source-audio.json').exists())
        self.assertFalse((cache / 'source-audio.mp4').exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), original)

    def test_shifted_audio_clock_is_rejected_even_with_identical_packets(self):
        source = self.source();track = self.folder / 'shifted.mp4'
        subprocess.run([str(FFMPEG), '-v', 'error', '-copyts', '-itsoffset', '1', '-i', str(source),
                        '-map', '0:a:0', '-c:a', 'copy', '-avoid_negative_ts', 'disabled', str(track)], check=True)
        with self.assertRaisesRegex(ValueError, '时间戳'):verify_audio_copy(source, track)

    def test_rejected_audio_never_exports_or_decodes_video(self):
        source = self.source();meta, frames, plan = self.plan(source)
        class Reject:
            def inspect(self, path, report):
                with av.open(str(path)) as c:
                    if len(c.streams.video):raise AssertionError('Review candidate contains video')
                return {'status':'speech_found', 'findings':[{'start':.1, 'end':.3, 'text':'speech'}]}
        cfg = settings({'mode':'extract', 'review_enabled':True})
        with patch('asmrclip.media.copy_media_packets', wraps=copy_media_packets) as copying, patch('asmrclip.exporter.validate_decode') as validation:
            with self.assertRaises(SpeechRemaining) as caught:
                export(source, self.folder/'out', meta, frames, plan, cfg, fingerprint(source), Reject())
        self.assertEqual([c.args[5]['kind'] for c in copying.call_args_list], ['audio'])
        validation.assert_not_called()
        self.assertEqual(list((self.folder/'out').iterdir()), [])
        mapping=caught.exception.report['export_mapping']
        found=mapped_output_to_source(caught.exception.report['findings'],mapping)
        self.assertAlmostEqual(found[0][0],mapping[0]['analysis_start']+.1)
        self.assertNotAlmostEqual(found[0][0],plan['keep_frames'][0][0]*1024/meta['sample_rate']+.1)

    def test_final_video_audio_is_identical_to_reviewed_audio(self):
        for i, (extension, audio) in enumerate((('mp4','aac'), ('mkv','aac'), ('webm','libopus'))):
            with self.subTest(container=extension):
                source = self.source(extension, audio)
                meta, frames = analyze(source, self.folder/f'cache-{i}');dt = 1024/meta['sample_rate']
                plan = {'keep_frames':[[int(1.3/dt),int(4.5/dt)],[int(5.2/dt),int(7.7/dt)]]}
                class Approve:
                    def inspect(self, path, report):
                        self.audio = decode_review_audio(path, timeline=True)
                        return {'status':'passed', 'findings':[], 'candidate_payload_sha256':report['payload_sha256']}
                review = Approve();cfg = settings({'mode':'relaxed','review_enabled':True})
                result = export(source, self.folder/f'out-{i}', meta, frames, plan, cfg, fingerprint(source), review)
                np.testing.assert_array_equal(review.audio, decode_review_audio(result['output'], timeline=True))
                self.assertEqual(result['speech_review']['candidate_payload_sha256'],result['payload_sha256'])
                self.assertTrue(result['audio_review_before_video_export'])
                self.assertEqual(result['decode_validation']['scope'],'final_output_only')
                self.assertFalse(list(Path(result['output']).parent.glob('audio-review*')))

    def test_final_decode_has_explicit_thread_limits(self):
        with patch('asmrclip.audio_source.run_ffmpeg') as run:
            validate_decode(Path('result.mp4'), FFMPEG, {'duration': 8})
        args = run.call_args.args[0]
        for name in ('-threads:v','-threads:a'):
            self.assertEqual(args[args.index(name)+1], '2')
            self.assertLess(args.index(name), args.index('-i'))


if __name__ == '__main__':unittest.main()
