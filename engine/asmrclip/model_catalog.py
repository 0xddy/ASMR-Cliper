"""Stable model IDs shared by settings, inference and environment management."""
from pathlib import Path

VOICE_MODELS={
    'whisper-large-v3':{'name':'Whisper large-v3','component':'review','path':'models/whisper-review','backend':'whisper'},
    'qwen3-asr':{'name':'Qwen3-ASR-1.7B','component':'qwen','path':'models/qwen-asr','backend':'qwen'},
    'whisper-turbo':{'name':'Whisper large-v3-turbo','component':'whisper','path':'models/whisper-turbo','backend':'whisper'},
}


def required_components(cfg):
    required={'python','dependencies','ast','ffmpeg'}
    ids=[cfg.get('speech_model','whisper-large-v3')]
    reviewing=cfg.get('review_enabled',True) or cfg.get('mode')=='extract'
    if reviewing:ids.append(cfg.get('review_model_id','whisper-large-v3'))
    for name in ids:
        if name not in VOICE_MODELS:raise ValueError('未知语音模型：'+str(name))
        required.add(VOICE_MODELS[name]['component'])
        if name=='qwen3-asr':required.update(('aligner','neural'))
    qwen_review=reviewing and cfg.get('review_model_id','whisper-large-v3')=='qwen3-asr'
    if cfg.get('mode')=='extract' or not cfg.get('keep_drinking',False) or cfg.get('keep_whisper',True) or qwen_review:
        required.update(('clap','neural'))
    return required


def voice_path(cfg,root,review=False):
    key='review_model_id' if review else 'speech_model'
    model=cfg.get(key,'whisper-large-v3')
    if model not in VOICE_MODELS:raise ValueError('未知语音模型：'+str(model))
    return Path(root)/VOICE_MODELS[model]['path']


def model_signature(path):
    path=Path(path)
    files=[p for p in path.rglob('*') if p.is_file() and p.suffix in ('.bin','.safetensors','.json','.txt') and p.name!='README.md']
    if not files:raise RuntimeError('模型尚未安装：'+str(path))
    return [(str(p.relative_to(path)),p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(files)]


def validate_models(cfg,root):
    import json
    required=required_components(cfg)
    manifest=json.loads((root/'config/environment.json').read_text(encoding='utf8'))
    missing=[]
    for asset in manifest['assets']:
        if asset['component'] not in required:continue
        p=root/asset['path']
        if not p.is_file() or p.stat().st_size!=asset['size']:missing.append(asset['component'])
    if 'neural' in required and not (root/'runtime/neural/python.exe').is_file():missing.append('neural')
    if missing:raise RuntimeError('所选模型尚未就绪，请在运行环境中下载 / 修复：'+', '.join(sorted(set(missing))))
