import json
import warnings
from pathlib import Path

import numpy as np

from .common import event, read_json, save_json, merge, complement
from .exclusions import summarize, strong_voice, airflow_events, supported_utterance, drinking_events, extend_breaks, laugh_kind, impact_events, rhythmic_events, KEEP_DEFAULTS
from .extraction import extraction_regions
from .acoustic_features import ast_features
from .progress import activity


class Classifier:
    def __init__(self, cfg, pcm, cache):
        import onnxruntime as ort
        from transformers import ASTFeatureExtractor
        ort.set_default_logger_severity(3)
        self.pcm, self.cfg = pcm, cfg
        self.retained_whispers=[]
        model_path = Path(cfg['ast_model'])
        # This model's standard 128-bin filter bank intentionally has empty
        # low-frequency FFT bins. Avoid a non-actionable warning in the GUI.
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore',message='At least one mel filter has all zero values.*',category=UserWarning)
            self.feature = ASTFeatureExtractor.from_pretrained(str(model_path), local_files_only=True)
        self.labels = read_json(model_path/'config.json')['id2label']
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 6
        opts.log_severity_level = 3
        providers = ['CPUExecutionProvider']
        if cfg['device'] != 'cpu' and 'CUDAExecutionProvider' in ort.get_available_providers():
            providers.insert(0, ('CUDAExecutionProvider', {'cudnn_conv_algo_search':'DEFAULT'}))
        self.session=None;self.opts=opts;self.providers=providers;self.model_path=model_path
        self.path = cache/'acoustic-cache.json'
        self.identity = str(model_path.resolve())+':'+str((model_path/'onnx/model.onnx').stat().st_mtime_ns)+':events-v6'
        previous = read_json(self.path) if self.path.exists() else {}
        self.cached = previous.get('windows', {}) if previous.get('model') == self.identity else {}

    def close(self):self.session=None

    def records(self,keys):
        from .whispering import covers
        return [{**self.cached[key],'retained_whisper':True} if covers(self.cached[key]['start'],self.cached[key]['end'],getattr(self,'retained_whispers',[]))
                else self.cached[key] for key in keys]

    def windows(self, windows):
        wanted = [(float(a), float(b)) for a,b in windows]
        keys = [f'{a:.5f}:{b:.5f}' for a,b in wanted]
        pending = dict((key, pair) for key,pair in zip(keys, wanted) if key not in self.cached)
        jobs = list(pending.items())
        if jobs:activity(f'声学窗口 0/{len(jobs)}')
        # Boundary planning and retries often ask only for cached windows.
        # Do not rewrite a multi-megabyte cache when nothing changed.
        if not jobs:
            return self.records(keys)
        for offset in range(0, len(jobs), 8):
            arrays, live = [], []
            for key, (a,b) in jobs[offset:offset+8]:
                audio = self.pcm[max(0,round(a*16000)):min(len(self.pcm),round(b*16000))].astype(np.float32)/32768
                if len(audio) < 400 or np.sqrt(np.mean(audio*audio)) < 10**(-62/20):
                    self.cached[key] = {'start':a,'end':b,'quiet':True,**summarize({})}
                    continue
                arrays.append(ast_features(audio, self.feature))
                live.append((key,a,b))
            if arrays:
                if self.session is None:
                    import onnxruntime as ort
                    self.session=ort.InferenceSession(str(self.model_path/'onnx/model.onnx'),sess_options=self.opts,providers=self.providers)
                    if self.cfg['device']=='cuda' and 'CUDAExecutionProvider' not in self.session.get_providers():
                        raise RuntimeError('CUDA 声学分类器初始化失败。请检查环境或选择自动 / CPU。')
                logits = self.session.run(None, {'input_values':np.stack(arrays)})[0]
                probabilities = 1/(1+np.exp(-logits))
                for (key,a,b),p in zip(live, probabilities):
                    scores = {self.labels[str(i)]:float(x) for i,x in enumerate(p)}
                    self.cached[key] = {'start':a,'end':b,'quiet':False,**summarize(scores)}
            completed = min(offset+8, len(jobs))
            activity(f'声学窗口 {completed}/{len(jobs)}')
            if completed % 160 == 0 or completed == len(jobs):
                # Atomic checkpoints survive cancellation during long scans.
                save_json(self.path, {'model':self.identity,'windows':self.cached})
                if len(jobs) >= 160:
                    event('log', f'声学上下文分析：{completed} / {len(jobs)} 个窗口')
        return self.records(keys)

    def music_intervals(self, duration):
        # Ten-second windows cover the entire recording; no source-specific
        # activity timestamps or recording-language assumptions are embedded.
        records = self.windows([(t,min(t+10,duration)) for t in np.arange(0,duration,10)])
        return [[r['start'],r['end']] for r in records if r['music']>.55 and r['texture']<.12]

    def exclusions(self, speech, music, duration):
        # Cover the interior of every candidate section, including sections not
        # initially selected by the boundary planner. Subsequent replanning or
        # optional ASR audits therefore cannot expose unchecked interiors.
        regions=complement(speech['spoken']+music,duration)
        windows=[]
        for a,b in regions:
            for t in np.arange(a,b,2.):
                if b-t>=.4: windows.append((t,min(t+6,b)))
        records=self.windows(windows)
        suspects=[r for r in records if strong_voice(r)]
        fine=[]
        for r in suspects:
            fine.extend((t,min(t+2,r['end'])) for t in np.arange(r['start'],r['end'],1.) if r['end']-t>=.4)
        localized=self.windows(fine)
        voice=merge([[r['start'],r['end']] for r in localized if strong_voice(r)],.35)
        # Very strong context is kept as exclusion evidence even if short
        # crops lose the model's context, rather than turning into ASMR.
        for r in suspects:
            if max(r.get('speech',0.),r.get('expressive',0.))>=.85 and not any(a<r['end'] and b>r['start'] for a,b in voice):
                voice.append([r['start'],r['end']])
        uncertain=speech.get('uncertain',[])
        spans=[(max(0,s['start']-.4),min(duration,max(s['end'],s['start']+1.)+.4)) for s in uncertain]
        supported=[]
        for s,r in zip(uncertain,self.windows(spans)):
            if supported_utterance(s,r):
                voice.append([s['start'],s['end']])
                supported.append({'start':s['start'],'end':s['end'],'text':s['text'],'speech':r['speech'],'expressive':r['expressive']})
        noise=extend_breaks(airflow_events(records),records)
        drinks=extend_breaks(drinking_events(records,speech['accepted']),records)
        # Detect categories separately. Retention settings are applied by the
        # planner so cached evidence never encodes a previous checkbox state.
        laughter={kind:extend_breaks(merge([[r['start'],r['end']] for r in records if laugh_kind(r)==kind]),records)
                  for kind in ('soft_laugh','loud_laugh')}
        impacts=impact_events(records,self.pcm)
        rhythms={kind:rhythmic_events(records,kind) for kind in ('heartbeat','tapping')}
        expressive=extend_breaks(merge([[r['start'],r['end']] for r in records if r.get('expressive',0.)>=.42]),records)
        voice+=expressive
        report={'version':6,'voice':merge(voice,.65),'airflow':noise,'drinking':drinks,**laughter,**rhythms,'impacts':impacts,
                'extraction':extraction_regions(records),
                'retention':{k:self.cfg.get(k,v) for k,v in KEEP_DEFAULTS.items()},'expressive_breaks':expressive,'supported_utterances':supported,
                'windows_checked':len(records),'note':'airflow/drinking 表示根据声音组合判断的呼气/烟雾类气流动作或饮水休息，非设备身份识别'}
        save_json(self.path.parent/'exclusions-latest.json',report)
        return report
