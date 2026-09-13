"""Application-only distribution checks; no model downloads or Python environment installs."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_win64_app as app

COMMIT = 'a' * 40


class AppPackagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ASMR app 中文 ')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.source = self.folder / 'source'
        self.native = self.folder / 'native'
        self.root = self.folder / 'relocated app 中文'
        for name in (*app.SOURCE_FILES, 'CMakeLists.txt',
                     'engine/main.py', 'engine/neural_worker.py', 'engine/asmrclip/__init__.py'):
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        (self.source / 'docs/APP_README.md').write_text('# Application package fixture\n', encoding='utf8')
        self.native.mkdir()
        (self.native / 'asmrcliper.exe').write_bytes(b'native application')
        app.save(self.native / 'build-source.json', {'commit': COMMIT, 'version': app.version(self.source),
                 'exe_sha256': app.sha256(self.native / 'asmrcliper.exe').upper()})
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'GITHUB_SHA': COMMIT, 'GITHUB_RUN_ID': '123456',
                               'GITHUB_OUTPUT': '', 'ASMRCLIP_BUILD_PROXY': ''}).start()

    def ffmpeg_zip(self, missing=None):
        archive = self.folder / 'ffmpeg.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            for name in ('bin/ffmpeg.exe', 'bin/ffprobe.exe', 'LICENSE', 'README.txt'):
                if name != missing:
                    package.writestr('ffmpeg-test/' + name, ('FFmpeg upstream ' + name).encode('utf8'))
            package.writestr('ffmpeg-test/bin/ffplay.exe', b'unneeded binary')
            package.writestr('../../outside.txt', b'must never be extracted')
        return archive

    def install_ffmpeg(self, missing=None):
        archive = self.ffmpeg_zip(missing)
        digest = app.sha256(archive)
        with patch.object(app, 'fetch_text', return_value=digest + '  ffmpeg.zip') as fetch, \
                patch.object(app, 'download', return_value=archive) as download:
            app.install_ffmpeg(self.root, self.folder / 'downloads')
            self.assertEqual(download.call_args.kwargs['expected_hash'], digest)
            self.assertEqual(fetch.call_count, 1)

    def prepare(self):
        app.prepare(self.source, self.native, self.root)

    def test_archive_contains_program_and_tools_without_environments_or_private_files(self):
        unwanted = ('runtime/python/python.exe', 'runtime/neural/python.exe', 'runtime/venv/Scripts/python.exe',
                    'runtime/cache/private.wav', 'runtime/downloads/model.zip', 'runtime/jobs/private.json',
                    'models/whisper/model.bin', 'config/user.json', 'config/history.json',
                    'engine/__pycache__/main.pyc', 'engine/private.json', 'build/output.exe')
        for name in unwanted:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'private source file')
        self.prepare()
        self.install_ffmpeg()
        for name in unwanted:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'incidental GUI test state')
        info = app.read(self.root / 'build-info.json')
        info['verification'] = {'passed': True}
        app.save(self.root / 'build-info.json', info)
        output = self.folder / 'github-output.txt'
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            archive = app.archive(self.root, self.folder / 'artifacts')
        with zipfile.ZipFile(archive) as package:
            names = {name.removeprefix('ASMR-Cliper/') for name in package.namelist()}
            self.assertFalse(set(unwanted) & names)
            self.assertTrue({'asmrcliper.exe', 'engine/main.py', 'scripts/environment.ps1',
                             'engine/neural-requirements.txt', 'runtime/tools/ffmpeg.exe',
                             'runtime/tools/ffprobe.exe', 'runtime/tools/FFmpeg-LICENSE.txt'} <= names)
            self.assertFalse(any(name.endswith('ffplay.exe') for name in names))
            self.assertIsNone(package.testzip())
            info = json.loads(package.read('ASMR-Cliper/build-info.json'))
            self.assertEqual(info['target'], 'win64-app')
            self.assertFalse(info['includes_python'])
            self.assertFalse(info['includes_models'])
        self.assertEqual(archive.with_suffix('.zip.sha256').read_text().split()[0], app.sha256(archive))
        self.assertIn('archive_name=' + archive.name, output.read_text(encoding='utf8'))
        self.assertFalse((self.folder / 'outside.txt').exists())

    def test_staging_removes_machine_paths_and_download_proxy(self):
        cfg = app.read(self.source / 'config/defaults.json')
        cfg.update(input='C:/private.wav', output='C:/private-output', ffmpeg='C:/custom/ffmpeg.exe',
                   proxy_enabled=True, proxy_url='http://user:password@host:1234')
        app.save(self.source / 'config/defaults.json', cfg)
        self.prepare()
        clean = app.read(self.root / 'config/defaults.json')
        self.assertFalse(clean['proxy_enabled'])
        self.assertEqual(clean['proxy_url'], '')
        self.assertEqual(clean['ffmpeg'], 'runtime/tools/ffmpeg.exe')
        self.assertNotIn('input', clean)
        self.assertNotIn('output', clean)
        self.assertEqual(app.read(self.source / 'config/defaults.json'), cfg)

    def test_native_checksum_and_source_revision_must_match(self):
        original = app.read(self.native / 'build-source.json')
        for field, value in (('commit', 'b' * 40), ('version', '9.9.9'), ('exe_sha256', '0' * 64)):
            with self.subTest(field=field):
                app.save(self.native / 'build-source.json', {**original, field: value})
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertFalse(self.root.exists())

    def test_invalid_publisher_checksum_stops_before_download(self):
        self.prepare()
        with patch.object(app, 'fetch_text', return_value='invalid checksum'), patch.object(app, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'publisher checksum'):
                app.install_ffmpeg(self.root, self.folder / 'downloads')
            download.assert_not_called()

    def test_missing_ffprobe_rejects_incomplete_distribution(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'bin/ffprobe.exe'):
            self.install_ffmpeg(missing='bin/ffprobe.exe')

    def test_unverified_or_wrong_target_cannot_be_archived(self):
        self.prepare()
        for target, passed in (('win64-app', False), ('win64-nv', True)):
            info = app.read(self.root / 'build-info.json')
            info.update(target=target, verification={'passed': passed})
            app.save(self.root / 'build-info.json', info)
            with self.assertRaisesRegex(ValueError, 'verified'):
                app.archive(self.root, self.folder / 'artifacts')
        self.assertFalse((self.folder / 'artifacts').exists())


if __name__ == '__main__':
    unittest.main()
