import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from asmrclip.common import settings
from asmrclip.planner import make_plan, scene_guard


def probe(start, end, kind):
    return {'start': start, 'end': end,
            'texture': .8 if kind == 'texture' else .01,
            'breath': .2 if kind == 'breath' else 0.,
            'speech': .9 if kind == 'voice' else .1}


class SceneGuardTests(unittest.TestCase):
    def probes(self, side):
        spans = [(10, 12), (12, 14), (14, 16)] if side == 'start' else [
            (88, 90), (86, 88), (84, 86)]
        return [probe(a, b, kind) for (a, b), kind in zip(spans, ['voice', 'voice', 'breath'])]

    def test_limited_breath_cannot_restore_strong_voice(self):
        for side, edge, expected in [('start', 10, 14), ('end', 90, 86)]:
            with self.subTest(side=side):
                guard, _ = scene_guard(self.probes(side), edge, side, tail_limit=2)
                self.assertEqual(guard, expected)

    def test_breath_without_strong_voice_stays_limited(self):
        for side, edge, expected in [('start', 10, 12), ('end', 90, 88)]:
            with self.subTest(side=side):
                guard, _ = scene_guard([self.probes(side)[-1]], edge, side, tail_limit=2)
                self.assertEqual(guard, expected)

    def test_unlimited_breath_still_extends_guard(self):
        for side, edge, expected in [('start', 10, 16), ('end', 90, 84)]:
            with self.subTest(side=side):
                guard, _ = scene_guard(self.probes(side), edge, side)
                self.assertEqual(guard, expected)

    def test_plan_keeps_asmr_without_restoring_voice_at_either_boundary(self):
        class BoundaryClassifier:
            def windows(self, spans):
                return [probe(a, b, 'voice' if a < 14 or b > 86 else
                              'breath' if a < 16 or b > 84 else 'texture') for a, b in spans]

        meta = {'frame_samples': 1024, 'sample_rate': 10240}
        frames = {'levels': np.full((1000, 2), .08, np.float32)}
        speech = {'spoken': [[0, 10], [90, 100]], 'accepted': []}
        exclusions = {'extraction': {'intervals': [[10, 90]]}}
        for mode in ('relaxed', 'extract'):
            with self.subTest(mode=mode):
                cfg = settings({'mode': mode, 'join_fade_enabled': True})
                plan = make_plan(meta, frames, speech, [], cfg, BoundaryClassifier(), exclusions)
                self.assertGreater(plan['duration'], 65)
                dt = plan['frame_seconds']
                for a, b in plan['keep_frames']:
                    self.assertGreaterEqual(a * dt, 14)
                    self.assertLessEqual(b * dt, 86)


if __name__ == '__main__':
    unittest.main()
