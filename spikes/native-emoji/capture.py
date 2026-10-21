#!/usr/bin/env python3
"""Time-bounded, read-only Frida evidence capture. No sending or process-memory writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
from threading import Event,RLock
import time
import uuid


def write_private(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as file:file.write(data)


def describe_frame(data,depth=0):
    """Show field layout, not session keys, IDs, URLs, or arbitrary string values."""
    position=0
    def varint():
        nonlocal position
        value=0
        for offset in range(10):
            if position>=len(data):raise ValueError('truncated varint')
            byte=data[position];position+=1
            value|=(byte&127)<<(offset*7)
            if byte<128:return value
        raise ValueError('oversized varint')
    fields=[]
    while position<len(data):
        if len(fields)>=128:raise ValueError('too many fields')
        tag=varint();number=tag>>3;wire=tag&7
        if not number:raise ValueError('invalid field number')
        field={'field':number,'wire':wire}
        if wire==0:
            value=varint()
            if value<=255:field['small_integer']=value
            else:field['integer_bits']=value.bit_length()
        elif wire in (1,2,5):
            size=varint() if wire==2 else (8 if wire==1 else 4)
            if position+size>len(data):raise ValueError('truncated field')
            value=data[position:position+size];position+=size;field['length']=size
            if value==b'filehelper':field['public_value']='filehelper'
            if depth<3 and wire==2 and size and value[0] in (8,10,16,18):
                try:field['children']=describe_frame(value,depth+1)
                except ValueError:pass
        else:raise ValueError('unsupported wire type')
        fields.append(field)
    return fields


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temporary=tempfile.mkstemp(dir=path.parent,prefix='.probe-')
    try:
        with os.fdopen(fd,'w') as file:json.dump(value,file,ensure_ascii=False,indent=2)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=600)
    args=parser.parse_args()
    if not 30<=args.seconds<=900:parser.error('--seconds must be between 30 and 900')
    import frida
    base=Path(__file__).resolve().parents[2]
    root=base/'diagnostics/native-emoji-probe'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    run=root/(time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    run.mkdir(mode=0o700)
    config_path=base/'config.json'
    before=hashlib.sha256(config_path.read_bytes()).hexdigest()
    offsets_path=Path(os.environ.get('WECHAT_CONFIG', str(base/'onebot/wechat_version/4_1_12_53_mac.json'))).expanduser()
    offsets=json.loads(offsets_path.read_text())
    state={'run_dir':str(run),'ready':False,'read_only':True,'target':'filehelper',
           'native_send_verified':False,'started_at':time.time(),'seconds':args.seconds,
           'frames':[],'native_tasks':0,'state':'initializing'}
    lock=RLock();stop=Event();session=None;script=None;last_response=0
    def save():
        with lock:
            atomic_json(run/'status.json',state)
            atomic_json(root/'latest.json',state)
    def message_callback(message,data):
        nonlocal last_response
        with lock:
            if message.get('type')=='error':
                state['error']=message.get('description','probe script error')
                state['state']='error';stop.set();save();return
            payload=message.get('payload') or {}
            if not isinstance(payload,dict):return
            kind=payload.get('kind')
            if kind=='native-task':
                state['native_tasks']+=1;save();return
            if kind not in ('request-candidate','response') or not data:return
            if len(data)>65536:return
            number=len(state['frames'])+1
            path=run/(str(number).zfill(2)+'-'+kind+'.bin')
            write_private(path,data)
            item={**payload,'file':str(path),'sha256':hashlib.sha256(data).hexdigest()}
            try:item['wire_layout']=describe_frame(data)
            except ValueError as error:item['wire_layout_error']=str(error)
            state['frames'].append(item)
            if kind=='response':last_response=time.monotonic()
            state['state']='capturing';save()
            print(json.dumps({'event':kind,'task_id':payload.get('task_id'),'bytes':len(data),'file':path.name}),flush=True)
    def detached(reason,crash=None):
        with lock:
            state['detach_reason']=str(reason)
            if reason!='application-requested':stop.set()
    def on_signal(signum,frame):stop.set()
    signal.signal(signal.SIGTERM,on_signal);signal.signal(signal.SIGINT,on_signal)
    try:
        device=frida.get_device_manager().add_remote_device('127.0.0.1:27042')
        processes=[process for process in device.enumerate_processes() if process.name=='Gadget']
        if len(processes)!=1:raise RuntimeError('expected one verified WeChat Gadget process')
        state['target_pid']=processes[0].pid
        session=device.attach(processes[0].pid)
        session.on('detached',detached)
        script=session.create_script(Path(__file__).with_name('probe.js').read_text())
        script.on('message',message_callback)
        script.load()
        snapshot=script.exports_sync.start({'offsets':offsets,'seconds':args.seconds})
        if snapshot.get('hook_count')!=5 or not snapshot.get('running'):raise RuntimeError('probe did not arm all expected observers')
        with lock:
            state.update(ready=True,state='waiting_for_mac_filehelper_emoji',observer=snapshot)
            save()
        # Read back the exact injected observer before announcing readiness.
        actual=script.exports_sync.status()
        if not actual.get('running') or actual.get('hook_count')!=5:raise RuntimeError('probe read-back verification failed')
        print(json.dumps({'event':'ready','target':'filehelper','pid':state['target_pid'],'hook_count':5,'read_only':True,'expires_in_seconds':args.seconds,'status_file':str(run/'status.json')},ensure_ascii=False),flush=True)
        deadline=time.monotonic()+args.seconds
        while time.monotonic()<deadline and not stop.wait(1):
            snapshot=script.exports_sync.status()
            with lock:
                state['observer']=snapshot
                save()
            if last_response and time.monotonic()-last_response>=10:break
    except Exception as error:
        with lock:state.update(state='error',error=str(error));save()
        raise
    finally:
        if script is not None:
            try:script.exports_sync.finish()
            except Exception:pass
            try:script.unload()
            except Exception:pass
        if session is not None:
            try:session.detach()
            except Exception:pass
        with lock:
            state['ready']=False
            state['finished_at']=time.time()
            state['config_unchanged']=hashlib.sha256(config_path.read_bytes()).hexdigest()==before
            if state['state']!='error':state['state']='captured' if state['frames'] else 'finished_without_sample'
            save()
        print(json.dumps({'event':'finished','state':state['state'],'frames':len(state['frames']),'config_unchanged':state['config_unchanged'],'native_send_verified':False},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
