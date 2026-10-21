#!/usr/bin/env python3
"""Read-only evidence for native WeChat emoji. Never calls a send API or Frida."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

MAX_ASSET_BYTES=20*1024*1024


def inspect_message(message, asset_directory):
    records=[]
    root=Path(asset_directory).resolve()
    for item in message.get('message',[]):
        if not isinstance(item,dict) or item.get('type')!='face':
            continue
        record={'source_type':'face','asset_valid':False,'native_send_verified':False}
        records.append(record)
        data=item.get('data') or {}
        xml=data.get('text','')
        if not isinstance(xml,str) or len(xml)>65536 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
            record['error']='unsafe_xml';continue
        try:
            node=ET.fromstring(xml).find('emoji')
        except ET.ParseError:
            record['error']='invalid_xml';continue
        if node is None:
            record['error']='missing_emoji_metadata';continue
        md5=node.get('md5','')
        if not re.fullmatch('[0-9a-fA-F]{32}',md5):
            record['error']='invalid_md5';continue
        # Preserve the received identifier exactly; compare checksums case-insensitively only.
        record['md5']=md5
        record['emoji_format_type']=node.get('type','')
        for key in ('width','height'):
            raw=node.get(key,'')
            record[key]=int(raw) if raw.isdecimal() and len(raw)<6 else None
        uri=urlparse(str(data.get('url','')))
        if uri.scheme!='file' or uri.netloc not in ('','localhost'):
            record['error']='not_a_local_asset';continue
        asset=Path(unquote(uri.path)).resolve()
        if root not in asset.parents:
            record['error']='outside_asset_directory';continue
        if not asset.is_file():
            record['error']='missing_asset';continue
        size=asset.stat().st_size
        if not 0<size<=MAX_ASSET_BYTES:
            record['error']='asset_size_limit';continue
        with asset.open('rb') as file:
            content=file.read(MAX_ASSET_BYTES+1)
        if len(content)!=size:
            record['error']='asset_changed';continue
        if not content.startswith((b'GIF87a',b'GIF89a',b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff',b'RIFF')):
            record['error']='unsupported_image_format';continue
        if hashlib.md5(content).hexdigest()!=md5.lower():
            record['error']='md5_mismatch';continue
        record.update(asset_valid=True,bytes=size,path=str(asset),extension=asset.suffix)
        # Deliberately omit message sender, CDN URLs, tokens, AES keys and raw XML.
    return records


def inspect_log(log_path,asset_directory,max_log_bytes=16*1024*1024):
    path=Path(log_path)
    with path.open('rb') as file:
        file.seek(max(0,path.stat().st_size-max_log_bytes))
        lines=file.read().decode(errors='replace').splitlines()
    records={}
    seen_messages=0
    for line in lines:
        try:
            outer=json.loads(line)
        except ValueError:
            continue
        if not isinstance(outer,dict):
            continue
        candidates=[outer]
        for value in outer.values():
            if not isinstance(value,str) or not value.startswith('{'):
                continue
            try:
                candidates.append(json.loads(value))
            except ValueError:
                pass
        for message in candidates:
            if not isinstance(message,dict) or not isinstance(message.get('message'),list):
                continue
            for record in inspect_message(message,asset_directory):
                seen_messages+=1
                key=record.get('md5') or 'invalid-'+str(seen_messages)
                records[key]=record
    assets=list(records.values())
    return {'status':'partial','note':'接收素材校验，不是原生表情发送成功证明。未发送任何消息。',
            'face_events_in_log_slice':seen_messages,'unique_candidates':len(assets),
            'valid_local_assets':sum(item['asset_valid'] for item in assets),
            'native_send_verified':False,'assets':assets}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    base=Path(__file__).resolve().parents[2]
    parser.add_argument('--log',type=Path,default=base/'onebot/log/macos.log')
    parser.add_argument('--assets',type=Path,default=base/'onebot/image')
    parser.add_argument('--output',type=Path,default=base/'diagnostics/native-emoji-inventory.json')
    args=parser.parse_args()
    report=inspect_log(args.log,args.assets)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(dir=args.output.parent,prefix='.emoji-inventory-')
    try:
        with os.fdopen(fd,'w') as file:
            json.dump(report,file,ensure_ascii=False,indent=2)
        os.replace(temporary,args.output)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    print(json.dumps({key:value for key,value in report.items() if key!='assets'},ensure_ascii=False))
    print('report='+str(args.output))

if __name__=='__main__':main()
