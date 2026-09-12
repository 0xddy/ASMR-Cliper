import argparse
import os
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--proxy',default='')
args=parser.parse_args()
if args.proxy:
    os.environ['HTTP_PROXY']=os.environ['HTTPS_PROXY']=args.proxy
os.environ['HF_HUB_DISABLE_XET']='1'
from huggingface_hub import snapshot_download
root=Path(__file__).resolve().parents[1]
snapshot_download('mobiuslabsgmbh/faster-whisper-large-v3-turbo',local_dir=root/'models/whisper-turbo',
                  allow_patterns=['config.json','model.bin','tokenizer.json','vocabulary.json','preprocessor_config.json'])
snapshot_download('Xenova/ast-finetuned-audioset-10-10-0.4593',local_dir=root/'models/ast',
                  allow_patterns=['config.json','preprocessor_config.json','onnx/model.onnx'])
print('Local models are ready.')
