"""Local Qwen/CLAP worker. Only this process imports optional PyTorch packages."""
import json,sys,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
sys.stdout.reconfigure(encoding='utf8');sys.stderr.reconfigure(encoding='utf8')


def emit(kind,**data):print(json.dumps({'type':kind,**data},ensure_ascii=False),flush=True)


def load(kind,cfg):
    import torch
    device=cfg['device']
    if device=='auto':device='cuda' if torch.cuda.is_available() else 'cpu'
    if device=='cuda' and not torch.cuda.is_available():raise RuntimeError('独立音频模型运行环境未检测到 CUDA，请修复依赖或选择 CPU。')
    torch.set_num_threads(6)
    emit('log',message=f'载入 {kind} · {device.upper()}')
    if kind=='qwen':
        from qwen_asr import Qwen3ASRModel
        model=Qwen3ASRModel.from_pretrained(str(ROOT/'models/qwen-asr'),dtype=torch.bfloat16 if device=='cuda' else torch.float32,
            device_map=device,local_files_only=True,attn_implementation='sdpa',max_inference_batch_size=1,max_new_tokens=256,
            forced_aligner=str(ROOT/'models/qwen-aligner'),forced_aligner_kwargs={'dtype':torch.bfloat16 if device=='cuda' else torch.float32,
                'device_map':device,'local_files_only':True,'attn_implementation':'sdpa'})
        return model,None,device
    from transformers import ClapModel,ClapProcessor
    model=ClapModel.from_pretrained(str(ROOT/'models/clap'),local_files_only=True).to(device).eval()
    processor=ClapProcessor.from_pretrained(str(ROOT/'models/clap'),local_files_only=True)
    return model,processor,device


def transcribe(model,req):
    import numpy as np
    audio=np.load(req['audio']);rows=[]
    for i,clip in enumerate(req['clips']):
        a,b=clip['start'],clip['end'];part=audio[max(0,round(a*16000)):round(b*16000)]
        if len(part)<1600:
            emit('work_progress',done=i+1,total=len(req['clips']))
            continue
        result=model.transcribe(audio=(part,16000),language=req.get('language'),return_time_stamps=True)[0]
        words=[{'start':float(w.start_time)+a,'end':float(w.end_time)+a,'word':w.text} for w in result.time_stamps or [] if w.end_time>w.start_time]
        rows.append({'text':result.text,'language':result.language,'words':words,'backend':'qwen3-asr',
            'start':min((w['start'] for w in words),default=a),'end':max((w['end'] for w in words),default=b)})
        emit('work_progress',done=i+1,total=len(req['clips']))
    return rows


def classify(model,processor,device,req):
    import numpy as np,torch
    from scipy.signal import resample_poly
    audio=np.load(req['audio']);arrays=[]
    for a,b in req['clips']:
        part=audio[max(0,round(a*16000)):round(b*16000)]
        arrays.append(resample_poly(part,3,1).astype(np.float32))
    with torch.inference_mode():
        inputs=processor(text=req['prompts'],audios=arrays,return_tensors='pt',padding=True,sampling_rate=48000).to(device)
        result=model(**inputs)
        scores=(result.audio_embeds@result.text_embeds.T).cpu().float().numpy()
    return scores.tolist()


def main():
    model=processor=device=None
    for line in sys.stdin:
        try:
            req=json.loads(line);op=req['op']
            if op=='quit':return
            if op=='load':
                model,processor,device=load(req['kind'],req['cfg']);result={'device':device}
            elif op=='transcribe':result=transcribe(model,req)
            elif op=='classify':result=classify(model,processor,device,req)
            else:raise ValueError('未知模型请求')
            emit('response',ok=True,result=result)
        except Exception as e:
            traceback.print_exc(file=sys.stderr);emit('response',ok=False,error=str(e))

if __name__=='__main__':main()
