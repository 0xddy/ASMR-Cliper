import hashlib
import http.server
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from download_support import download,extract_checked,proxy_value

PAYLOAD=bytes(range(256))*9000
DIGEST=hashlib.sha256(PAYLOAD).hexdigest()


class Handler(http.server.BaseHTTPRequestHandler):
    ranges=[]
    def log_message(self,*args):pass
    def do_GET(self):
        offset=int(self.headers.get('Range','bytes=0-').split('=')[1].split('-')[0])
        self.ranges.append((self.path,offset))
        if self.path=='/ignore':offset=0
        if self.path=='/chunks':
            requested=self.headers.get('Range','').split('-')[-1]
            end=min(len(PAYLOAD),int(requested)+1) if requested else len(PAYLOAD)
            self.send_response(206);self.send_header('Content-Range',f'bytes {offset}-{end-1}/{len(PAYLOAD)}')
            self.send_header('Content-Length',str(end-offset));self.end_headers();self.wfile.write(PAYLOAD[offset:end]);return
        self.send_response(206 if offset else 200)
        if offset:self.send_header('Content-Range',f'bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}')
        self.send_header('Content-Length',str(len(PAYLOAD)-offset));self.end_headers()
        data=PAYLOAD[offset:]
        if self.path=='/corrupt':data=b'x'*len(data)
        if self.path=='/disconnect' and offset==0:data=data[:700000]
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass


class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.dest=Path(self.tmp.name)/'含空格 文件.bin';Handler.ranges=[]

    def partial(self,path,hash_value=DIGEST):
        self.dest.with_name(self.dest.name+'.part').write_bytes(PAYLOAD[:300000])
        self.dest.with_name(self.dest.name+'.part.json').write_text(json.dumps({'url':self.base+path,'hash':hash_value,'size':len(PAYLOAD)}))

    def fetch(self,path,hash_value=DIGEST):
        with patch('download_support.time.sleep'):
            return download(self.base+path,self.dest,{'proxy_enabled':False},hash_value,len(PAYLOAD))

    def test_partial_download_handles_range_support_and_identity(self):
        cases=(('/range',DIGEST,300000),('/ignore',DIGEST,300000),('/range','0'*64,0))
        for index,(path,identity,offset) in enumerate(cases):
            with self.subTest(path=path,identity=identity):
                self.dest=Path(self.tmp.name)/f'含空格 文件-{index}.bin'
                Handler.ranges=[]
                self.partial(path,identity);self.fetch(path)
                self.assertEqual(self.dest.read_bytes(),PAYLOAD)
                self.assertEqual(Handler.ranges[0][1],offset)

    def test_large_model_download_uses_bounded_resumable_ranges(self):
        with patch('download_support.LARGE_DOWNLOAD_THRESHOLD',1000000),patch('download_support.DOWNLOAD_CHUNK',262144):
            self.fetch('/chunks')
        self.assertEqual(self.dest.read_bytes(),PAYLOAD)
        self.assertEqual([n for _,n in Handler.ranges],list(range(0,len(PAYLOAD),262144)))

    def test_interrupted_response_retries_from_saved_offset(self):
        self.fetch('/disconnect');self.assertEqual(self.dest.read_bytes(),PAYLOAD)
        self.assertTrue(any(offset>0 for _,offset in Handler.ranges))

    def test_corrupt_download_does_not_replace_existing_file(self):
        self.dest.write_bytes(b'existing file')
        with self.assertRaises(RuntimeError):self.fetch('/corrupt')
        self.assertEqual(self.dest.read_bytes(),b'existing file')

    def test_verified_existing_file_never_contacts_network(self):
        self.dest.write_bytes(PAYLOAD);self.fetch('/range');self.assertEqual(Handler.ranges,[])

    def test_direct_connection_ignores_ambient_proxy(self):
        with patch.dict(os.environ,{'http_proxy':'http://127.0.0.1:1','https_proxy':'http://127.0.0.1:1'}):self.fetch('/range')
        self.assertEqual(self.dest.read_bytes(),PAYLOAD)

    def test_invalid_proxy_reports_error(self):
        for value in ['','socks5://localhost:1080','http://','http://127.0.0.1:99999','http://a b']:
            with self.subTest(value=value),self.assertRaises(ValueError):proxy_value({'proxy_enabled':True,'proxy_url':value})

    def test_zip_traversal_rejected_before_any_extraction(self):
        archive=Path(self.tmp.name)/'bad.zip';target=Path(self.tmp.name)/'runtime'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('valid.txt','safe');z.writestr('../outside.txt','bad')
        with self.assertRaises(ValueError):extract_checked(archive,target)
        self.assertFalse((target/'valid.txt').exists());self.assertFalse((target.parent/'outside.txt').exists())

    def test_valid_zip_extraction(self):
        archive=Path(self.tmp.name)/'good.zip';target=Path(self.tmp.name)/'runtime'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('pip/__init__.py','content')
        extract_checked(archive,target);self.assertEqual((target/'pip/__init__.py').read_text(),'content')


if __name__=='__main__':unittest.main()
