import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import av

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
from asmrclip.audio_source import run_ffmpeg
from asmrclip.exporter import validate_decode

FFMPEG = ROOT / 'runtime/tools/ffmpeg.exe'


class DecodeValidationTests(unittest.TestCase):
    def setUp(self):
        if not FFMPEG.exists():
            self.skipTest('FFmpeg is required')
        temp = tempfile.TemporaryDirectory(prefix='ASMR decode 中文 ')
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / 'cluster.mp4'
        # Most frames are 40 ms apart, but the final ten are 1 ms apart.
        # A null encoder using the nominal 25 fps time base rounds these
        # distinct, valid source timestamps into duplicate output DTS.
        command = [str(FFMPEG), '-v', 'error', '-nostdin',
                   '-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=25:duration=3',
                   '-f', 'lavfi', '-i', 'sine=duration=3',
                   '-vf', r'settb=1/1000,setpts=if(lt(N\,65)\,N*40\,2600+(N-65))',
                   '-c:v', 'libx264', '-threads:v', '1', '-bf', '0',
                   '-fps_mode:v', 'passthrough', '-enc_time_base:v', '1/1000',
                   '-c:a', 'aac', str(self.source)]
        result = subprocess.run(command, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_dense_vfr_tail_validates_without_dropping_frames(self):
        with av.open(str(self.source)) as container:
            times = [frame.pts * frame.time_base for frame in container.decode(video=0)]
        self.assertEqual(len(times), 75)
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))
        self.assertLess(min(b - a for a, b in zip(times, times[1:])), .002)

        # Exercise the real validator and its normal error handling, then
        # inspect FFmpeg progress from that same command to rule out a fix
        # that merely drops the tightly spaced frames during validation.
        with patch('asmrclip.audio_source.run_ffmpeg', wraps=run_ffmpeg) as run:
            validate_decode(self.source, FFMPEG, {'duration': 3})
        result = subprocess.run(run.call_args.args[0], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stderr, b'')
        progress = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
        self.assertEqual(progress['progress'], 'end')
        self.assertEqual(int(progress['frame']), 75)
        self.assertEqual(int(progress['drop_frames']), 0)
        self.assertEqual(int(progress['dup_frames']), 0)

    def test_corrupt_video_payload_still_fails_validation(self):
        with av.open(str(self.source)) as container:
            packet = [p for p in container.demux(video=0) if p.size][30]
        self.assertGreaterEqual(packet.pos, 0)
        self.assertGreater(packet.size, 5)
        payload = bytearray(self.source.read_bytes())
        # Preserve the MP4 container and NAL length/header; damage a slice
        # so this tests real video decoding rather than container opening.
        payload[packet.pos + 5:packet.pos + packet.size] = bytes(packet.size - 5)
        damaged = self.source.with_name('damaged.mp4')
        damaged.write_bytes(payload)
        with self.assertRaises(RuntimeError):
            validate_decode(damaged, FFMPEG, {'duration': 3})


if __name__ == '__main__':
    unittest.main()
