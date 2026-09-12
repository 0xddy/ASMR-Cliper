"""Task progress is work coverage, never an estimate of remaining time.

Scopes compose coverage without changing model calls, batching or cache keys.
Each review attempt gets its own 0–100% range; the round limit is an upper bound.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_current = ContextVar('task_progress', default=None)
STAGES = ('音轨准备', '语音识别', '声音筛选', '剪辑规划', '成片复核', '导出校验', '生成成片节目单')


def tracked(function):
    @wraps(function)
    def run(*args, **kwargs):
        token = _current.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _current.reset(token)
    return run


def phase(index, detail='', *, round_index=0, round_limit=0, legacy=None, total=None, position=None):
    state = _current.get()
    if state is None:
        return
    count=total if total is not None else state.get('stages',len(STAGES))
    state.clear()
    state.update(stage=position or index, stages=count, title=STAGES[index-1],
                 round=round_index, round_limit=round_limit, percent=None,
                 detail=detail, scopes=[], legacy=legacy)
    _emit()


def _snapshot():
    return {k: v for k, v in _current.get().items() if k not in ('scopes', 'legacy')}


def _emit():
    from .common import event
    event('task_progress', task_progress=_snapshot())


def legacy_update(message, percent):
    """Adapt bounded FFmpeg/frame decoding counters, not arbitrary model logs."""
    state = _current.get()
    if not state or state['legacy'] is None:
        return None
    lo, hi = state['legacy']
    state['detail'] = message
    if percent is not None:
        state['percent'] = round(100*max(0, min(1, (percent-lo)/(hi-lo))), 1)
    return _snapshot()


def advance(done, total, unit='窗口'):
    state = _current.get()
    if not state:
        return
    scopes = state['scopes']
    lo, hi = scopes[-1][1:] if scopes else (0., 1.)
    fraction = max(0., min(1., done/total)) if total else 1.
    state['percent'] = round(100*(lo+(hi-lo)*fraction), 1)
    labels = [s[0] for s in scopes if s[0]]
    if unit:
        labels.append(f'{unit} {done}/{total}' if total else '无待检查窗口')
    state['detail'] = ' · '.join(labels)
    _emit()


def activity(message):
    state = _current.get()
    if state:
        state['detail'] = ' · '.join([s[0] for s in state['scopes'] if s[0]] + [message])
        _emit()


@contextmanager
def scope(label, start=0., end=1.):
    state = _current.get()
    if not state:
        yield
        return
    stack = state['scopes']
    lo, hi = stack[-1][1:] if stack else (0., 1.)
    stack.append((label, lo+(hi-lo)*start, lo+(hi-lo)*end))
    advance(0, 1, '')
    try:
        yield
    except BaseException:
        raise
    else:
        advance(1, 1, '')
    finally:
        stack.pop()
