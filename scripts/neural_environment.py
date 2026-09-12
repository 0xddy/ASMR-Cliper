"""Optional, isolated Windows runtime for Qwen ASR and CLAP."""
from pathlib import Path
import os,subprocess


def python_path(root):return root/'runtime/neural/python.exe'


def inspect(root,env):
    import json
    python=python_path(root)
    if not python.is_file():return False,'尚未安装',{}
    code="import torch,transformers,numpy,scipy,librosa,nagisa,soynlp,importlib.metadata,json;from qwen_asr import Qwen3ASRModel;from transformers import ClapModel;print(json.dumps({'torch':torch.__version__,'transformers':transformers.__version__,'qwen':importlib.metadata.version('qwen-asr'),'cuda':torch.cuda.is_available()}))"
    try:
        p=subprocess.run([str(python),'-X','utf8','-c',code],env=env,capture_output=True,timeout=100,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if p.returncode:return False,'依赖加载失败，需要修复',{}
        info=json.loads(p.stdout.decode('utf8').splitlines()[-1])
        ok=info['torch'].split('+')[0]=='2.9.1' and info['transformers']=='4.57.6' and info['qwen']=='0.0.6'
        return ok,('PyTorch · '+('GPU' if info['cuda'] else 'CPU')) if ok else '依赖版本需要修复',info
    except Exception:return False,'依赖检测失败，需要修复',{}


def install(root,manifest,download,extract,emit,env,use_cuda=True):
    folder=root/'runtime/neural';folder.mkdir(parents=True,exist_ok=True)
    py=manifest['python'];archive=download(py['url'],root/'runtime/downloads/neural-python.zip',py['sha256'],py['size'])
    extract(archive,folder)
    for p in folder.glob('python*._pth'):p.write_text('python312.zip\n.\nLib/site-packages\nimport site\n',encoding='utf8')
    pip=manifest['pip'];wheel=download(pip['url'],root/'runtime/downloads/pip-25.1.1.whl',pip['sha256'],pip['size'])
    extract(wheel,folder/'Lib/site-packages')
    python=python_path(root)
    index='https://download.pytorch.org/whl/'+('cu128' if use_cuda else 'cpu')
    commands=[['torch==2.9.1','--index-url',index],['--only-binary',':all:','-r',str(root/'engine/neural-requirements.txt')],['--no-deps','qwen-asr==0.0.6']]
    for args in commands:
        p=subprocess.Popen([str(python),'-X','utf8','-m','pip','install','--no-warn-script-location','--progress-bar','off','--retries','3','--timeout','40',*args],
            env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for raw in p.stdout:
            line=raw.decode('utf8',errors='replace').strip()
            if line:emit('log',line)
        if p.wait():raise RuntimeError('Qwen / ASMR 识别依赖安装失败，请检查日志后重试。')
    ok,detail,_=inspect(root,env)
    if not ok:raise RuntimeError(detail)
    emit('log','独立音频模型运行环境安装完成')
