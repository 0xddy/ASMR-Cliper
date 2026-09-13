"""Keep published paths usable by Windows shell players, not just FFmpeg."""
import hashlib
from pathlib import Path


def windows_length(text):
    return len(str(text).encode('utf-16-le'))//2


def compact_stem(name,budget):
    if windows_length(name)<=budget:return name
    suffix='_'+hashlib.sha256(name.encode('utf-8')).hexdigest()[:6]
    if budget<len(suffix)+1:raise ValueError('输出目录路径过长，请选择更短的保存目录。')
    prefix=''
    for c in name:
        if windows_length(prefix+c+suffix)>budget:break
        prefix+=c
    return prefix.rstrip(' .')+suffix


def export_names(output_dir,stem,mode,stamp,token,extension):
    # Count UTF-16 units (including astral characters). Leave room below
    # MAX_PATH for shell associations and companion reports. FFmpeg accepting
    # a longer staging path does not prove an external player can open it.
    suffix=f'_{mode}_{stamp}_{token}'
    budget=240-windows_length(Path(output_dir)/('x'+suffix)/('x_ASMR_'+mode+extension))+2
    name=compact_stem(stem,min(48,budget//2))
    return Path(output_dir)/(name+suffix),f'{name}_ASMR_{mode}{extension}'
