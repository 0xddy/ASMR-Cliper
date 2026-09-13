import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from asmrclip.model_catalog import required_components


@unittest.skipUnless(os.name=='nt' and shutil.which('powershell.exe'), 'Windows PowerShell required')
class EnvironmentBootstrapTests(unittest.TestCase):
    def test_no_python_inspection_matches_engine_model_requirements(self):
        defaults=json.loads((ROOT/'config/defaults.json').read_text(encoding='utf-8'))
        base={**defaults,'mode':'relaxed','speech_model':'qwen3-asr','review_model_id':'qwen3-asr',
              'review_enabled':False,'keep_whisper':False,'keep_drinking':True}
        cases=[
            ('Qwen review enabled',{'review_enabled':True},(),True),
            ('Qwen scan without review',{},(),False),
            ('whisper retention default',{},('keep_whisper',),True),
            ('drinking exclusion default',{},('keep_drinking',),True),
            ('V4 mandatory review',{'mode':'extract'},(),True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir()
            shutil.copyfile(ROOT/'config/environment.json',root/'config/environment.json')
            config=root/'config/defaults.json'
            for name,overrides,omitted,needs_clap in cases:
                with self.subTest(case=name):
                    cfg={**base,**overrides}
                    for key in omitted:cfg.pop(key,None)
                    config.write_text(json.dumps(cfg),encoding='utf-8')
                    result=subprocess.run([
                        'powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                        '-File',str(ROOT/'scripts/environment.ps1'),'-Action','inspect',
                        '-Root',str(root),'-Config',str(config)],
                        capture_output=True,text=True,encoding='utf-8',timeout=20,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode,0,result.stderr or result.stdout)
                    events=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                    self.assertFalse(any(row['type']=='error' for row in events),events)
                    report=next(row for row in events if row['type']=='environment')
                    required={key for key,row in report['components'].items() if row['required']}
                    self.assertEqual('clap' in required,needs_clap)
                    self.assertEqual(required,required_components(cfg))
                    self.assertFalse(report['ready'])
                    self.assertEqual(report['components']['python']['status'],'missing')
                    self.assertEqual(report['components']['clap']['status'],'missing')
                    # Inspection of a clean installation must not bootstrap a
                    # runtime or fetch any models just to report dependencies.
                    self.assertFalse((root/'runtime').exists())


if __name__=='__main__':unittest.main()
