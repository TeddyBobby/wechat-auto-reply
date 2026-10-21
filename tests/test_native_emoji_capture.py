import importlib.util
from pathlib import Path
import tempfile
import unittest

PATH=Path(__file__).parents[1]/'spikes/native-emoji/capture.py'
def load():
    assert PATH.exists(),'read-only capture driver is not implemented'
    spec=importlib.util.spec_from_file_location('native_capture',PATH)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

class CaptureSafetyTests(unittest.TestCase):
    def test_capture_files_are_private_and_never_overwritten(self):
        module=load()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'request.bin'
            module.write_private(path,b'fixture-request')
            self.assertEqual(path.read_bytes(),b'fixture-request')
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(FileExistsError):module.write_private(path,b'replace')
    def test_wire_description_redacts_strings_and_rejects_truncation(self):
        module=load()
        private=b'private-session-token'
        fields=module.describe_frame(bytes([0x0a,len(private)])+private+bytes([0x10,47]))
        self.assertNotIn('private-session-token',str(fields))
        self.assertEqual(fields[0]['field'],1)
        self.assertEqual(fields[0]['length'],len(private))
        self.assertEqual(fields[1]['small_integer'],47)
        with self.assertRaises(ValueError):module.describe_frame(bytes([0x0a,20,1]))

if __name__=='__main__':unittest.main()
