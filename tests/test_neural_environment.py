"""Neural runtime installation and diagnostics without downloads or GPU dependencies."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import neural_environment as neural

MODEL_SOURCE = '''class Model:
    def __init__(self, model, params):
        if params:
            model.populate(params)
'''


class NeuralEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ASMR neural 中文 ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = self.root / 'runtime/neural'
        self.runtime.mkdir(parents=True)
        (self.runtime / 'python.exe').touch()
        self.model_source = self.runtime / 'Lib/site-packages/nagisa/model.py'

    def prepare_model(self, source=MODEL_SOURCE):
        self.model_source.parent.mkdir(parents=True)
        self.model_source.write_text(source, encoding='utf8')

    def test_model_load_uses_relative_filename_and_preserves_cwd(self):
        self.prepare_model()
        params = self.model_source.parent / 'data/nagisa_v001.model'
        params.parent.mkdir()
        params.write_bytes(b'model weights')
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)

        def populate(filename):
            if not filename.isascii():
                raise RuntimeError('Could not read model from ' + filename)
            self.assertEqual(Path(filename).read_bytes(), b'model weights')
            self.assertEqual(Path.cwd(), params.parent)

        scope = {}
        exec(MODEL_SOURCE, scope)
        with self.assertRaisesRegex(RuntimeError, 'Could not read model'):
            scope['Model'](Mock(populate=populate), str(params))

        neural.patch_nagisa_model_loader(self.root)
        patched = self.model_source.read_text(encoding='utf8')
        neural.patch_nagisa_model_loader(self.root)
        self.assertEqual(self.model_source.read_text(encoding='utf8'), patched)
        exec(patched, scope)
        scope['Model'](Mock(populate=populate), str(params))
        self.assertEqual(Path.cwd(), original_cwd)
        with self.assertRaisesRegex(RuntimeError, 'invalid weights'):
            scope['Model'](Mock(populate=Mock(side_effect=RuntimeError('invalid weights'))), str(params))
        self.assertEqual(Path.cwd(), original_cwd)

    def test_unrecognized_nagisa_source_fails_without_modifying_it(self):
        self.prepare_model('unexpected upstream code')
        with self.assertRaisesRegex(RuntimeError, 'model.py'):
            neural.patch_nagisa_model_loader(self.root)
        self.assertEqual(self.model_source.read_text(encoding='utf8'), 'unexpected upstream code')

    def test_probe_preserves_process_diagnostics(self):
        cases = [('python exception', 1, b'Importing nagisa\n', b'Traceback:\nRuntimeError: Could not read model\n',
                  ('退出码 1', 'Importing nagisa', 'Traceback:', 'Could not read model')),
                 ('native crash', 3221225477, b'', b'', ('3221225477',)),
                 ('invalid report', 0, b'invalid version report', b'native diagnostic',
                  ('invalid version report', 'native diagnostic'))]
        for name, code, stdout, stderr, expected in cases:
            with self.subTest(case=name), patch.object(neural.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], code, stdout, stderr)):
                ok, detail, info = neural.inspect(self.root, {})
                self.assertFalse(ok)
                self.assertIn('依赖加载失败' if code else '依赖检测失败', detail)
                for text in expected:
                    self.assertIn(text, info['error'])

    def test_probe_preserves_timeout_output_and_startup_errors(self):
        errors = (subprocess.TimeoutExpired('python', 100, output=b'loading torch', stderr=b'stalled'),
                  OSError('missing runtime DLL'))
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(neural.subprocess, 'run', side_effect=error):
                ok, _, info = neural.inspect(self.root, {})
            self.assertFalse(ok)
            if isinstance(error, subprocess.TimeoutExpired):
                for text in ('100', 'loading torch', 'stalled'):
                    self.assertIn(text, info['error'])
            else:
                self.assertIn('missing runtime DLL', info['error'])

    def test_probe_accepts_cuda_build_on_runner_without_gpu(self):
        output = b'{"torch":"2.9.1+cu128","transformers":"4.57.6","qwen":"0.0.6","cuda":false}\n'
        with patch.object(neural.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, b'')):
            ok, _, info = neural.inspect(self.root, {})
        self.assertTrue(ok)
        self.assertFalse(info['cuda'])

    def test_install_prepares_crt_before_children_and_patches_before_inspection(self):
        self.prepare_model()
        manifest = {key: {'url': key, 'sha256': 'hash', 'size': 1} for key in ('python', 'pip')}
        dll = self.runtime / 'vcruntime140.dll'

        def extract(archive, folder):
            if folder == self.runtime:
                dll.write_bytes(b'embedded CRT')
                (folder / 'python312._pth').touch()
            else:
                self.model_source.write_text(MODEL_SOURCE, encoding='utf8')

        def prepare(folder):
            self.assertEqual(dll.read_bytes(), b'embedded CRT')
            (folder / dll.name).write_bytes(b'complete VS CRT')

        def start_child(*args, **kwargs):
            self.assertEqual(dll.read_bytes(), b'complete VS CRT')
            return Mock(stdout=[b'pip completed\n'], wait=Mock(return_value=0))

        def inspect(root, env):
            self.assertEqual(dll.read_bytes(), b'complete VS CRT')
            self.assertIn('os.path.basename(params_path)', self.model_source.read_text(encoding='utf8'))
            return False, '依赖加载失败，需要修复', {'error': 'specific native import error'}

        prepare_runtime = Mock(side_effect=prepare)
        emit = Mock()
        with (patch.object(neural.subprocess, 'Popen', side_effect=start_child) as popen,
              patch.object(neural, 'inspect', side_effect=inspect)):
            with self.assertRaisesRegex(RuntimeError, 'specific native import error'):
                neural.install(self.root, manifest, lambda url, path, *args: path, extract, emit, {},
                               prepare_runtime=prepare_runtime)
        prepare_runtime.assert_called_once_with(self.runtime)
        self.assertEqual(popen.call_count, 3)
        self.assertIn('https://download.pytorch.org/whl/cu128', popen.call_args_list[0].args[0])
        emit.assert_any_call('log', 'pip completed')


if __name__ == '__main__':
    unittest.main()
