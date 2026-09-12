"""Optional, isolated Windows runtime for Qwen ASR and CLAP."""
from pathlib import Path
import os,subprocess


def python_path(root):return root/'runtime/neural/python.exe'


def patch_nagisa_model_loader(root):
    """Keep nagisa 0.2.11's DyNet model loading usable in Unicode install paths."""
    path=root/'runtime/neural/Lib/site-packages/nagisa/model.py'
    source=path.read_text(encoding='utf8')
    original='            model.populate(params)'
    replacement='''            # ASMR-Cliper: DyNet opens narrow filenames on Windows.
            # Python can enter a Unicode directory; the bundled model basename is ASCII.
            import os
            params_path = os.path.abspath(params)
            previous_directory = os.getcwd()
            try:
                os.chdir(os.path.dirname(params_path))
                model.populate(os.path.basename(params_path))
            finally:
                os.chdir(previous_directory)'''
    if replacement in source:return
    if source.count(original)!=1:
        raise RuntimeError('无法应用 nagisa 0.2.11 中文路径兼容修复：model.py 与预期不符。')
    path.write_text(source.replace(original,replacement),encoding='utf8')


def probe_output(value):
    return value.decode('utf8',errors='replace') if isinstance(value,bytes) else value or ''


def inspect(root,env):
    import json
    python=python_path(root)
    if not python.is_file():return False,'尚未安装',{}
    code="import torch,transformers,numpy,scipy,librosa,nagisa,soynlp,importlib.metadata,json;from qwen_asr import Qwen3ASRModel;from transformers import ClapModel;print(json.dumps({'torch':torch.__version__,'transformers':transformers.__version__,'qwen':importlib.metadata.version('qwen-asr'),'cuda':torch.cuda.is_available()}))"
    output=''
    try:
        p=subprocess.run([str(python),'-X','utf8','-c',code],env=env,capture_output=True,timeout=100,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        output='\n'.join(part.strip() for part in (probe_output(p.stdout),probe_output(p.stderr)) if part.strip())
        if p.returncode:
            return False,'依赖加载失败，需要修复',{'error':f'神经环境导入检查退出码 {p.returncode}\n{output}'}
        info=json.loads(probe_output(p.stdout).splitlines()[-1])
        ok=info['torch'].split('+')[0]=='2.9.1' and info['transformers']=='4.57.6' and info['qwen']=='0.0.6'
        return ok,('PyTorch · '+('GPU' if info['cuda'] else 'CPU')) if ok else '依赖版本需要修复',info
    except subprocess.TimeoutExpired as exc:
        return False,'依赖检测超时，需要修复',{'error':f'神经环境导入检查超时（{exc.timeout} 秒）\n{probe_output(exc.stdout)}\n{probe_output(exc.stderr)}'}
    except Exception as exc:
        return False,'依赖检测失败，需要修复',{'error':f'{type(exc).__name__}: {exc}\n{output}'}


def install(root,manifest,download,extract,emit,env,use_cuda=True,prepare_runtime=None):
    folder=root/'runtime/neural';folder.mkdir(parents=True,exist_ok=True)
    py=manifest['python'];archive=download(py['url'],root/'runtime/downloads/neural-python.zip',py['sha256'],py['size'])
    extract(archive,folder)
    # Restore app-local CRT after extraction, before any child loads its DLLs.
    if prepare_runtime is not None:prepare_runtime(folder)
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
    patch_nagisa_model_loader(root)
    ok,detail,info=inspect(root,env)
    if not ok:raise RuntimeError(detail+'\n'+info.get('error',str(info)))
    emit('log','独立音频模型运行环境安装完成')
