"""JSON protocol to the optional isolated PyTorch process."""
import json,os,subprocess,tempfile
from pathlib import Path
import numpy as np
from .common import ROOT,event


class NeuralClient:
    def __init__(self,kind,cfg):
        python=ROOT/'runtime/neural/python.exe'
        if not python.is_file():raise RuntimeError('请在运行环境中安装 Qwen / ASMR 识别依赖。')
        scratch=ROOT/'runtime/neural-jobs';scratch.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='inference-',dir=scratch)
        env=os.environ.copy();env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',PYTHONIOENCODING='utf-8')
        self.process=subprocess.Popen([str(python),'-X','utf8',str(ROOT/'engine/neural_worker.py')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf8',errors='replace',bufsize=1,
            env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:self.info=self.request({'op':'load','kind':kind,'cfg':cfg})
        except Exception:self.close();raise

    def request(self,data,audio=None):
        if audio is not None:
            path=Path(self.temp.name)/'audio.npy';np.save(path,np.asarray(audio,dtype=np.float32));data={**data,'audio':str(path)}
        self.process.stdin.write(json.dumps(data,ensure_ascii=False)+'\n');self.process.stdin.flush()
        for line in self.process.stdout:
            try:response=json.loads(line)
            except ValueError:
                if line.strip():event('log',line.strip())
                continue
            if response.get('type')=='response':
                if not response.get('ok'):raise RuntimeError(response.get('error','音频模型推理失败'))
                return response.get('result')
            if response.get('type')=='work_progress':
                from .progress import advance
                advance(response['done'],response['total'])
            elif response.get('type')=='progress':event('progress',response.get('message',''),response.get('progress'))
            elif response.get('message'):event('log',response['message'])
        raise RuntimeError('音频模型进程提前结束，请查看运行日志。')

    def close(self):
        p=getattr(self,'process',None)
        if p:
            if p.poll() is None:
                try:p.stdin.write('{"op":"quit"}\n');p.stdin.flush();p.wait(timeout=8)
                except Exception:p.terminate();p.wait(timeout=8)
            p.stdin.close();p.stdout.close();self.process=None
        temp=getattr(self,'temp',None)
        if temp:temp.cleanup();self.temp=None

    def __del__(self):
        try:self.close()
        except Exception:pass
