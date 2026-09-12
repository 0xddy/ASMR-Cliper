"""Release lifecycle tests use a fake GitHub API; no public release is created."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import publish_release as release

COMMIT = 'a' * 40
REPOSITORY = 'example/ASMR-Cliper'


class FakeGitHub:
    def __init__(self):
        self.release = None
        self.contents = {}
        self.uploads = []
        self.creates = 0
        self.publishes = 0
        self.fail_asset = None
        self.corrupt_upload = False

    def __call__(self, *args, **kwargs):
        if args[:2] == ('release', 'view'):
            return json.dumps({'databaseId': 123}) if self.release else None
        if args[0] == 'api':
            if args[1] != f'repos/{REPOSITORY}/releases/123':
                raise AssertionError('Drafts must be read by release ID.')
            return json.dumps(self.release)
        if args[:2] == ('release', 'create'):
            assert self.release is None and '--draft' in args
            self.creates += 1
            self.release = {'draft': True, 'target_commitish': args[args.index('--target') + 1],
                            'assets': [], 'html_url': f'https://github.com/{REPOSITORY}/releases/tag/{args[2]}'}
            return self.release['html_url']
        if args[:2] == ('release', 'upload'):
            assert self.release['draft']
            path = Path(args[3])
            self.uploads.append(path.name)
            if self.fail_asset == path.name:
                raise RuntimeError('simulated connection failure')
            data = path.read_bytes()
            self.contents[path.name] = data
            asset = {'name': path.name, 'size': len(data), 'state': 'uploaded',
                     'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
            if self.corrupt_upload:
                asset['digest'] = 'sha256:' + '0' * 64
            self.release['assets'] = [a for a in self.release['assets'] if a['name'] != path.name] + [asset]
            return ''
        if args[:2] == ('release', 'edit'):
            assert '--draft=false' in args and self.release['draft']
            self.release['draft'] = False
            self.publishes += 1
            return ''
        raise AssertionError(args)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ASMR release 中文 ')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.archive = self.folder / 'ASMR-Cliper-0.6.13-win64-nv.zip'
        self.checksum = self.archive.with_suffix('.zip.sha256')
        self.build_info = {'version': '0.6.13', 'commit': COMMIT, 'verification': {'passed': True}}
        self.write_archive()
        self.github = FakeGitHub()
        self.addCleanup(patch.stopall)
        patch.object(release, 'gh', self.github).start()
        patch.object(release.time, 'sleep').start()
        patch.dict(os.environ, {'GITHUB_OUTPUT': '', 'GITHUB_STEP_SUMMARY': ''}).start()

    def write_archive(self):
        with zipfile.ZipFile(self.archive, 'w') as package:
            package.writestr('ASMR-Cliper/build-info.json', json.dumps(self.build_info))
            package.writestr('ASMR-Cliper/asmrcliper.exe', bytes(range(256)) * 8)
        self.checksum.write_text(f'{release.sha256(self.archive)}  {self.archive.name}\n', encoding='ascii')

    def publish(self):
        return release.publish(self.archive, self.checksum, REPOSITORY, COMMIT, '123456', part_size=512)

    def test_publish_checks_parts_and_only_then_exposes_release(self):
        url = self.publish()
        self.assertTrue(url.endswith('/v0.6.13-win64-nv-123456'))
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.github.publishes, 1)
        self.assertFalse(self.github.release['draft'])
        manifest = json.loads(self.github.contents[self.archive.name + '.parts.json'])
        joined = b''.join(self.github.contents[p['name']] for p in manifest['parts'])
        self.assertEqual(joined, self.archive.read_bytes())
        self.assertEqual(manifest['archive']['sha256'], hashlib.sha256(joined).hexdigest())
        for part in manifest['parts']:
            self.assertLessEqual(part['size'], 512)
            self.assertEqual(part['sha256'], hashlib.sha256(self.github.contents[part['name']]).hexdigest())
        self.assertIn('merge-win64-nv.ps1', self.github.contents)
        self.assertIn(self.archive.name + '.sha256', self.github.contents)
        self.assertFalse(list(self.folder.glob('release-assets-*')))

    def test_failed_upload_leaves_draft_and_retry_skips_verified_parts(self):
        first = self.archive.name + '.001'
        self.github.fail_asset = self.archive.name + '.002'
        with self.assertRaisesRegex(RuntimeError, 'connection failure'):
            self.publish()
        self.assertTrue(self.github.release['draft'])
        self.assertEqual(self.github.publishes, 0)
        self.github.fail_asset = None
        self.publish()
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.github.uploads.count(first), 1)
        self.assertEqual(self.github.publishes, 1)
        uploads = list(self.github.uploads)
        self.publish()
        self.assertEqual(self.github.uploads, uploads)
        self.assertEqual(self.github.publishes, 1)

    def test_bad_remote_digest_never_publishes(self):
        self.github.corrupt_upload = True
        with self.assertRaisesRegex(RuntimeError, 'verification failed'):
            self.publish()
        self.assertTrue(self.github.release['draft'])
        self.assertEqual(self.github.publishes, 0)

    def test_published_assets_cannot_be_overwritten(self):
        self.publish()
        self.github.release['assets'][0]['digest'] = 'sha256:' + '0' * 64
        uploads = list(self.github.uploads)
        with self.assertRaisesRegex(RuntimeError, 'already published'):
            self.publish()
        self.assertEqual(self.github.uploads, uploads)

    def test_wrong_checksum_or_unverified_archive_cannot_create_release(self):
        self.checksum.write_text('0' * 64 + '  ' + self.archive.name, encoding='ascii')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            self.publish()
        for info in ({**self.build_info, 'commit': 'b' * 40},
                     {**self.build_info, 'verification': {'passed': False}}):
            self.build_info = info
            self.write_archive()
            with self.assertRaisesRegex(ValueError, 'exact source revision'):
                self.publish()
        self.assertEqual(self.github.creates, 0)

    def test_existing_release_for_other_commit_is_rejected(self):
        self.github.release = {'draft': True, 'target_commitish': 'b' * 40, 'assets': []}
        with self.assertRaisesRegex(ValueError, 'different source revision'):
            self.publish()
        self.assertFalse(self.github.uploads)

    def app_archive(self, extra=None):
        self.archive = self.folder / 'ASMR-Cliper-0.6.13-win64-app.zip'
        self.checksum = self.archive.with_suffix('.zip.sha256')
        self.build_info.update(target='win64-app', includes_python=False, includes_models=False)
        self.write_archive()
        with zipfile.ZipFile(self.archive, 'a') as package:
            for name in ('engine/main.py', 'runtime/tools/ffmpeg.exe', 'runtime/tools/ffprobe.exe'):
                package.writestr('ASMR-Cliper/' + name, b'application file')
            if extra:
                package.writestr('ASMR-Cliper/' + extra, b'unwanted runtime')
        self.checksum.write_text(f'{release.sha256(self.archive)}  {self.archive.name}\n', encoding='ascii')

    def publish_app(self):
        return release.publish(self.archive, self.checksum, REPOSITORY, COMMIT, '123456', target='win64-app')

    def test_app_release_uploads_direct_zip_and_checksum_without_parts(self):
        self.app_archive()
        url = self.publish_app()
        self.assertTrue(url.endswith('/v0.6.13-win64-app-123456'))
        self.assertEqual(set(self.github.contents), {self.archive.name, self.archive.name + '.sha256'})
        self.assertEqual(self.github.contents[self.archive.name], self.archive.read_bytes())
        self.assertEqual(self.github.publishes, 1)
        uploads = list(self.github.uploads)
        self.publish_app()
        self.assertEqual(self.github.uploads, uploads)
        self.assertEqual(self.github.publishes, 1)

    def test_app_release_failed_checksum_upload_can_resume_verified_zip(self):
        self.app_archive()
        self.github.fail_asset = self.archive.name + '.sha256'
        with self.assertRaisesRegex(RuntimeError, 'connection failure'):
            self.publish_app()
        self.assertTrue(self.github.release['draft'])
        self.assertEqual(self.github.publishes, 0)
        self.github.fail_asset = None
        self.publish_app()
        self.assertEqual(self.github.uploads.count(self.archive.name), 1)
        self.assertEqual(self.github.publishes, 1)

    def test_app_release_rejects_bundled_models_and_environments(self):
        for name in ('runtime/python/python.exe', 'models/whisper/model.bin', 'runtime/downloads/source.zip'):
            self.app_archive(extra=name)
            with self.assertRaisesRegex(ValueError, 'exclude Python/model'):
                self.publish_app()
        self.assertEqual(self.github.creates, 0)

    def test_release_part_size_must_be_below_github_limit(self):
        for size in (0, release.ASSET_LIMIT):
            with self.subTest(size=size), self.assertRaises(ValueError):
                release.publish(self.archive, self.checksum, REPOSITORY, COMMIT, '123456', part_size=size)
        self.assertEqual(self.github.creates, 0)

    def test_splitter_keeps_only_one_part_and_cleans_up_on_failure(self):
        parts_folder = self.folder / 'parts'
        parts_folder.mkdir()
        parts = release.split_archive(self.archive, parts_folder, 512)
        for index in range(2):
            part, identity = next(parts)
            self.assertEqual(list(parts_folder.iterdir()), [part])
            self.assertEqual(identity['sha256'], release.sha256(part))
        parts.close()
        self.assertEqual(list(parts_folder.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows merge helper integration')
    def test_windows_merge_roundtrip_and_corrupt_parts(self):
        self.publish()
        downloads = self.folder / '下载目录'
        downloads.mkdir()
        for name, data in self.github.contents.items():
            (downloads / name).write_bytes(data)
        command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                   str(downloads / 'merge-win64-nv.ps1')]

        def merge():
            return subprocess.run(command, capture_output=True, timeout=60)

        merged = downloads / self.archive.name
        result = merge()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(merged.read_bytes(), self.archive.read_bytes())
        self.assertEqual(merge().returncode, 0)  # Re-running a successful merge is harmless.
        merged.write_bytes(b'existing output')
        self.assertNotEqual(merge().returncode, 0)
        self.assertEqual(merged.read_bytes(), b'existing output')
        merged.unlink()
        first = downloads / (self.archive.name + '.001')
        original = first.read_bytes()
        first.write_bytes(b'x' * len(original))
        result = merge()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'SHA-256 mismatch', result.stderr)
        self.assertFalse(merged.exists())
        self.assertFalse(merged.with_suffix('.zip.joining').exists())
        first.unlink()
        self.assertNotEqual(merge().returncode, 0)
        self.assertFalse(merged.exists())


if __name__ == '__main__':
    unittest.main()
