import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts/wechat_rmfh.py'
spec=importlib.util.spec_from_file_location('rmfh',SCRIPT)
rmfh=importlib.util.module_from_spec(spec)
spec.loader.exec_module(rmfh)

KEY=bytes(range(16))
NONCE=bytes(range(12))

def wrap(ciphertext,tag,nonce=NONCE):
    head=bytearray(128);head[:4]=b'RMFH';head[19:31]=nonce
    tail=bytearray(128);tail[:4]=b'RMFT';tail[10:26]=tag
    return bytes(head)+ciphertext+bytes(tail)

def fixture(plain,key=KEY,nonce=NONCE):
    data=AESGCM(key).encrypt(nonce,plain,None)
    return wrap(data[:-16],data[-16:],nonce)

class RMFHTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.source=self.base/'backup';self.source.mkdir()
        self.file=self.source/'ChatPackage'/'sample';self.file.parent.mkdir()
    def put(self,data):
        self.file.write_bytes(data)
        return self.file
    def test_nist_known_vector(self):
        p=self.put(wrap(bytes.fromhex('0388dace60b6a392f328c2b971b2fe78'),bytes.fromhex('ab6e47d42cec13bdf53a67b21257bddf'),bytes(12)))
        out=io.BytesIO();self.assertEqual(rmfh.decrypt_stream(p,bytes(16),out),16)
        self.assertEqual(out.getvalue(),bytes(16))
    def test_large_stream_roundtrip_and_original_unchanged(self):
        plain=('你好，测试聊天。\n'.encode()*120000)
        p=self.put(fixture(plain));original=hashlib.sha256(p.read_bytes()).digest()
        destination=self.base/'plain'
        rmfh.publish_file(p,destination,KEY)
        self.assertEqual(destination.read_bytes(),plain)
        self.assertEqual(hashlib.sha256(p.read_bytes()).digest(),original)
    def test_wrong_key_no_plain_or_partial(self):
        self.put(fixture(b'secret'))
        with self.assertRaises(rmfh.ToolError):rmfh.publish_file(self.file,self.base/'plain',b'x'*16)
        self.assertFalse((self.base/'plain').exists())
        self.assertFalse(list(self.base.glob('*.partial')))
    def test_tag_tamper_no_plain_or_partial(self):
        data=bytearray(fixture(b'test'));data[-118]^=1;self.put(data)
        with self.assertRaises(rmfh.ToolError):rmfh.publish_file(self.file,self.base/'plain',KEY)
        self.assertFalse((self.base/'plain').exists())
        self.assertFalse(list(self.base.glob('*.partial')))
    def test_ciphertext_tamper(self):
        data=bytearray(fixture(b'test'));data[128]^=1;self.put(data)
        with self.assertRaises(rmfh.ToolError):rmfh.decrypt_stream(self.file,KEY)
    def test_wrong_key_does_not_create_output_dir(self):
        self.put(fixture(b'test'))
        with self.assertRaises(rmfh.ToolError):rmfh.decrypt_batch(self.source,self.base/'out',b'z'*16)
        self.assertFalse((self.base/'out').exists())
    def test_no_overwrite(self):
        self.put(fixture(b'test'));destination=self.base/'plain';destination.write_bytes(b'keep')
        with self.assertRaises(rmfh.ToolError):rmfh.publish_file(self.file,destination,KEY)
        self.assertEqual(destination.read_bytes(),b'keep')
        with self.assertRaises(rmfh.ToolError):rmfh.decrypt_batch(self.source,self.base,KEY)
    def test_source_output_isolation(self):
        self.put(fixture(b'test'))
        for dest in [self.source,self.source/'out',self.base]:
            with self.assertRaises(rmfh.ToolError):rmfh.decrypt_batch(self.source,dest,KEY)
    def test_batch_partial_failure_and_plain_metadata(self):
        self.put(fixture(b'good'))
        bad=self.file.parent/'bad';data=bytearray(fixture(b'bad'));data[-118]^=1;bad.write_bytes(data)
        broken=self.file.parent/'broken-header';broken.write_bytes(b'not a container')
        (self.source/'pkg_info.dat').write_bytes(b'\x08\x01')
        out=self.base/'out'
        with contextlib.redirect_stdout(io.StringIO()):report,code=rmfh.decrypt_batch(self.source,out,KEY)
        self.assertEqual(code,2)
        self.assertEqual(report['summary'],{'failed':2,'authenticated_decrypted':1,'copied_without_decryption':1})
        self.assertEqual((out/'decrypted/ChatPackage/sample').read_bytes(),b'good')
        self.assertEqual((out/'unchanged/pkg_info.dat').read_bytes(),b'\x08\x01')
        self.assertFalse((out/'decrypted/ChatPackage/bad').exists())
        self.assertFalse((out/'unchanged/ChatPackage/broken-header').exists())
        self.assertFalse(list(out.rglob('*.partial')))
        self.assertNotIn(KEY.hex(),(out/'report.json').read_text(encoding='utf8'))
    def test_empty_payload(self):
        self.put(fixture(b''));destination=self.base/'plain'
        self.assertEqual(rmfh.publish_file(self.file,destination,KEY),0)
        self.assertEqual(destination.read_bytes(),b'')
    def test_truncated_wrapper(self):
        self.put(b'RMFH'+b'x'*100)
        self.assertEqual(rmfh.file_kind(self.file),'invalid_rmfh')
        with self.assertRaises(rmfh.ToolError):rmfh.decrypt_stream(self.file,KEY)
    def test_parse_keys(self):
        self.assertEqual(rmfh.parse_key('00 '*16),bytes(16))
        for invalid in ['password','00'*15,'00'*17,'gg'*16]:
            with self.assertRaises(rmfh.ToolError):rmfh.parse_key(invalid)
    def test_aes192_and_256(self):
        for size in [24,32]:
            key=bytes(range(size));self.put(fixture(b'test',key))
            out=io.BytesIO();rmfh.decrypt_stream(self.file,key,out)
            self.assertEqual(out.getvalue(),b'test')

if __name__=='__main__':unittest.main(verbosity=2)
