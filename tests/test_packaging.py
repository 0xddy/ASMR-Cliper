"""Portable distribution tests; no network or installed GPU is required."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import package_win64_nv as bundle


class PackagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ASMR package 中文 ')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.root = self.folder / 'payload'
        self.root.mkdir()

    def put(self, path, value=b'test'):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        return target

    def test_all_models_required_and_manifest_paths_safe(self):
        manifest = bundle.read(ROOT / 'config/environment.json')
        self.assertEqual({a['component'] for a in bundle.assets(manifest)}, bundle.MODEL_COMPONENTS)
        missing = copy.deepcopy(manifest)
        missing['assets'] = [a for a in missing['assets'] if a['component'] != 'clap']
        with self.assertRaises(ValueError): bundle.assets(missing)
        for bad in ('models/../../secret.bin', 'C:/secret.bin', 'models/../secret.bin', 'models/evil\\file.bin'):
            data = copy.deepcopy(manifest)
            data['assets'][0]['path'] = bad
            with self.assertRaises(ValueError): bundle.assets(data)

    def test_settings_remove_machine_paths_and_proxy(self):
        cfg = bundle.read(ROOT / 'config/defaults.json')
        cfg.update(input='F:/personal.mp4', output='G:/output', ffmpeg='C:/ffmpeg.exe', proxy_url='http://name:password@host:123')
        cleaned = bundle.portable_config(cfg)
        self.assertNotIn('input', cleaned)
        self.assertNotIn('output', cleaned)
        self.assertEqual(cleaned['proxy_url'], '')
        self.assertFalse(cleaned['proxy_enabled'])
        self.assertEqual(cleaned['ffmpeg'], 'runtime/tools/ffmpeg.exe')
        self.assertEqual(cfg['input'], 'F:/personal.mp4')

    def test_package_excludes_user_files_downloads_and_venvs(self):
        wanted = ('asmrcliper.exe', 'runtime/python/python.exe', 'runtime/neural/python.exe',
                  'runtime/tools/ffmpeg.exe', 'runtime/python/Lib/site-packages/torch.dist-info/LICENSE',
                  'models/qwen-asr/model.safetensors', 'config/defaults.json', 'engine/main.py')
        unwanted = ('runtime/venv/Scripts/python.exe', 'runtime/python/Scripts/pip.exe', 'runtime/environment-status.json',
                    'runtime/environment-hashes.json', 'runtime/cache/private.wav', 'runtime/downloads/model.zip',
                    'runtime/jobs/secret.json', 'config/user.json', 'config/history.json', 'output/private.m4a',
                    'engine/__pycache__/main.pyc', 'models/incomplete.bin.part', 'ASMR-Cliper.lnk', 'build/private.txt')
        for name in wanted + unwanted: self.put(name)
        actual = {r.as_posix() for _, r in bundle.package_files(self.root)}
        self.assertEqual(actual, set(wanted))

    def test_install_refuses_system_python_or_developer_venv(self):
        with self.assertRaisesRegex(RuntimeError, 'staged'):
            bundle.assert_core_python(self.root)

    def make_crt(self):
        crt = self.folder / 'Microsoft.VC143.CRT'
        crt.mkdir()
        for name in ('msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140_2.dll'):
            (crt / name).write_bytes(('VS redistributable: ' + name).encode('ascii'))
        return crt

    def test_prepare_replaces_embedded_crt_before_starting_staged_python(self):
        crt = self.make_crt()
        source = self.root
        for name in bundle.SOURCE_FILES:
            self.put(name, (ROOT / name).read_bytes())
        archive = self.folder / 'python.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('python.exe', b'embedded interpreter')
            package.writestr('vcruntime140.dll', b'older embedded CRT')
        manifest = bundle.read(source / 'config/environment.json')
        manifest['python'].update(sha256=bundle.sha256(archive), size=archive.stat().st_size)
        bundle.save(source / 'config/environment.json', manifest)
        target = self.folder / 'prepared payload 中文'

        bundle.prepare(source, target, archive, crt)

        for dll in crt.glob('*.dll'):
            self.assertEqual((target / 'runtime/python' / dll.name).read_bytes(), dll.read_bytes())
        self.assertEqual((target / 'runtime/python/python.exe').read_bytes(), b'embedded interpreter')

    def test_install_preserves_locked_core_crt_and_restores_neural_crt(self):
        crt = self.make_crt()
        core = self.root / 'runtime/python'
        bundle.copy_crt(crt, core)
        self.put('config/defaults.json', b'{}')
        bundle.save(self.root / 'config/environment.json', bundle.read(ROOT / 'config/environment.json'))
        self.put('build-info.json', b'{}')
        self.put('runtime/downloads/ffmpeg-essentials.zip', b'FFmpeg archive')
        manager, neural = Mock(), Mock()
        # Extracting neural Python replaces its bundled CRT; install must restore the VS set.
        def install_neural(*args, prepare_runtime, **kwargs):
            self.put('runtime/neural/vcruntime140.dll', b'older embedded CRT')
            prepare_runtime(self.root / 'runtime/neural')
            for dll in crt.glob('*.dll'):
                self.assertEqual((self.root / 'runtime/neural' / dll.name).read_bytes(), dll.read_bytes())
        neural.install.side_effect = install_neural
        copy_file = shutil.copy2

        def copy_unlocked(source, target):
            if Path(target).parent == core:
                raise PermissionError('WinError 32: running core Python has locked its CRT')
            return copy_file(source, target)

        # Stub network/package work while preserving actual CRT copies and build-info output.
        with (patch.dict(sys.modules, {'environment_manager': manager, 'neural_environment': neural}),
              patch.object(sys, 'executable', str(core / 'python.exe')),
              patch.object(bundle, 'assets', return_value=[]),
              patch.object(bundle.shutil, 'copy2', side_effect=copy_unlocked),
              patch.object(bundle.subprocess, 'run', return_value=Mock(stdout='[]'))):
            bundle.install(self.root, crt)

        manager.install_dependencies.assert_called_once()
        neural.install.assert_called_once()
        for name in ('python', 'neural'):
            for dll in crt.glob('*.dll'):
                self.assertEqual((self.root / 'runtime' / name / dll.name).read_bytes(), dll.read_bytes())
        self.assertEqual(bundle.read(self.root / 'build-info.json')['crt'],
                         [{'name': dll.name, 'sha256': bundle.sha256(dll)} for dll in sorted(crt.glob('*.dll'))])

    def test_offline_probes_cannot_use_developer_python_cuda_or_proxy(self):
        environment = {'SystemRoot': 'C:\\Windows', 'PYTHONPATH': 'G:\\private', 'PYTHONHOME': 'G:\\python',
                       'CUDA_PATH': 'C:\\CUDA', 'HTTPS_PROXY': 'http://private', 'PATH': 'C:\\CUDA\\bin',
                       'ASMRCLIP_BUILD_PROXY': 'http://name:password@host:123'}
        with patch.dict(os.environ, environment, clear=True):
            env = bundle.clean_env(self.root, offline=True)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('PYTHONHOME', env)
        self.assertNotIn('CUDA_PATH', env)
        self.assertNotIn('HTTPS_PROXY', env)
        self.assertNotIn('CUDA', env['PATH'])
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')

    def test_archive_needs_validation_and_output_outside_payload(self):
        bundle.save(self.root / 'build-info.json', {'version':'0.6.0'})
        with self.assertRaisesRegex(ValueError, 'verification'):
            bundle.archive(self.root, self.folder / 'out')
        with self.assertRaisesRegex(ValueError, 'outside'):
            bundle.archive(self.root, self.root / 'nested')

    def test_archive_roundtrip_preserves_bytes_and_relative_layout(self):
        self.put('asmrcliper.exe', bytes(range(256)) * 20)
        self.put('runtime/python/python.exe', b'embedded')
        self.put('runtime/neural/python.exe', b'cuda')
        self.put('config/user.json', b'PRIVATE')
        self.put('runtime/environment-status.json', b'ABSOLUTE PATH')
        bundle.save(self.root / 'build-info.json', {'version':'0.6.0','verification':{'passed':True}})
        with patch.dict(os.environ, {'GITHUB_OUTPUT':'', 'GITHUB_STEP_SUMMARY':''}):
            bundle.archive(self.root, self.folder / 'out')
        archive = self.folder / 'out/ASMR-Cliper-0.6.0-win64-nv.zip'
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read('ASMR-Cliper/asmrcliper.exe'), bytes(range(256)) * 20)
            self.assertNotIn('ASMR-Cliper/config/user.json', z.namelist())
            self.assertNotIn('ASMR-Cliper/runtime/environment-status.json', z.namelist())
            self.assertTrue(all(n.startswith('ASMR-Cliper/') and '\\' not in n for n in z.namelist()))
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertTrue(archive.with_suffix('.zip.sha256').read_text().startswith(expected + '  '))
        with self.assertRaises(FileExistsError): bundle.archive(self.root, self.folder / 'out')

    def test_path_guard_blocks_parent_directory(self):
        with self.assertRaises(ValueError): bundle.inside(self.root, '../outside')
        self.assertEqual(bundle.inside(self.root, 'models/file.bin'), self.root / 'models/file.bin')

    def test_snapshot_can_be_reused_without_staging_paths_or_private_files(self):
        self.put('models/asmr/weights.bin', b'model contents')
        self.put('runtime/python/Lib/site-packages/numpy/.libs/runtime.dll', b'native library')
        self.put('runtime/downloads/cached.zip', b'cache')
        self.put('runtime/python/Scripts/pip.exe', b'absolute launcher path')
        self.put('config/user.json', b'private')
        target = self.folder / 'download-artifact'
        bundle.snapshot(self.root, target)
        self.assertEqual((target/'models/asmr/weights.bin').read_bytes(), b'model contents')
        self.assertTrue((target/'runtime/python/Lib/site-packages/numpy/.libs/runtime.dll').exists())
        self.assertFalse((target/'runtime/downloads').exists())
        self.assertFalse((target/'config/user.json').exists())
        self.assertFalse((target/'runtime/python/Scripts').exists())
        # Artifact content remains usable even after the original job's staging file disappears.
        (self.root/'models/asmr/weights.bin').unlink()
        self.assertEqual((target/'models/asmr/weights.bin').read_bytes(), b'model contents')

    def test_mode_help_files_are_part_of_the_payload(self):
        for name in ('strict-v2.txt','relaxed-v3.txt','extract-v4.txt'):
            relative = 'docs/prompts/' + name
            self.assertIn(relative, bundle.SOURCE_FILES)
            self.assertTrue((ROOT / relative).is_file())


if __name__ == '__main__':
    unittest.main()
