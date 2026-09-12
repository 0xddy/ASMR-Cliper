import contextlib
import gc
import os
from pathlib import Path

from .common import configure_dlls, event, fingerprint, read_json, save_json, settings


def doctor(cfg):
    configure_dlls()
    import av
    import ctranslate2
    import onnxruntime as ort
    import faster_whisper
    from .model_catalog import validate_models
    from .common import ROOT
    validate_models(cfg,ROOT)
    checks=[]
    for label,path in [('语音模型',Path(cfg['whisper_model'])),('声学模型',Path(cfg['ast_model'])/'onnx/model.onnx'),('FFmpeg',Path(cfg['ffmpeg']))]:
        checks.append({'name':label,'ok':path.exists(),'path':str(path)})
    if cfg.get('review_enabled',True) or cfg['mode']=='extract':
        path=Path(cfg['review_model'])
        checks.append({'name':'成片复核模型','ok':path.is_dir(),'path':str(path)})
    missing=[x['name'] for x in checks if not x['ok']]
    if missing:
        raise ValueError('缺少：'+', '.join(missing)+'。请运行 scripts/setup-runtime.ps1。')
    event('doctor','环境文件与依赖检查通过',100,checks=checks,cuda_devices=ctranslate2.get_cuda_device_count(),
          onnx_providers=ort.get_available_providers(),python=os.sys.executable)


@contextlib.contextmanager
def cache_lock(cache):
    import msvcrt
    cache.mkdir(parents=True,exist_ok=True)
    with (cache/'.lock').open('a+b') as f:
        if f.tell()==0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            raise RuntimeError('同一音频已有另一个任务正在处理。') from None
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


def run(data):
    cfg=settings(data)
    source=Path(cfg['input']).resolve()
    if not source.is_file():
        raise FileNotFoundError('请选择存在的音频或视频文件。')
    configure_dlls()
    from scipy.io import wavfile
    from .analysis import inspect_audio
    from .audio_source import analyze_input_audio
    from .recognition import Recognizer
    from .classifier import Classifier
    from .planner import make_plan
    from .exporter import export
    from .reviewer import Reviewer, SpeechRemaining, output_to_source, mapped_output_to_source, validate_review_model
    from .media import inspect_media
    from .model_catalog import validate_models
    from .common import ROOT
    media=inspect_media(source,cfg.get('output_kind','auto'))
    event('log','输出视频，复制源视频与音频编码；切点按关键帧向内调整。' if media['kind']=='video' else '输出音频，复制原编码。')
    if media['audio_tracks']>1:event('log',f'检测到 {media["audio_tracks"]} 个音轨：本次只分析并输出第一个音轨。')
    validate_models(cfg,ROOT)
    if cfg['review_enabled'] or cfg['mode']=='extract':validate_review_model(cfg)
    inspect_audio(source)
    identity=fingerprint(source)
    cache=Path(cfg['cache_dir'])/identity
    with cache_lock(cache):
        save_json(cache/'source.json',{'path':str(source),'fingerprint':identity})
        meta,frames=analyze_input_audio(source,cache,cfg,media)
        recognizer=Recognizer(cfg)
        try:speech=recognizer.scan(cache/'analysis.wav',cfg,cache)
        finally:recognizer.close()
        _,pcm=wavfile.read(cache/'analysis.wav',mmap=True)
        event('progress','识别背景音乐与声音动作',61)
        classifier=Classifier(cfg,pcm,cache)
        music=classifier.music_intervals(meta['analysis_duration'])
        event('progress','检查人声、笑声、休息与突兀撞击',65)
        exclusions=classifier.exclusions(speech,music,meta['analysis_duration'])
        if cfg['mode']=='extract':
            from .semantic import confirm
            event('progress','确认 ASMR 声音与连续动作',66)
            exclusions['extraction']=confirm(cfg,pcm,cache,classifier,speech,music,exclusions,meta['analysis_duration'])
            exclusions['extraction'].pop('records',None)
            save_json(cache/'exclusions-latest.json',exclusions)
        event('log',f'检出：{len(exclusions["voice"])} 处人声，{len(exclusions["soft_laugh"])} 处轻笑，{len(exclusions["heartbeat"])} 处心跳，{len(exclusions["tapping"])} 处道具敲击，{len(exclusions["loud_laugh"])} 处大笑，{len(exclusions["airflow"])} 处呼气/烟雾类气流动作，{len(exclusions["drinking"])} 处疑似饮水休息，{len(exclusions["impacts"])} 处突兀撞击。按保留选项生成剪辑计划。')
        event('progress','按所选模式寻找自然切点',70)
        plan=make_plan(meta,frames,speech,music,cfg,classifier,exclusions)
        if cfg['audit'] and plan['keep_frames']:
            recognizer=Recognizer(cfg)
            try:found=recognizer.audit(pcm,plan['keep_frames'],plan['frame_seconds'])
            finally:recognizer.close()
            save_json(cache/'audit-latest.json',{'new_candidates':found})
            if found:
                event('log',f'复查发现 {len(found)} 处疑似话语，重新调整对应上下文边界。')
                speech['spoken']+=found
                plan=make_plan(meta,frames,speech,music,cfg,classifier,exclusions)
                plan['audit_added']=found
        plan['language']=speech['language']
        if not plan['keep_frames']:
            raise ValueError('没有找到可自然衔接、且 ASMR 证据足够的片段。可尝试宽松模式。' if cfg['mode']=='extract' else '当前规则下没有可保留的片段。')
        save_json(cache/f'plan-{cfg["mode"]}.json',plan)
        event('plan',f'计划保留 {len(plan["keep_frames"])} 段，共 {plan["duration"]/60:.1f} 分钟',88,
              duration=plan['duration'],segments=len(plan['keep_frames']))
        del recognizer
        classifier.close()
        gc.collect()
        reviewer=Reviewer(cfg,speech['language'],cache/'review-results') if cfg['review_enabled'] or cfg['mode']=='extract' else None
        passes=[]
        for attempt in range(cfg['review_max_passes'] if reviewer else 1):
            plan['review_passes']=passes
            try:
                report=export(source,cfg['output_dir'],meta,frames,plan,cfg,identity,reviewer,
                              allow_review_findings=cfg['mode']!='extract' and attempt+1==cfg['review_max_passes'],
                              media_context=media)
                break
            except SpeechRemaining as remaining:
                audit=remaining.report
                found=(mapped_output_to_source(audit['findings'],audit['export_mapping']) if audit.get('export_mapping')
                       else output_to_source(audit['findings'],plan['keep_frames'],plan['frame_seconds']))
                audit['source_intervals']=found
                passes.append(audit)
                save_json(cache/'post-review-latest.json',{'passes':passes,'status':'speech_found'})
                if not found or attempt+1>=cfg['review_max_passes']:
                    if reviewer:reviewer.close()
                    raise RuntimeError('成片大模型复核仍检出疑似话语，未发布为已完成结果。详情：'+str(cache/'post-review-latest.json')) from None
                event('log',f'第 {attempt+1} 轮成片复核发现 {len(found)} 处疑似话语，重新寻找自然边界。')
                speech['spoken']+=found
                if reviewer:reviewer.close()
                plan=make_plan(meta,frames,speech,music,cfg,classifier,exclusions)
                classifier.close()
                plan['language']=speech['language']
                save_json(cache/f'plan-{cfg["mode"]}.json',plan)
        save_json(cache/f'plan-{cfg["mode"]}.json',plan)
        if reviewer:reviewer.close()
        save_json(cache/'post-review-latest.json',report['speech_review'])
        status=report['speech_review']['status']
        message='剪辑完成，原帧校验与成片模型复核通过' if status=='passed' else '成片已保存，仍有疑似话语待复听，位置见人声复核.csv' if status=='needs_review' else '剪辑完成，原编码包校验通过（未开启成片复核）'
        event('complete',message,100,**report)
        return report
