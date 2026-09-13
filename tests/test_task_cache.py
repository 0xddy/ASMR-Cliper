"""Exercise cache deletion on disk, including Windows locks and path boundaries."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'engine'))
from asmrclip import task_cache as cache
from asmrclip.common import read_json, save_json


class TaskCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ASMR cache 中文 ')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder / 'cache'
        self.root.mkdir()
        self.output = self.folder / 'output' / 'finished.m4a'
        self.output.parent.mkdir(); self.output.write_bytes(b'finished media')
        self.source = self.folder / 'source.m4a'; self.source.write_bytes(b'original media')
        self.cfg = {'cache_dir': str(self.root), 'input': str(self.source), 'output_dir': str(self.output.parent)}
        self.now = time.time()
        self.quiet = contextlib.redirect_stdout(io.StringIO()); self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def task(self, label='task', age=0, size=100, status='active', menu=False):
        identity = hashlib.sha256(label.encode()).hexdigest()[:24]
        path = (self.root / 'program-menus' if menu else self.root) / identity
        path.mkdir(parents=True)
        save_json(path / 'source.json', {'path': str(self.source), 'fingerprint': identity})
        save_json(path / 'task-state.json', {'status': status, 'last_used': self.now - age})
        name = 'menu-windows.json' if menu else 'analysis.wav'
        payload = path / name; payload.write_bytes(b'x' * size)
        os.utime(payload, (self.now - age, self.now - age))
        return path

    def maintain(self, **kwargs):
        return cache.maintain(self.cfg, now=self.now, min_free=0, completed=set(), **kwargs)

    def test_completed_job_cleans_ai_artifacts_and_preserves_results_models_and_unknown_files(self):
        task = self.task()
        names = ['frames.npz', 'source-audio.mp4', 'analysis.part.wav', 'speech-a' + '0' * 11 + '.jsonl',
                 'acoustic-cache.json', 'semantic-cache.json.tmp', 'extraction-evidence.json', 'drinking-review.json', 'whisper-review.json',
                 'plan-extract.json', 'review-results/' + 'a' * 64 + '.json',
                 'review-results/sounds-' + 'c' * 20 + '/acoustic-cache.json',
                 'review-results/sounds-' + 'c' * 20 + '/semantic-cache.json.tmp',
                 'review-results/chunks/' + 'b' * 64 + '.json', 'inference-test/audio.npy']
        for name in names:
            path = task / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'intermediate')
        unknown = task / 'personal.wav'; unknown.write_bytes(b'leave me')
        unknown_sound=task/'review-results/sounds-personal/semantic-cache.json'
        unknown_sound.parent.mkdir();unknown_sound.write_bytes(b'leave me too')
        model = self.root / 'models' / 'model.bin'; model.parent.mkdir(); model.write_bytes(b'model')
        for name in ('校验报告.json', '剪辑计划.json', '人声复核.csv', '节目单.json'):
            (self.output.parent / name).write_text('result', encoding='utf8')
        with cache.cache_lock(task):
            result = cache.finish(self.cfg, task, self.output)
        self.assertEqual(result['status'], 'cleaned')
        self.assertEqual(result['removed_files'], 1 + len(names))
        self.assertEqual(list(cache.payload_files(task)), [])
        self.assertEqual(unknown.read_bytes(), b'leave me'); self.assertEqual(model.read_bytes(), b'model')
        self.assertEqual(unknown_sound.read_bytes(),b'leave me too')
        self.assertEqual(self.source.read_bytes(), b'original media'); self.assertEqual(self.output.read_bytes(), b'finished media')
        self.assertTrue((self.output.parent / '人声复核.csv').is_file())
        self.assertTrue((task / '.lock').is_file())
        self.assertEqual(read_json(task / 'task-state.json')['status'], 'completed')

    def test_unpublished_output_does_not_clear_retry_data(self):
        task = self.task()
        with cache.cache_lock(task): result = cache.finish(self.cfg, task, self.folder / 'missing.m4a')
        self.assertEqual(result['status'], 'partial'); self.assertTrue((task / 'analysis.wav').exists())

    def test_recent_interrupted_tasks_survive_until_retry_and_expired_tasks_are_pruned(self):
        recent = self.task('recent', age=60)
        expired = self.task('expired', age=cache.RETENTION_SECONDS + 10)
        result = self.maintain()
        self.assertEqual(result['freed_bytes'], 100)
        self.assertTrue((recent / 'analysis.wav').exists()); self.assertFalse((expired / 'analysis.wav').exists())

    def test_capacity_evicts_oldest_inactive_tasks_first_including_menu_windows(self):
        oldest = self.task('oldest', age=300, size=120)
        second = self.task('second', age=200, size=100, menu=True)
        newest = self.task('newest', age=100, size=80)
        result = self.maintain(budget=190)
        self.assertEqual(result['freed_bytes'], 120)
        self.assertFalse((oldest / 'analysis.wav').exists()); self.assertTrue((second / 'menu-windows.json').exists())
        self.assertTrue((newest / 'analysis.wav').exists())

    def test_completed_work_is_removed_before_older_retry_data_under_capacity_pressure(self):
        retry = self.task('retry', age=300, size=80)
        finished = self.task('finished', age=20, size=100, status='completed')
        result = self.maintain(budget=100)
        self.assertEqual(result['freed_bytes'], 100)
        self.assertTrue((retry / 'analysis.wav').exists()); self.assertFalse((finished / 'analysis.wav').exists())

    def test_receipt_write_failure_preserves_actual_freed_byte_count(self):
        task = self.task()
        with cache.cache_lock(task), patch.object(cache, 'save_json', side_effect=OSError('receipt locked')):
            result = cache.finish(self.cfg, task, self.output)
        self.assertEqual(result['freed_bytes'], 100); self.assertEqual(result['status'], 'partial')
        self.assertTrue(self.output.exists())

    def test_active_lock_and_current_retry_exclusion_are_never_evicted(self):
        active = self.task('active', age=cache.RETENTION_SECONDS + 100)
        retry = self.task('retry', age=cache.RETENTION_SECONDS + 50)
        inactive = self.task('inactive', age=cache.RETENTION_SECONDS + 10)
        with cache.cache_lock(active): result = self.maintain(budget=0, exclude=(retry,))
        self.assertEqual(result['active_skipped'], 1)
        self.assertTrue((active / 'analysis.wav').exists()); self.assertTrue((retry / 'analysis.wav').exists())
        self.assertFalse((inactive / 'analysis.wav').exists())

    def test_disk_pressure_reclaims_inactive_caches_before_the_age_limit(self):
        task = self.task()
        usage = type('Usage', (), {'free': 0})()
        with patch.object(cache.shutil, 'disk_usage', return_value=usage):
            result = cache.maintain(self.cfg, now=self.now, min_free=1024, completed=set())
        self.assertEqual(result['freed_bytes'], 100); self.assertFalse((task / 'analysis.wav').exists())

    def test_completed_receipt_cleans_leftovers_and_legacy_history_requires_no_new_retry(self):
        finished = self.task('finished', status='completed')
        legacy = self.task('legacy'); (legacy / 'task-state.json').unlink()
        retry = self.task('retry')
        result = cache.maintain(self.cfg, now=self.now, min_free=0, completed={legacy.name, retry.name})
        self.assertEqual(result['freed_bytes'], 200)
        self.assertFalse((finished / 'analysis.wav').exists()); self.assertFalse((legacy / 'analysis.wav').exists())
        self.assertTrue((retry / 'analysis.wav').exists())

    def test_source_output_and_outside_cache_paths_are_protected(self):
        task = self.task()
        with cache.cache_lock(task):
            self.assertTrue(cache.purge(self.root, task, (task / 'analysis.wav',))['errors'])
        self.assertTrue((task / 'analysis.wav').exists())
        outside = self.folder / ('a' * 24); outside.mkdir(); (outside / 'analysis.wav').write_bytes(b'outside')
        self.assertTrue(cache.purge(self.root, outside)['errors'])
        self.assertTrue((outside / 'analysis.wav').exists())

    def test_directory_junctions_are_not_followed(self):
        task = self.task()
        outside = self.folder / 'outside'; outside.mkdir()
        payload = outside / ('a' * 64 + '.json'); payload.write_bytes(b'not a cache')
        link = task / 'review-results'
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        else: link.symlink_to(outside, target_is_directory=True)
        with cache.cache_lock(task): cache.finish(self.cfg, task, self.output)
        self.assertEqual(payload.read_bytes(), b'not a cache')
        self.assertTrue(link.exists())

    def test_locked_file_cleanup_failure_does_not_fail_the_finished_task(self):
        task = self.task(); original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name == 'analysis.wav': raise PermissionError('locked')
            return original(path, *args, **kwargs)
        with cache.cache_lock(task), patch.object(Path, 'unlink', unlink):
            result = cache.finish(self.cfg, task, self.output)
        self.assertEqual(result['status'], 'partial'); self.assertTrue(self.output.exists())
        self.maintain(); self.assertFalse((task / 'analysis.wav').exists())

    def test_failed_pipeline_releases_mmap_but_preserves_analysis_for_retry(self):
        import wave
        from asmrclip.pipeline import run
        source = self.folder / 'source.wav'
        with wave.open(str(source), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00' * 32000)
        recognizer = MagicMock(); recognizer.scan.return_value = {'spoken': [], 'language': 'en'}
        classifier = MagicMock(); classifier.music_intervals.return_value = []
        classifier.exclusions.return_value = {name: [] for name in
            ('voice', 'soft_laugh', 'heartbeat', 'tapping', 'loud_laugh', 'airflow', 'drinking', 'impacts')}
        with patch('asmrclip.model_catalog.validate_models'), \
             patch('asmrclip.recognition.Recognizer', return_value=recognizer), \
             patch('asmrclip.classifier.Classifier', return_value=classifier), \
             patch('asmrclip.transitions.review_transitions', return_value=([], {'candidates': []})), \
             patch('asmrclip.drinking.review_drinking', return_value=([], {'candidates': []})), \
             patch('asmrclip.whispering.detect', return_value=([], {'intervals': []})), \
             patch('asmrclip.planner.make_plan', side_effect=RuntimeError('planning interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'planning interrupted'):
                run({**self.cfg, 'input': str(source), 'review_enabled': False, 'generate_program_menu': False})
        task = self.root / cache.fingerprint(source)
        self.assertTrue((task / 'analysis.wav').exists()); self.assertTrue((task / 'frames.npz').exists())
        self.assertEqual(cache.state(task)['status'], 'active')
        self.maintain(); self.assertTrue((task / 'analysis.wav').exists())
        # A Windows rename proves the interrupted pipeline released its mmap.
        (task / 'analysis.wav').replace(task / 'analysis.part.wav')
        self.assertTrue((task / 'analysis.part.wav').exists())


if __name__ == '__main__': unittest.main()
