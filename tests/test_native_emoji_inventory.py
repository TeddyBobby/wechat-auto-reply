"""Read-only native emoji inventory; fixtures never send any WeChat message."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).parents[1]
MODULE=ROOT/'spikes/native-emoji/inventory.py'

class NativeEmojiInventoryTests(unittest.TestCase):
    def test_inventory_validates_native_metadata_and_omits_private_xml_fields(self):
        self.assertTrue(MODULE.exists(),'native emoji inventory is not implemented')
        spec=importlib.util.spec_from_file_location('emoji_inventory',MODULE)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);images=base/'image';images.mkdir()
            # Hand-built 1x1 GIF: header, screen, palette, one pixel, trailer.
            data=(b'GIF89a' + bytes([1,0,1,0,128,0,0])
                  + bytes([0,0,0,255,255,255])
                  + bytes([33,249,4,1,0,0,0,0,44,0,0,0,0,1,0,1,0,0,2,2,68,1,0,59]))
            asset=images/'fixture.gif';asset.write_bytes(data)
            md5=hashlib.md5(data).hexdigest()
            xml=f'<msg><emoji md5="{md5}" len="{len(data)}" type="2" width="1" height="1" cdnurl="https://private.invalid/?secret=abc" fromusername="wxid_private"/></msg>'
            message={'message':[{'type':'face','data':{'text':xml,'url':asset.as_uri()}}]}
            record=module.inspect_message(message,images)[0]
            self.assertEqual(record['md5'],md5)
            self.assertEqual(record['bytes'],len(data))
            self.assertTrue(record['asset_valid'])
            self.assertEqual(record['source_type'],'face')
            self.assertFalse(record['native_send_verified'])
            serialized=json.dumps(record)
            self.assertNotIn('private.invalid',serialized)
            self.assertNotIn('wxid_private',serialized)

    def test_outside_directory_symlink_and_md5_mismatch_are_rejected(self):
        self.assertTrue(MODULE.exists(),'native emoji inventory is not implemented')
        spec=importlib.util.spec_from_file_location('emoji_inventory',MODULE)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);images=base/'image';images.mkdir()
            outside=base/'outside.gif';outside.write_bytes(b'GIF89a-test-fixture')
            inside=images/'inside.gif';inside.symlink_to(outside)
            for uri in (outside.as_uri(),inside.as_uri()):
                record=module.inspect_message({'message':[{'type':'face','data':{'text':'<msg><emoji md5="'+'0'*32+'"/></msg>','url':uri}}]},images)[0]
                self.assertFalse(record['asset_valid'])
                self.assertEqual(record['error'],'outside_asset_directory')
            file=images/'mismatch.gif';file.write_bytes(b'GIF89a-test-fixture')
            record=module.inspect_message({'message':[{'type':'face','data':{'text':'<msg><emoji md5="'+'0'*32+'"/></msg>','url':file.as_uri()}}]},images)[0]
            self.assertFalse(record['asset_valid'])
            self.assertEqual(record['error'],'md5_mismatch')

    def test_text_xml_is_not_misclassified_and_declared_entities_are_rejected(self):
        self.assertTrue(MODULE.exists(),'native emoji inventory is not implemented')
        spec=importlib.util.spec_from_file_location('emoji_inventory',MODULE)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp)
            self.assertEqual(module.inspect_message({'message':[{'type':'text','data':{'text':'<msg><emoji/></msg>'}}]},base),[])
            result=module.inspect_message({'message':[{'type':'face','data':{'text':'<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///etc/passwd">]><msg><emoji/></msg>'}}]},base)[0]
            self.assertFalse(result['asset_valid'])
            self.assertEqual(result['error'],'unsafe_xml')

if __name__=='__main__':unittest.main()
