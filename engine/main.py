import argparse
import json
import sys
import traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

sys.stdout.reconfigure(encoding='utf-8',errors='replace')
sys.stderr.reconfigure(encoding='utf-8',errors='replace')

from asmrclip.common import event, read_json, settings


def main():
    parser=argparse.ArgumentParser(description='ASMRCLIP local editing engine')
    parser.add_argument('command',choices=['run','doctor'])
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    try:
        data=read_json(args.config)
        from asmrclip.pipeline import run,doctor
        if args.command=='doctor':
            doctor(settings(data))
        else:
            run(data)
        return 0
    except Exception as exc:
        event('error',str(exc) or type(exc).__name__)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
