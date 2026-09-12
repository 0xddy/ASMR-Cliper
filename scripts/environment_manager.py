import argparse
import contextlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))
from download_support import download,fetch_text,sha256,proxy_value,extract_checked
from asmrclip.model_catalog import required_components
import neural_environment

sys.stdout.reconfigure(encoding='utf8',errors='replace')
sys.stderr.reconfigure(encoding='utf8',errors='replace')
ROOT=Path(__file__).resolve().parents[1]
CONFIG={}
LABELS={'python':'Python 运行时','dependencies':'分析依赖','whisper':'Whisper Turbo','ast':'声音分类模型','review':'Whisper large-v3','ffmpeg':'FFmpeg','gpu':'GPU 加速',
        'qwen':'Qwen3-ASR-1.7B','aligner':'Qwen 时间定位','clap':'ASMR 声音识别','neural':'Qwen / ASMR 识别依赖'}


def emit(kind,message='',**values):
    proxy=CONFIG.get('proxy_url','')
    if proxy:message=message.replace(proxy,'[已配置的代理]')
    print(json.dumps({'type':kind,'message':message,**values},ensure_ascii=False),flush=True)


def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8');os.replace(temp,path)


def project_path(relative):
    result=(ROOT/relative).resolve()
    if not result.is_relative_to(ROOT.resolve()):raise ValueError('环境组件路径必须位于项目目录内。')
    return result


def model_path(asset):
    # A model's download destination is independent of its selected role.
    return project_path(asset['path'])


def component(key,status,detail,**fields):
    required=key in required_components(CONFIG)
    value={'id':key,'name':LABELS[key],'status':status,'detail':detail,'required':required,**fields}
    emit('component',**value)
    return value


def child_environment(for_download=True):
    env=os.environ.copy()
    for key in list(env):
        if key.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy','pip_proxy'):env.pop(key,None)
    proxy=proxy_value(CONFIG) if for_download else ''
    if proxy:env.update(HTTP_PROXY=proxy,HTTPS_PROXY=proxy,PIP_PROXY=proxy)
    env.update(PYTHONIOENCODING='utf-8',PYTHONUTF8='1',PIP_DISABLE_PIP_VERSION_CHECK='1',TRANSFORMERS_VERBOSITY='error')
    return env


def probe_dependencies():
    code=r'''
import sys,json,importlib,importlib.metadata
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'engine'))
from asmrclip.common import configure_dlls
configure_dlls()
result={'errors':[],'cuda_devices':0,'versions':{}}
for name in ['av','numpy','scipy','faster_whisper','ctranslate2','onnxruntime','transformers']:
 try:
  module=importlib.import_module(name);result['versions'][name]=getattr(module,'__version__','available')
 except Exception as e:result['errors'].append(name+': '+str(e))
try:
 for line in (Path(sys.argv[1])/'engine/requirements.txt').read_text().splitlines():
  if '==' not in line:continue
  package,expected=line.strip().split('==')
  try:
   actual=importlib.metadata.version(package)
   if actual!=expected:result['errors'].append(package+': '+actual+' -> '+expected)
  except importlib.metadata.PackageNotFoundError:result['errors'].append(package+': missing')
 if result['versions'].get('onnxruntime')!='1.23.2':result['errors'].append('ONNX Runtime imported files need repair')
except Exception as e:result['errors'].append(str(e))
try:
 import ctranslate2
 result['cuda_devices']=ctranslate2.get_cuda_device_count()
except Exception:pass
print(json.dumps(result))
'''
    p=subprocess.run([sys.executable,'-X','utf8','-c',code,str(ROOT)],capture_output=True,timeout=100,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env=child_environment(False))
    for line in reversed(p.stdout.decode('utf8',errors='replace').splitlines()):
        try:return json.loads(line)
        except ValueError:pass
    return {'errors':['无法加载分析依赖，请下载或修复运行环境。'],'cuda_devices':0,'versions':{}}


def inspect_environment(manifest,base=0,span=100):
    emit('progress','检查本地运行环境',progress=base)
    result=[]
    py_ok=sys.version_info[:2]==(3,12)
    result.append(component('python','ready' if py_ok else 'error',f'Python {sys.version.split()[0]} · 64 位',path=sys.executable))
    component('dependencies','checking','正在导入依赖与检查版本…')
    try:probe=probe_dependencies()
    except subprocess.TimeoutExpired:probe={'errors':['依赖加载超时，请重试检测或修复依赖。'],'cuda_devices':0}
    result.append(component('dependencies','ready' if not probe['errors'] else 'missing','依赖版本与加载检查通过' if not probe['errors'] else f"发现 {len(probe['errors'])} 项缺失或版本问题，请下载 / 修复",errors=probe['errors']))
    for error in probe['errors']:emit('log',error)
    emit('progress','验证模型文件完整性',progress=base+span*.25)
    stamp_path=ROOT/'runtime/environment-hashes.json'
    try:stamps=json.loads(stamp_path.read_text(encoding='utf8'))
    except (OSError,ValueError):stamps={}
    for key in ['whisper','ast','review','qwen','aligner','clap']:
        component(key,'checking','正在校验模型文件…')
        bad=[];total=0
        for asset in (a for a in manifest['assets'] if a['component']==key):
            path=model_path(asset);total+=asset['size'];identity=str(path)
            if not path.is_file() or path.stat().st_size!=asset['size']:
                bad.append(path.name);continue
            stat=path.stat();stamp=[stat.st_size,stat.st_mtime_ns,asset['sha256']]
            if stamps.get(identity)!=stamp:
                if sha256(path)!=asset['sha256']:bad.append(path.name);continue
                stamps[identity]=stamp
        if total==0:bad.append('下载清单缺失')
        detail=f'{total/(1024**3):.2f} GB · 完整性校验通过' if not bad else f'约 {total/(1024**3):.2f} GB · 尚未安装完整'
        result.append(component(key,'ready' if not bad else 'missing',detail,missing=bad))
    save(stamp_path,stamps)
    neural_ok,neural_detail,neural_info=neural_environment.inspect(ROOT,child_environment(False))
    result.append(component('neural','ready' if neural_ok else 'missing',neural_detail,versions=neural_info))
    ffmpeg=project_path(CONFIG.get('ffmpeg','runtime/tools/ffmpeg.exe'))
    good=False;version='尚未安装'
    if ffmpeg.is_file():
        try:
            p=subprocess.run([str(ffmpeg),'-version'],capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            good=p.returncode==0;version=p.stdout.decode('utf8',errors='replace').splitlines()[0][:120]
        except Exception:version='文件存在，但无法启动'
    result.append(component('ffmpeg','ready' if good else 'missing',version))
    gpu=probe.get('cuda_devices',0)
    result.append(component('gpu','ready' if gpu else 'optional',f'检测到 {gpu} 个 NVIDIA GPU' if gpu else '可使用 CPU 剪辑，无需安装显卡驱动'))
    ready=all(c['status']=='ready' for c in result if c['required'])
    record={'components':result,'ready':ready,'checked_at':time.strftime('%Y-%m-%d %H:%M:%S'),'python':sys.executable}
    save(ROOT/'runtime/environment-status.json',record)
    emit('environment','环境已就绪' if ready else '检测完成，有组件需要下载或修复',progress=base+span,**record)
    return record


def file_download(url,path,expected_hash=None,expected_size=None,base=40,span=45,label=None):
    label=label or path.name
    def progress(done,total,speed,cached):
        value=base+span*done/total if total else base
        emit('download',f'{label} · {done/1048576:.1f} / {total/1048576:.1f} MB'+(' · 已复用' if cached else ''),
             progress=value,filename=label,downloaded=done,total=total,bytes_per_second=speed)
    return download(url,path,CONFIG,expected_hash,expected_size,progress)


def install_dependencies(manifest):
    try:import pip
    except ImportError:
        component('dependencies','downloading','准备依赖安装器…')
        wheel=file_download(manifest['pip']['url'],ROOT/'runtime/downloads/pip-25.1.1.whl',manifest['pip']['sha256'],manifest['pip']['size'],10,6)
        extract_checked(wheel,Path(sys.prefix)/'Lib/site-packages')
    component('dependencies','downloading','正在下载并安装分析依赖…')
    commands=[['-m','pip','install','--only-binary',':all:','--force-reinstall','--progress-bar','off','--retries','3','--timeout','35','-r',str(ROOT/'engine/requirements.txt')],
              ['-m','pip','install','--progress-bar','off','--no-deps','--force-reinstall','onnxruntime-gpu==1.23.2']]
    for index,args in enumerate(commands):
        emit('progress','安装分析依赖，请稍候…',progress=18+index*12)
        p=subprocess.Popen([sys.executable,'-X','utf8',*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            env=child_environment(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for raw in p.stdout:
            line=raw.decode('utf8',errors='replace').strip()
            if line:emit('log',line)
        if p.wait():raise RuntimeError('依赖安装未完成。请检查网络或代理，然后重新下载缺失项。')
    component('dependencies','ready','分析依赖安装完成')


def install_ffmpeg(manifest):
    component('ffmpeg','downloading','获取 FFmpeg 及发布者校验值…')
    checksum=fetch_text(manifest['ffmpeg']['checksum_url'],CONFIG,8192).strip().split()[0].lower()
    if not re.fullmatch('[0-9a-f]{64}',checksum):raise ValueError('FFmpeg 发布者校验值无效。')
    archive=file_download(manifest['ffmpeg']['url'],ROOT/'runtime/downloads/ffmpeg-essentials.zip',checksum,base=88,span=8)
    dest=project_path(CONFIG.get('ffmpeg','runtime/tools/ffmpeg.exe'));dest.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names=[n for n in z.namelist() if n.endswith('/bin/ffmpeg.exe')]
        if len(names)!=1:raise ValueError('FFmpeg 安装包目录不符合预期。')
        temp=dest.with_suffix('.download.exe')
        with z.open(names[0]) as src,temp.open('wb') as output:shutil.copyfileobj(src,output)
        p=subprocess.run([str(temp),'-version'],capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if p.returncode:raise RuntimeError('下载的 FFmpeg 无法启动。')
        os.replace(temp,dest)
        for name in z.namelist():
            if Path(name).name.lower() in ('license','license.txt'):
                (dest.parent/'FFmpeg-LICENSE.txt').write_bytes(z.read(name));break
    component('ffmpeg','ready','FFmpeg 下载与启动校验通过')


@contextlib.contextmanager
def installation_lock():
    import msvcrt
    path=ROOT/'runtime/environment-install.lock';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise RuntimeError('已有另一个环境任务正在运行。') from None
        try:yield
        finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


def installation_components(status,requested):
    states={c['id']:c for c in status['components']}
    selected={c['id'] for c in states.values() if c['required'] and c['status']!='ready'} if requested=='all' else {requested}
    if 'qwen' in selected:selected.update(('aligner','neural'))
    if 'clap' in selected:selected.add('neural')
    return {key for key in selected if key in states and states[key]['status']!='ready'}


def install(manifest,requested):
    proxy_value(CONFIG)
    with installation_lock():
        status=inspect_environment(manifest,0,8)
        selected=installation_components(status,requested)
        if 'dependencies' in selected:install_dependencies(manifest)
        if 'neural' in selected:
            component('neural','downloading','安装独立的音频模型运行环境…')
            try:
                import ctranslate2
                cuda=ctranslate2.get_cuda_device_count()>0
            except Exception:cuda=False
            neural_environment.install(ROOT,manifest,file_download,extract_checked,emit,child_environment(),cuda)
        assets=[a for a in manifest['assets'] if a['component'] in selected]
        total=sum(a['size'] for a in assets) or 1;done=0
        for asset in assets:
            component(asset['component'],'downloading','下载并校验 '+Path(asset['path']).name)
            file_download(asset['url'],model_path(asset),asset['sha256'],asset['size'],42+44*done/total,44*asset['size']/total)
            done+=asset['size']
        if 'ffmpeg' in selected:install_ffmpeg(manifest)
        final=inspect_environment(manifest,96,4)
        failed=[c['name'] for c in final['components'] if c['id'] in selected and c['status']!='ready']
        if failed:raise RuntimeError('安装后检测仍未通过：'+'、'.join(failed)+'。请查看日志并重试。')
        emit('setup_complete','所选缺失组件已补齐' if selected else '现有组件有效，无需重复下载',ready=final['ready'],progress=100)


def test_proxy():
    proxy_value(CONFIG)
    targets=[('Python 下载站','https://www.python.org/ftp/python/3.12.10/'),('模型下载站','https://huggingface.co/api/models/mobiuslabsgmbh/faster-whisper-large-v3-turbo'),('依赖下载站','https://pypi.org/pypi/pip/25.1.1/json')]
    checks=[]
    for i,(name,url) in enumerate(targets):
        emit('progress','正在测试 '+name,progress=i/len(targets)*100)
        start=time.monotonic()
        try:
            fetch_text(url,CONFIG,2*1024*1024);checks.append({'name':name,'ok':True,'milliseconds':round((time.monotonic()-start)*1000)})
        except Exception as exc:checks.append({'name':name,'ok':False,'error':str(exc)})
    ok=all(c['ok'] for c in checks)
    emit('proxy_result','连接测试通过，下载源均可访问' if ok else '部分下载源连接失败，请检查代理设置',ok=ok,checks=checks,progress=100)
    return ok


def main():
    global ROOT,CONFIG
    parser=argparse.ArgumentParser();parser.add_argument('--action',choices=['inspect','install','testproxy'],required=True)
    parser.add_argument('--config',type=Path,required=True);parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--component',choices=['all','python','dependencies','whisper','ast','review','ffmpeg','qwen','aligner','clap','neural'],default='all')
    args=parser.parse_args();ROOT=args.root.resolve()
    try:
        CONFIG=json.loads(args.config.read_text(encoding='utf-8-sig'))
        manifest=json.loads((ROOT/'config/environment.json').read_text(encoding='utf8'))
        if args.action=='inspect':inspect_environment(manifest)
        elif args.action=='install':install(manifest,args.component)
        else:return 0 if test_proxy() else 2
        return 0
    except Exception as exc:
        emit('error',str(exc));return 1


if __name__=='__main__':raise SystemExit(main())
