"""Precise cuts decode references but retain only approved video frames."""
import unittest
import subprocess
from unittest.mock import patch

import av

import test_media
from asmrclip.common import settings, fingerprint
from asmrclip.exporter import export
from asmrclip.media import source_intervals
from asmrclip.reviewer import SpeechRemaining, decode_review_audio


class PreciseVideoTests(unittest.TestCase):
    setUp = test_media.MediaExportTests.setUp
    source = test_media.MediaExportTests.source
    plan = test_media.MediaExportTests.plan

    def render(self, source, plan=None, **options):
        meta, frames, default = self.plan(source)
        cfg = settings({'mode': 'relaxed', 'output_kind': 'video', 'video_cut_mode': 'precise',
            'review_enabled': False, 'edge_fade_enabled': False, 'device': 'cpu', **options})
        report = export(source, self.folder / 'out', meta, frames, plan or default, cfg, fingerprint(source))
        return report, source_intervals(meta, frames, plan or default)

    def test_short_interval_without_any_keyframe_exports_decodable_video_and_original_audio(self):
        source = self.source()
        report, accepted = self.render(source, {'keep_frames': [[70, 80]]})
        self.assertTrue(report['decode_verified']); self.assertFalse(report['video_payload_unchanged'])
        self.assertTrue(report['payload_unchanged']); self.assertTrue(report['reviewed_audio_payload_verified'])
        self.assertEqual(report['video_encoding']['encoder'], 'libx264')
        self.assertEqual(report['settings']['video_cut_mode'], 'precise')
        self.assertLess(report['video_frame_trim_seconds'], .08)
        self.assertEqual(report['keyframe_trim_seconds'], 0)
        with av.open(report['output']) as output:
            pictures = list(output.decode(video=0))
            self.assertEqual(len(pictures), report['video_packets'])
            self.assertEqual((pictures[0].width, pictures[0].height), (160, 96))
        row = report['mapping'][0]
        self.assertGreaterEqual(row['source_start'], accepted[0]['source_start'] - 1e-7)
        self.assertLessEqual(row['source_end'], accepted[0]['source_end'] + 1e-7)

    def test_multiple_cuts_and_shifted_timestamps_keep_video_and_audio_on_one_clock(self):
        source = self.source(extra=['-output_ts_offset', '3'])
        report, accepted = self.render(source)
        expected = []
        with av.open(str(source)) as original:
            for frame in original.decode(video=0):
                time = float(frame.pts * frame.time_base)
                for row in report['mapping']:
                    if row['source_start'] - 1e-7 <= time < row['source_end'] - 1e-7:
                        expected.append(time - row['source_start'] + row['output_start'])
        with av.open(report['output']) as output:
            actual = [float(frame.pts * frame.time_base) for frame in output.decode(video=0)]
        self.assertEqual(len(actual), len(expected))
        self.assertLess(max(abs(a - b) for a, b in zip(actual, expected)), .0011)
        self.assertAlmostEqual(len(decode_review_audio(report['output'], timeline=True)) / 16000,
                               report['duration'], delta=.05)
        for row in report['mapping']:
            self.assertTrue(any(r['source_start'] <= row['source_start'] + 1e-7 and row['source_end'] <= r['source_end'] + 1e-7 for r in accepted))

    def test_flac_and_edge_fades_keep_reviewed_audio_unchanged_when_muxed(self):
        report, _ = self.render(self.source(), audio_output_codec='flac', edge_fade_enabled=True)
        self.assertTrue(report['output'].endswith('.mkv'))
        self.assertEqual(report['codec'], 'flac'); self.assertFalse(report['payload_unchanged'])
        self.assertTrue(report['reviewed_audio_payload_verified'])
        self.assertEqual(len(report['audio_fades']['edges']), 2)

    def test_rejected_audio_never_starts_video_encoding_and_leaves_no_candidate(self):
        source = self.source(); meta, frames, plan = self.plan(source)
        class Reject:
            def inspect(self, path, report):
                return {'status': 'speech_found', 'findings': [{'start': .2, 'end': .4, 'text': 'speech'}]}
        with patch('asmrclip.video_encode.encode_video', side_effect=AssertionError('video before review')):
            with self.assertRaises(SpeechRemaining):
                export(source, self.folder / 'out', meta, frames, plan,
                    settings({'mode': 'extract', 'output_kind': 'video', 'video_cut_mode': 'precise'}),
                    fingerprint(source), Reject())
        self.assertEqual(list((self.folder / 'out').iterdir()), [])

    def test_vfr_webm_input_uses_compatible_container_without_frame_duplication(self):
        source = self.source('webm', 'libvpx-vp9', 'libopus', extra=['-vf', "select='not(mod(n,2))+not(mod(n,5))'", '-fps_mode', 'vfr'])
        report, _ = self.render(source)
        self.assertTrue(report['output'].endswith('.mkv'))
        self.assertTrue(report['payload_unchanged']); self.assertEqual(report['codec'], 'opus')
        with av.open(report['output']) as output:
            times = [float(frame.pts * frame.time_base) for frame in output.decode(video=0)]
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertEqual(len(times), report['video_packets'])

    def test_nvenc_failure_falls_back_before_publishing(self):
        from asmrclip import video_encode
        original = video_encode.encode_attempt; encoders = []
        def attempt(*args):
            encoders.append(args[-1])
            if args[-1] == 'h264_nvenc': raise av.error.ExternalError(1, 'unavailable test GPU')
            return original(*args)
        with patch.object(video_encode, 'encode_attempt', side_effect=attempt):
            report, _ = self.render(self.source(), device='auto')
        self.assertEqual(encoders, ['h264_nvenc', 'libx264'])
        self.assertEqual(report['video_encoding']['encoder'], 'libx264')

    def test_rotated_video_keeps_display_orientation_after_encoding_and_mux(self):
        original = self.source(); source = self.folder / 'rotated.mp4'
        subprocess.run([str(test_media.FFMPEG), '-v', 'error', '-i', str(original), '-c', 'copy',
            '-metadata:s:v:0', 'rotate=90', str(source)], check=True, capture_output=True)
        report, _ = self.render(source)
        with av.open(str(source)) as src, av.open(report['output']) as result:
            a = next(src.decode(video=0)); b = next(result.decode(video=0))
            self.assertNotEqual(a.rotation, 0)
            self.assertEqual(bytes(a.side_data['DISPLAYMATRIX']), bytes(b.side_data['DISPLAYMATRIX']))

    def test_hevc_10bit_keeps_resolution_bit_depth_and_colour(self):
        source = self.source(video='libx265', extra=['-pix_fmt', 'yuv420p10le',
            '-color_primaries', 'bt2020', '-color_trc', 'smpte2084', '-colorspace', 'bt2020nc'])
        report, _ = self.render(source)
        self.assertEqual(report['video_encoding']['encoder'], 'libx265')
        with av.open(str(source)) as src, av.open(report['output']) as result:
            a = src.streams.video[0].codec_context; b = result.streams.video[0].codec_context
            self.assertEqual(a.format.name, b.format.name)
            self.assertEqual((a.width, a.height), (b.width, b.height))
            for key in ('color_primaries', 'color_trc', 'colorspace'):
                self.assertEqual(getattr(a, key), getattr(b, key))

    def test_unknown_video_cut_mode_is_rejected(self):
        with self.assertRaises(ValueError): settings({'video_cut_mode': 'unsafe-copy'})


if __name__ == '__main__': unittest.main()
