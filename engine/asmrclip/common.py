import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DLL_HANDLES = []


def event(kind, message='', progress=None, **values):
    record = {'type': kind, 'message': message, **values}
    if kind == 'progress':
        from .progress import legacy_update
        structured = legacy_update(message, progress)
        if structured is not None:
            record['task_progress'] = structured
    if progress is not None:
        record['progress'] = round(progress, 2)
    print(json.dumps(record, ensure_ascii=False), flush=True)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def merge(intervals, gap=0):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a - result[-1][1] <= gap:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def complement(intervals, end):
    result, cursor = [], 0
    for a, b in merge([[max(0, a), min(end, b)] for a, b in intervals]):
        if a > cursor:
            result.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < end:
        result.append([cursor, end])
    return result


def configure_dlls():
    import sys
    os.environ.setdefault('TRANSFORMERS_VERBOSITY', 'error')
    locations = list((Path(sys.prefix) / 'Lib/site-packages/nvidia').glob('*/bin'))
    if os.environ.get('CUDA_PATH'):
        locations.append(Path(os.environ['CUDA_PATH']) / 'bin')
    locations += list(Path('C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA').glob('*/bin'))
    for p in locations:
        if p.is_dir():
            os.environ['PATH'] = str(p) + os.pathsep + os.environ.get('PATH', '')
            if hasattr(os, 'add_dll_directory'):
                DLL_HANDLES.append(os.add_dll_directory(str(p)))


def fingerprint(source):
    p = Path(source).resolve()
    stat = p.stat()
    # Cache identity is metadata-based; export independently hashes every copied packet.
    raw = json.dumps([str(p), stat.st_size, stat.st_mtime_ns, 2])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def settings(data):
    from .exclusions import KEEP_DEFAULTS
    defaults = read_json(ROOT / 'config/defaults.json')
    cfg = {**defaults, **data}
    if cfg.get('output_kind','auto') not in ('auto','audio','video'):
        raise ValueError('未知输出类型。')
    from .model_catalog import required_components,voice_path
    required_components(cfg)
    if cfg.get('review_model_id')=='whisper-turbo':raise ValueError('成片复核请选择完整版 Whisper 或 Qwen。')
    cfg['whisper_model']=str(voice_path(cfg,ROOT))
    cfg['review_model']=str(voice_path(cfg,ROOT,review=True))
    for key,default in KEEP_DEFAULTS.items():
        cfg.setdefault(key,default)
        if not isinstance(cfg[key],bool): raise ValueError(f'{key} 必须为布尔值。')
    if cfg['mode'] not in ('strict', 'relaxed', 'extract'):
        raise ValueError('未知剪辑模式。')
    if not isinstance(cfg['review_enabled'],bool):raise ValueError('review_enabled 必须为布尔值。')
    if not isinstance(cfg['generate_program_menu'],bool):raise ValueError('generate_program_menu 必须为布尔值。')
    if not isinstance(cfg['join_fade_enabled'],bool):raise ValueError('join_fade_enabled 必须为布尔值。')
    if type(cfg['review_max_passes']) is not int or not 1<=cfg['review_max_passes']<=5:raise ValueError('复核轮数必须为 1 到 5 的整数。')
    for name, low, high in [('strict_pre', 0, 60), ('strict_post', 0, 60),
                            ('strict_min_section', 1, 600), ('strict_dense_gap', 0, 120),
                            ('silence_db', -90, -20), ('silence_seconds', 2.3, 120), ('join_fade_seconds', .05, 2)]:
        cfg[name] = float(cfg[name])
        if not low <= cfg[name] <= high:
            raise ValueError(f'{name} 超出允许范围 {low}..{high}')
    for name in ['whisper_model', 'review_model', 'ast_model', 'ffmpeg', 'cache_dir']:
        p = Path(cfg[name])
        cfg[name] = str(p if p.is_absolute() else ROOT / p)
    if cfg['device'] not in ('auto', 'cpu', 'cuda'):
        raise ValueError('未知计算设备。')
    if cfg['language'] not in ('auto', 'ko', 'ja', 'zh', 'en'):
        raise ValueError('不支持的语言设置。')
    return cfg
