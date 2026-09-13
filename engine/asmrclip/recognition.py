import dataclasses
import hashlib
import json
import math
import re
from bisect import bisect_right
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from .common import event, merge, save_json, read_json
from .progress import scope, advance, activity

GENERIC = re.compile(r'다음\s*영상|시청.*감사|영상.{0,25}감사|봐주.{0,15}감사|구독|자막|영상.*만나요|thanks?\s+for\s+watching|subscribe|ご視聴|字幕|感谢观看|謝謝觀看', re.I)
NONLEX = re.compile(r'^[으음어아오우흐흠하헤히헉휴후응웅잉에엥크킁흫와예악윽앗헐야ㅎㅋ]+$|^(?:[aumoh]+|ah|uh|hmm|ha)+$|^(?:he|hi|ho){2,}$|^[啊嗯哦呃唔哈呵嘻嘿はあうんえお]+$', re.I)


def recover_jsonl(path):
    if not path.exists():
        return []
    lines=path.read_text(encoding='utf-8',errors='replace').splitlines()
    rows=[]
    for i,line in enumerate(lines):
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if any(x.strip() for x in lines[i+1:]):
                raise ValueError('语音分析缓存损坏，请移走该文件对应的 runtime/cache 子目录后重试。') from None
            path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    return rows


def plausible(s, vad_intervals, continuous=False):
    if s.get('backend')=='qwen3-asr':return qwen_speech(s)
    text = s['text'].strip()
    clean = re.sub(r'[\W_]+', '', text)
    words = s.get('words') or []
    wp = sum(w.get('probability', 0) for w in words)/max(1, len(words))
    overlap = sum(max(0, min(s['end'], b)-max(s['start'], a)) for a, b in vad_intervals)
    if not clean or NONLEX.fullmatch(clean) or s['end'] <= s['start'] or s.get('compression_ratio', 0) > 2.5:
        return False
    # Generic endings are common ASMR transcription hallucinations. Require
    # independent voice activity plus a strong first lexical token.
    if GENERIC.search(text) and (overlap < .5 or not words or words[0]['probability'] < .4):
        return False
    lp = s.get('avg_logprob', -9)
    if continuous:
        return (lp > -1.2 and wp > .35 and len(clean) >= 4) or (lp > -.8 and wp > .5) or (overlap > .5 and lp > -1.65 and wp > .25)
    return (lp > -1.65 and wp > .18) or (wp > .35 and lp > -1.85)


def word_intervals(segments):
    ranges = []
    for s in segments:
        groups = []
        for w in s.get('words') or []:
            # Confidence accepts/rejects the utterance, not pieces of a phrase.
            # Dropping its low-confidence words leaves emotional syllables and
            # word tails audible even after the sentence was recognized.
            if w['end'] <= w['start']:
                continue
            if groups and w['start']-groups[-1][1] <= .9:
                groups[-1][1] = max(groups[-1][1], w['end'])
            else:
                groups.append([w['start'], w['end']])
        ranges += groups or [[s['start'], s['end']]]
    return merge(ranges, .65)


def qwen_speech(s):
    # Qwen does not expose Whisper-style word probabilities. Do not invent
    # confidence values; use lexical output and actual alignment validity.
    return qwen_lexical(s) and not qwen_alignment_issue(s)


def qwen_lexical(s):
    clean=re.sub(r'[\W_]+','',s.get('text',''))
    return bool(clean) and not NONLEX.fullmatch(clean) and not GENERIC.search(s.get('text',''))


def qwen_alignment_issue(s):
    """Forced alignment locates supplied text; it does not confirm it exists.

    Reject collapsed/partial alignments as automatic deletion evidence. Keep
    their text for listening review instead of expanding a few invalid ticks
    into minutes of deleted context. No fabricated confidence probabilities.
    """
    words=s.get('words') or []
    a,b=s.get('start',0),s.get('end',0)
    if not words or not all(math.isfinite(v) for v in (a,b)) or b<=a:
        return '话语时间定位无效'
    valid=[w for w in words if all(math.isfinite(w.get(k,0)) for k in ('start','end')) and w['end']>w['start']]
    total=s.get('alignment_total_words',len(words))
    if len(valid)<total*.6:return '多数词的时间定位塌缩'
    if any(w['end']-w['start']>max(6.,len(re.sub(r'[\W_]+','',w.get('word','')))*1.5) for w in valid):
        return '单个短词被错误延长到多个秒段'
    clean=re.sub(r'[\W_]+','',s.get('text',''))
    covered=sum(d-c for c,d in merge([[w['start'],w['end']] for w in valid]))
    if b-a<.18 or len(clean)/max(covered,1e-6)>30:
        return '识别文字与定位时长不匹配'
    return ''


def Recognizer(cfg):
    return QwenRecognizer(cfg) if cfg.get('speech_model')=='qwen3-asr' else WhisperRecognizer(cfg)


class WhisperRecognizer:
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.model = self.pipe = None
        self.batch_size=8 if cfg.get('speech_model')=='whisper-turbo' else 4
        self.language = None if cfg['language'] == 'auto' else cfg['language']

    def load(self):
        if self.pipe is not None:
            return
        from faster_whisper import WhisperModel, BatchedInferencePipeline
        import ctranslate2
        cfg = self.cfg
        device = cfg['device']
        if device == 'auto':
            device = 'cuda' if ctranslate2.get_cuda_device_count() else 'cpu'
        event('log', f'载入本地语音模型 · {device.upper()}')
        try:
            self.model = WhisperModel(cfg['whisper_model'], device=device, compute_type='float16' if device == 'cuda' else 'int8', local_files_only=True)
        except Exception:
            if cfg['device'] != 'auto' or device == 'cpu':
                raise
            event('log', 'GPU 初始化失败，改用 CPU 继续处理。')
            self.model = WhisperModel(cfg['whisper_model'], device='cpu', compute_type='int8', local_files_only=True)
        self.pipe = BatchedInferencePipeline(model=self.model)

    def transcribe(self, audio, clips, offset=0):
        if not clips:
            return []
        advance(0,len(clips))
        self.load()
        segs, info = self.pipe.transcribe(audio, language=self.language, beam_size=5, batch_size=self.batch_size,
            word_timestamps=True, clip_timestamps=clips, condition_on_previous_text=False,
            temperature=0, max_new_tokens=160, no_repeat_ngram_size=3)
        result = []
        ends=sorted(c['end'] for c in clips)
        completed=0
        for s in segs:
            # A transcript can have many segments per window, or none at all.
            # Only report windows already passed on the original timeline.
            past=bisect_right(ends,s.start)
            if past>completed:
                completed=past;advance(completed,len(clips))
            d = dataclasses.asdict(s)
            d['start'] += offset
            d['end'] += offset
            for w in d['words'] or []:
                w['start'] += offset
                w['end'] += offset
            result.append(d)
        advance(len(clips),len(clips))
        return result

    def transcribe_pass(self, audio, clips, offset, label, start, end):
        with scope(label,start,end):
            return self.transcribe(audio,clips,offset)

    def close(self):
        self.pipe=None;self.model=None

    def cache_identity(self,cfg):
        return [cfg['whisper_model'],Path(cfg['whisper_model'],'model.bin').stat().st_mtime_ns,cfg['language'],2]

    def detect_language(self,audio):
        self.load()
        language,probability,_=self.model.detect_language(audio,vad_filter=True,language_detection_segments=3)
        return language,f'（置信度 {probability:.0%}）'

    def scan(self, wav_path, cfg, cache):
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        sr, pcm = wavfile.read(wav_path, mmap=True)
        identity = self.cache_identity(cfg)
        token = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:12]
        path = cache / f'speech-{token}.jsonl'
        rows = recover_jsonl(path)
        done = {r['offset'] for r in rows}
        if rows:
            event('log', f'复用 {len(done)} 段已完成的语音定位缓存。')
        automatic=self.language is None
        detail='来自已完成的语言识别缓存'
        if automatic:
            if rows:
                self.language = rows[0]['language']
            else:
                audio = pcm[:min(len(pcm), sr*180)].astype(np.float32)/32768
                with scope('语言检测',0,0):
                    self.language, detail = self.detect_language(audio)
                event('log', f'自动识别语言：{self.language}{detail}；可在界面手动指定语言')
        if automatic and cfg.get('confirm_detected_language',False):
            from .interaction import confirm_language
            self.language=confirm_language(self.language,detail)
            # A changed language must never reuse transcripts from the old one.
            # Use the same identity as a manually selected-language task.
            identity=self.cache_identity({**cfg,'language':self.language})
            token=hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:12]
            path=cache/f'speech-{token}.jsonl'
            rows=recover_jsonl(path)
            done={r['offset'] for r in rows}
        activity('语言已确定，准备全段语音检查')
        with path.open('a', encoding='utf-8') as f:
            offsets=range(0, len(pcm), 600*sr)
            for index,offset in enumerate(offsets):
                sec = offset/sr
                if sec in done:
                    with scope(f'音频块 {index+1}/{len(offsets)} · 复用缓存',index/len(offsets),(index+1)/len(offsets)):
                        pass
                    continue
                audio = pcm[offset:offset+600*sr].astype(np.float32)/32768
                vad = get_speech_timestamps(audio, vad_options=VadOptions(threshold=.35, min_speech_duration_ms=180,
                    min_silence_duration_ms=350, speech_pad_ms=350, max_speech_duration_s=28))
                clips = [{'start':v['start']/sr, 'end':v['end']/sr} for v in vad]
                continuous = [{'start':x, 'end':min(x+28, len(audio)/sr)} for x in range(0, int(np.ceil(len(audio)/sr)), 28)]
                with scope(f'音频块 {index+1}/{len(offsets)}',index/len(offsets),(index+1)/len(offsets)):
                    split=len(clips)/max(1,len(clips)+len(continuous))
                    row = {'offset':sec, 'language':self.language, 'vad':[[v['start']/sr+sec, v['end']/sr+sec] for v in vad],
                           'vad_segments':self.transcribe_pass(audio,clips,sec,'人声检查 1/2',0,split),
                           'continuous_segments':self.transcribe_pass(audio,continuous,sec,'全段检查 2/2',split,1)}
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False)+'\n')
                f.flush()
                event('progress', f'语音定位：{min(len(pcm), offset+600*sr)/sr/60:.1f} / {len(pcm)/sr/60:.1f} 分钟', 18+42*min(1, (offset+600*sr)/len(pcm)))
        vad = merge([v for r in rows for v in r['vad']])
        accepted, uncertain = [], []
        for row in rows:
            for key in ['vad_segments', 'continuous_segments']:
                for s in row[key]:
                    if plausible(s,vad,key=='continuous_segments'): accepted.append(s)
                    elif s.get('backend')=='qwen3-asr' and qwen_lexical(s):
                        uncertain.append({**s,'review_only':True,'reason':qwen_alignment_issue(s)})
                    elif s['end']>s['start'] and s.get('avg_logprob',-9)>-1.85 and not GENERIC.search(s['text']): uncertain.append(s)
        unique={(s['start'],s['end'],s['text']):s for s in uncertain}
        result = {'language':self.language, 'model':cfg.get('speech_model','whisper-large-v3'),'accepted':accepted, 'spoken':word_intervals(accepted), 'vad':vad,'uncertain':list(unique.values())}
        save_json(cache / 'speech.json', result)
        return result

    def audit(self, pcm, keeps, dt):
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        mapping, parts, cursor = [], [], 0
        for a, b in keeps:
            part = pcm[round(a*dt*16000):round(b*dt*16000)]
            parts.append(part)
            mapping.append((cursor/16000, (cursor+len(part))/16000, a*dt))
            cursor += len(part)
        if not parts:
            return []
        edited = np.concatenate(parts)
        found = []
        offsets=range(0,len(edited),600*16000)
        for index,offset in enumerate(offsets):
            audio = edited[offset:offset+600*16000].astype(np.float32)/32768
            vad = get_speech_timestamps(audio, vad_options=VadOptions(threshold=.15, min_speech_duration_ms=140,
                min_silence_duration_ms=250, speech_pad_ms=300, max_speech_duration_s=28))
            clips = [{'start':v['start']/16000,'end':v['end']/16000} for v in vad]
            absolute = [[v['start']/16000+offset/16000,v['end']/16000+offset/16000] for v in vad]
            segments=self.transcribe_pass(audio,clips,offset/16000,f'计划预检 · 音频块 {index+1}/{len(offsets)}',index/len(offsets),(index+1)/len(offsets))
            for s in segments:
                if not plausible(s, absolute, continuous=True):
                    continue
                for lo, hi, source in mapping:
                    a, b = max(lo, s['start']), min(hi, s['end'])
                    if b > a:
                        found.append([source+a-lo, source+b-lo])
            event('progress', '复查剪辑计划中的剩余人声', 78+8*min(1, (offset+600*16000)/len(edited)))
        return merge(found, .65)


class QwenRecognizer(WhisperRecognizer):
    LANGUAGES={'ko':'Korean','ja':'Japanese','zh':'Chinese','en':'English'}
    def __init__(self,cfg):
        self.cfg=dict(cfg)
        self.client=None
        self.language=None if cfg['language']=='auto' else cfg['language']

    def cache_identity(self,cfg):
        from .model_catalog import model_signature
        from .common import ROOT
        return ['qwen-scan-2',model_signature(ROOT/'models/qwen-asr'),model_signature(ROOT/'models/qwen-aligner'),cfg['language']]

    def transcribe(self,audio,clips,offset=0):
        if not clips:return []
        advance(0,len(clips))
        if self.client is None:
            from .neural_client import NeuralClient
            self.client=NeuralClient('qwen',self.cfg)
        result=self.client.request({'op':'transcribe','clips':clips,'language':self.LANGUAGES.get(self.language)},audio)
        advance(len(clips),len(clips))
        for s in result:
            s['start']+=offset;s['end']+=offset
            for w in s['words']:w['start']+=offset;w['end']+=offset
        return result

    def detect_language(self,audio):
        rows=self.transcribe(audio,[{'start':0,'end':min(len(audio)/16000,28)}])
        language=next((s['language'] for s in rows if s.get('text','').strip() and s.get('language')),'')
        mapping={v:k for k,v in self.LANGUAGES.items()}
        return mapping.get(language,'auto'),'（Qwen；未知时继续逐窗识别）'

    def close(self):
        if getattr(self,'client',None):self.client.close();self.client=None
