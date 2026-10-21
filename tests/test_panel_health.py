import os
from pathlib import Path
import tempfile
from threading import Lock
import time
import unittest
from unittest.mock import patch
import panel_server

class HealthTests(unittest.TestCase):
    def test_old_manager_log_cannot_make_new_onebot_process_look_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'onebot/startup.log'
            path.parent.mkdir()
            path.write_text('[+] 捕获到 StartTask manager: 0x1234\n')
            os.utime(path,(time.time()-1000,time.time()-1000))
            ns={'HOME_DIR':directory,'port_open':lambda port:True,'ONEBOT_LISTEN':('127.0.0.1',36060),'model_lock':Lock(),'model_runtime':{'status':'unknown','pending':0,'detail':'尚未调用','last_call':None}}
            with patch.object(panel_server,'listener_pid',return_value=1234),patch.object(panel_server,'process_start_time',return_value=time.time()-10,create=True),patch.object(panel_server.shutil,'which',return_value='/bin/hermes'):
                services=panel_server.service_snapshot(ns)
            onebot=next(item for item in services if item['id']=='onebot')
            model=next(item for item in services if item['id']=='hermes')
            self.assertEqual(onebot['status'],'waiting')
            self.assertEqual(model['status'],'unknown')
            self.assertFalse(model['on'])

if __name__=='__main__':unittest.main()
