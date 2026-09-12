"""Standard-library downloads usable before third-party packages are installed."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

LARGE_DOWNLOAD_THRESHOLD=2*1024**3
DOWNLOAD_CHUNK=32*1024**2


def proxy_value(cfg):
    if not cfg.get('proxy_enabled',False):
        return ''
    value=cfg.get('proxy_url','').strip()
    parsed=urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http','https') or not parsed.hostname or any(x.isspace() for x in value):
        raise ValueError('请输入有效的 HTTP(S) 代理地址，例如 http://127.0.0.1:10886。')
    try:
        parsed.port
    except ValueError:
        raise ValueError('代理端口无效。') from None
    return value


def opener_for(cfg):
    proxy=proxy_value(cfg)
    # An explicit empty map prevents ambient system proxy settings from
    # overriding the application's Direct connection choice.
    return urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))


def sha256(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def fetch_text(url,cfg,max_size=1024*1024):
    request=urllib.request.Request(url,headers={'User-Agent':'ASMRCLIP/0.2'})
    with opener_for(cfg).open(request,timeout=25) as response:
        data=response.read(max_size+1)
    if len(data)>max_size:raise ValueError('远端元数据超过预期大小。')
    return data.decode('utf-8-sig')


def download(url,dest,cfg,expected_hash=None,expected_size=None,callback=None):
    """Resume with Range; verify before atomically promoting the .part file."""
    dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.is_file() and (not expected_size or dest.stat().st_size==expected_size) and expected_hash and sha256(dest)==expected_hash:
        if callback:callback(dest.stat().st_size,dest.stat().st_size,0,True)
        return dest
    part=dest.with_name(dest.name+'.part')
    identity=dest.with_name(dest.name+'.part.json')
    expected={'url':url,'hash':expected_hash,'size':expected_size}
    try:previous=json.loads(identity.read_text(encoding='utf8'))
    except (OSError,ValueError):previous=None
    if previous!=expected and part.exists():part.unlink()
    identity.write_text(json.dumps(expected),encoding='utf8')
    last_error=None
    attempt=0
    while attempt<3:
        offset=part.stat().st_size if part.exists() else 0
        if expected_size and offset==expected_size and expected_hash and sha256(part)==expected_hash:
            os.replace(part,dest);identity.unlink(missing_ok=True);return dest
        headers={'User-Agent':'ASMRCLIP/0.2','Accept-Encoding':'identity'}
        chunked=expected_size and expected_size>LARGE_DOWNLOAD_THRESHOLD
        if chunked:headers['Range']=f'bytes={offset}-{min(expected_size-1,offset+DOWNLOAD_CHUNK-1)}'
        elif offset:headers['Range']=f'bytes={offset}-'
        request=urllib.request.Request(url,headers=headers)
        try:
            with opener_for(cfg).open(request,timeout=35) as response:
                partial_end=None
                if response.status==206:
                    content_range=response.headers.get('Content-Range','')
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)',content_range)
                    if not match or int(match[1])!=offset:raise ValueError('下载服务器返回了不一致的断点位置。')
                    total=int(match[3]) if match[3]!='*' else expected_size
                    partial_end=int(match[2])+1
                    if partial_end<=offset or (expected_size and total!=expected_size):raise ValueError('下载服务器返回了不一致的文件范围。')
                    mode='ab'
                else:
                    offset=0;mode='wb'
                    total=int(response.headers.get('Content-Length') or 0) or expected_size
                downloaded=offset;started=time.monotonic();last=0
                with part.open(mode) as output:
                    while True:
                        chunk=response.read(512*1024)
                        if not chunk:break
                        output.write(chunk);downloaded+=len(chunk)
                        now=time.monotonic()
                        if callback and now-last>.25:
                            callback(downloaded,total or 0,(downloaded-offset)/max(now-started,.01),False);last=now
                if partial_end and downloaded==partial_end and total and downloaded<total:
                    attempt=0
                    continue
                if total and downloaded!=total:raise IOError('下载中断，已保留断点，重试将继续下载。')
                if expected_size and downloaded!=expected_size:raise IOError('下载大小不符合发布清单。')
            if expected_hash and sha256(part)!=expected_hash:
                part.unlink(missing_ok=True)
                raise ValueError('下载内容校验失败，正在重新获取。')
            os.replace(part,dest);identity.unlink(missing_ok=True)
            if callback:callback(dest.stat().st_size,dest.stat().st_size,0,False)
            return dest
        except urllib.error.HTTPError as exc:
            last_error=exc
            if exc.code==416:part.unlink(missing_ok=True)
            elif exc.code in (401,403,404,407):break
        except (OSError,ValueError) as exc:last_error=exc
        attempt+=1
        if attempt<3:time.sleep(attempt)
    raise RuntimeError(f'下载未完成：{dest.name}。请检查代理或网络后重试。原因：{last_error}')


def extract_checked(archive,destination):
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            target=(destination/info.filename).resolve()
            if not target.is_relative_to(destination):raise ValueError('压缩包包含越界路径。')
            if (info.external_attr>>16)&0o170000==0o120000:raise ValueError('压缩包包含不支持的符号链接。')
        package.extractall(destination)
