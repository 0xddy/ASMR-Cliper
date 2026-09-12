"""Optional GUI interaction. CLI jobs stay unattended unless explicitly enabled."""
import json
import sys

from .common import event
from .progress import activity


def confirm_language(detected, detail=''):
    activity('等待确认语言，可在弹窗中修改后继续')
    choices=['ko','ja','zh','en']
    # Keep any concrete auto-detected language available, including languages
    # beyond the GUI's common four. An unknown result must be chosen explicitly.
    if detected and detected!='auto' and detected not in choices:
        choices.insert(0,detected)
    event('language_confirmation','请确认录音的语言',detected=detected,detail=detail,choices=choices)
    line=sys.stdin.readline()
    if not line:
        raise RuntimeError('语言确认连接已关闭，任务未继续。')
    try:
        response=json.loads(line)
    except ValueError:
        raise RuntimeError('语言确认响应无效，任务未继续。') from None
    if response.get('type')!='language_confirmed' or response.get('language') not in choices:
        raise RuntimeError('未收到有效的语言选择，任务未继续。')
    chosen=response['language']
    event('log',f'本次任务已确认语言：{chosen}')
    return chosen
