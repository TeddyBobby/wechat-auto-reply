const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path').join(__dirname, '../onebot/script.js');
const source = fs.readFileSync(path, 'utf8');
function getFunction(name) {
  const start = source.indexOf('function ' + name + '(');
  assert.notEqual(start, -1);
  const next = source.indexOf('\nfunction ', start + 1);
  return source.slice(start, next < 0 ? source.length : next).split('\nrpc.exports')[0];
}
function harness() {
  let hook, call;
  const allocations = [];
  class Ptr {
    constructor(buf, off = 0) { this.buf = buf; this.off = off; }
    add(n) { return new Ptr(this.buf, this.off + n); }
    writeByteArray(a) { Buffer.from(a).copy(this.buf, this.off); }
    readByteArray(n) { return this.buf.subarray(this.off, this.off+n); }
    writeU32(n) { this.buf.writeUInt32LE(n, this.off); }
    writeU64(n) { this.buf.writeBigUInt64LE(BigInt(n), this.off); }
    writePointer(p) { this.buf.writeBigUInt64LE(BigInt(p.off), this.off); }
    isNull() { return false; }
  }
  const alloc = n => { const p = new Ptr(Buffer.alloc(n)); allocations.push(p); return p; };
  const c = {
    console: { log(){}, error(){} },
    Memory: { alloc }, ptr: () => alloc(1024), uint64: n => BigInt(n),
    Interceptor: { attach(addr, cb) { hook = cb; } },
    NativeFunction: function () { return (x0, payload) => { call = { x0, payload }; return 1; }; },
    setTimeout(){ return 1; },
    patchString(){}, hexToByteArray: hex => new Uint8Array(Buffer.from(hex, 'hex')),
    sendFuncAddr: alloc(16), fakeVtable: alloc(512), taskIdGlobal: 0x20000000,
    textCgiAddr: alloc(128), textMessageAddr: alloc(256), sendTextMessageAddr: alloc(256),
    textProtoHexGlobal:'', textProtoDataAddr:null,
    triggerX0:null, triggerX1Payload:null, triggerTaskSnapshot:null,
    ownedTaskPayloads:{}, sendMsgType:'',
  };
  for(const kind of ['img','video','reply','voice','file','fileUpload','appAttach']) {
    c[kind+'MessageAddr']=alloc(256); c[kind+'CgiAddr']=alloc(128);
    c['send'+kind[0].toUpperCase()+kind.slice(1)+'MessageAddr']=alloc(256);
  }
  vm.createContext(c);
  vm.runInContext(getFunction('setupSendTextMessageDynamic')+'\n'+getFunction('AttachSendFunc')+'\n'+getFunction('triggerSendMediaMessage'), c);
  c.setupSendTextMessageDynamic();
  c.AttachSendFunc();
  const nativeTask = alloc(1024);
  nativeTask.buf.fill(0x5a);
  const before = Buffer.from(nativeTask.buf);
  hook.onEnter.call({context:{x0:alloc(64),x1:nativeTask}});
  return {c,nativeTask,before,call:()=>call};
}
test('late acknowledgements cannot satisfy another task or a timed-out request', () => {
  const c=vm.createContext({activeDirectTask:{taskId:12,deadline:15000}});
  vm.runInContext(getFunction('isCurrentDirectAck'),c);
  assert.equal(c.isCurrentDirectAck(12,14000),true);
  assert.equal(c.isCurrentDirectAck(11,14000),false);
  assert.equal(c.isCurrentDirectAck(12,15001),false);
});

test('server ack requires an explicit valid ret and exposes the server message id', () => {
  const c = vm.createContext({});
  vm.runInContext(getFunction('readWireFields')+'\n'+getFunction('summarizeSendAck'),c);
  // Minimal synthetic protobuf: BaseResponse.ret=0, text receipt message id=12345.
  const bytes=Buffer.from([0x0a, 0x02, 0x08, 0x00, 0x1a, 0x03, 0x40, 0xb9, 0x60]);
  const ack=c.summarizeSendAck(bytes,'text');
  assert.equal(ack.ret,0);
  assert.equal(ack.serverMessageId,'12345');
  assert.throws(()=>c.summarizeSendAck(Buffer.from('0801','hex'),'text'));
  assert.throws(()=>c.summarizeSendAck(Buffer.from('0a04080012','hex'),'text'));
});

test('direct send never overwrites a captured native task', () => {
  const h=harness();
  assert.equal(h.c.triggerSendMediaMessage(0x20000001,'','filehelper','0801','01'.repeat(416),'text'),'1');
  assert.deepEqual(h.nativeTask.buf,h.before,'native task memory was overwritten');
  assert.notEqual(h.call().payload.buf,h.nativeTask.buf);
});
test('owned task has a clean tail, not a stale task snapshot', () => {
  const h=harness();
  h.c.triggerSendMediaMessage(0x20000001,'','filehelper','0801','01'.repeat(416),'text');
  assert.deepEqual(h.call().payload.buf.subarray(416),Buffer.alloc(1024-416));
});
