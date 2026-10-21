"""Offline checks for the candidate's account-scoped media output directory."""
import importlib.util,tempfile,hashlib,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).parents[1]
SPEC=importlib.util.spec_from_file_location('candidate_reconnect',ROOT/'reconnect_onebot.py')
candidate=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(candidate)
class StableImageCacheChecks(unittest.TestCase):
    def test_current_account_workspace_only_no_native_temp_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identity={'id':'wxid_fixture_current','folder':'wxid_fixture_current_ab12','wechat_pid':123}
            with patch.object(candidate,'ROOT',root),patch.object(candidate,'NativeAccountProbe',return_value=lambda:identity):
                actual,image=candidate.current_context()
            expected=root/'accounts'/hashlib.sha256(identity['id'].encode()).hexdigest()/'onebot/image'
            self.assertEqual(actual,identity);self.assertEqual(Path(image),expected.resolve())
            self.assertFalse(expected.exists(),'Read-only discovery may not mutate directories')
            from assistant_media import MediaStore
            expected.mkdir(parents=True);image_file=expected/'fixture.png';image_file.write_bytes(b'\x89PNG\r\n\x1a\n'+b'\0'*40)
            store=MediaStore(expected.parents[1]);self.assertEqual(store.import_local(str(image_file))['mime'],'image/png')
            other=root/'accounts'/'another_account'/'onebot'/'image';other.mkdir(parents=True)
            foreign=other/'foreign.png';foreign.write_bytes(image_file.read_bytes())
            with self.assertRaises(ValueError):store.import_local(str(foreign))
    def test_unknown_account_fails_closed(self):
        with patch.object(candidate,'NativeAccountProbe',return_value=lambda:{'id':''}):
            with self.assertRaises(RuntimeError):candidate.current_context()
    def test_symlink_cannot_point_to_another_account_or_external_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identity={'id':'wxid_fixture_current','folder':'wxid_fixture_current_ab12','wechat_pid':123}
            workspace=root/'accounts'/hashlib.sha256(identity['id'].encode()).hexdigest()
            workspace.mkdir(parents=True);other=root/'other';other.mkdir();(workspace/'onebot').symlink_to(other,target_is_directory=True)
            with patch.object(candidate,'ROOT',root),patch.object(candidate,'NativeAccountProbe',return_value=lambda:identity):
                with self.assertRaisesRegex(RuntimeError,'缓存路径'):candidate.current_context()
