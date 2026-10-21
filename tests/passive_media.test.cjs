const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../onebot/script.js'),'utf8');
// Only evaluate pure helper declarations; do not execute native bootstrap code.
const start=script.indexOf('var passiveMediaCaptureRuntime = null;');
assert.notEqual(start,-1);
const source=script.slice(start);
function fixture(){
 const hooks=[],sent=[],memory=new Map();
 class P{constructor(n){this.n=BigInt(n)} add(n){return new P(this.n+BigInt(n))} equals(p){return this.n===p.n} isNull(){return this.n===0n} toString(){return '0x'+this.n.toString(16)} readPointer(){return memory.get(this.toString())||new P(0)} readU8(){for(const [key,value] of memory){if(typeof value!=='string')continue;let offset=this.n-BigInt(key);if(offset>=0n&&offset<=BigInt(value.length))return value.charCodeAt(Number(offset))||0}throw Error('unreadable')} readUtf8String(){return memory.get(this.toString())||''} readByteArray(n){let b=memory.get(this.toString());if(!b)throw Error('unreadable');return Uint8Array.from(b).slice(0,n).buffer}}
 const ptr=x=>new P(x);const base=ptr('0x100000000');
 const inst=new Map([[base.add(0x562d95c).toString(),{mnemonic:'bl',opStr:'#'+base.add(0x5851af0)}],[base.add(0x568e5d0).toString(),{mnemonic:'bl',opStr:'#'+base.add(0x5851af0)}],[base.add(0x5851af0).toString(),{mnemonic:'sub',opStr:'sp, sp, #0x30'}],[base.add(0x5851af4).toString(),{mnemonic:'stp',opStr:'x20, x19, [sp, #0x10]'}],[base.add(0x5851af8).toString(),{mnemonic:'stp',opStr:'x29, x30, [sp, #0x20]'}]]);
 const ctx={ptr,Process:{arch:'arm64'},Instruction:{parse:p=>inst.get(p.toString())||{mnemonic:'invalid',opStr:''}},NativeCallback:function(fn){return fn},CModule:function(code,symbols){this.code=code;this.symbols=symbols;this.on_enter={native:true}},Interceptor:{attach:(address,handlers)=>{hooks.push({address,handlers});return {detach(){}}}},isReadablePointer:p=>p&&!p.isNull(),readPointerIfReadable:p=>p.readPointer(),readByteArrayIfReadable:(p,n)=>p.readByteArray(n),send:o=>sent.push(o),console:{log(){},warn(){},error(){}}};
 vm.createContext(ctx);vm.runInContext(source,ctx);return {ctx,base,ptr,inst,memory,hooks,sent};
}
test('validates exact version sites and common real function before attaching once',()=>{
 const f=fixture();assert.equal(typeof f.ctx.installPassiveMediaCapture,'function');
 const r=f.ctx.installPassiveMediaCapture(f.base,f.base.add(0x562d95c),f.base.add(0x568e5d0),'2');
 assert.equal(f.hooks.length,1);assert.equal(f.hooks[0].address.toString(),f.base.add(0x5851af0).toString());
 assert.equal(f.hooks[0].handlers.onEnter,r.module.on_enter);
 assert.match(r.module.code,/gum_invocation_context_get_return_address/);assert.match(r.module.code,/cpu_context/);
 assert.doesNotMatch(r.module.code,/write|memcpy|NativeFunction/);
});
test('rejects mismatched branch targets/signatures and unsupported versions before hooking',()=>{
 for(const bad of ['branch','entry','version']){
  const f=fixture();if(bad==='branch')f.inst.set(f.base.add(0x568e5d0).toString(),{mnemonic:'bl',opStr:'#0x1234'});
  if(bad==='entry')f.inst.set(f.base.add(0x5851af0).toString(),{mnemonic:'ret',opStr:''});
  assert.throws(()=>f.ctx.installPassiveMediaCapture(f.base,f.base.add(0x562d95c),f.base.add(0x568e5d0),bad==='version'?'1':'2'),/media|version|signature/);
  assert.equal(f.hooks.length,0);
 }
});
test('only forwards bounded readable bytes with media correlation metadata',()=>{
 const f=fixture(),owner=f.ptr('0x200000000'),buffer=f.ptr('0x210000000'),id=f.ptr('0x220000000'),cdn=f.ptr('0x230000000');
 f.memory.set(owner.add(0x2f8).toString(),id);f.memory.set(owner.add(0x310).toString(),cdn);
 f.memory.set(id.toString(),'isolated-image-id');f.memory.set(cdn.toString(),'isolated-cdn-key');f.memory.set(buffer.toString(),[1,2,3]);
 const r=f.ctx.installPassiveMediaCapture(f.base,f.base.add(0x562d95c),f.base.add(0x568e5d0),'2');
 r.callback(buffer,3,owner);assert.equal(f.sent.length,1);assert.deepEqual(Array.from(f.sent[0].media),[1,2,3]);assert.equal(f.sent[0].cdn_url,'isolated-cdn-key');
 r.callback(buffer,{toNumber:()=>3},owner);assert.equal(f.sent.length,2,'Frida ulong arrives as UInt64, not a JS number');
 r.callback(buffer,9*1024*1024,owner);r.callback(f.ptr(0),3,owner);r.callback(buffer,3,f.ptr(0));
 f.memory.delete(cdn.toString());r.callback(buffer,3,owner);assert.equal(f.sent.length,2);
 f.memory.set(cdn.toString(),'a'.repeat(5000));r.callback(buffer,3,owner);assert.equal(f.sent.length,2);
});
