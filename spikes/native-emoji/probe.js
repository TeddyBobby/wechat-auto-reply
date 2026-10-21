'use strict';
// Passive probe: observes known serializer/task locations; never calls a sender.
var handles = [];
var running = false;
var nativeTasks = {};
var serializerTasks = {};
var capturedTasks = {};
var commandCounts = {};
var cgiCounts = {};
var writerCalls = 0;
var serializerCalls = 0;
var matchedSerializerCalls = 0;
var captureCount = 0;
var responseCount = 0;
var expiresAt = 0;
var maximumFrames = 8;

function candidateMetadata(bytes) {
    var text = '';
    for (var i=0; i<bytes.length; i++) {
        var value = bytes[i];
        text += value >= 32 && value <= 126 ? String.fromCharCode(value) : ' ';
    }
    if (text.indexOf('filehelper') < 0) return null;
    var found = text.match(/(?:^|[^0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])/g) || [];
    var md5s = [];
    found.forEach(function (token) {
        var exact = token.match(/[0-9a-fA-F]{32}/)[0];
        if (md5s.indexOf(exact) < 0) md5s.push(exact);
    });
    return md5s.length ? {has_filehelper:true, md5s:md5s.slice(0,8)} : null;
}

function selectRecentTask(tasks, thread, now) {
    var found = null;
    Object.keys(tasks).forEach(function (key) {
        var task = tasks[key];
        if (task.thread === thread && now >= task.at && now-task.at <= 30000 && (!found || task.at > found.at)) found=task;
    });
    return found;
}

function active() {return running && Date.now()<expiresAt;}
function publicCgi(task) {
    function value(address) {
        var bytes=safeBytes(address,160);
        if (!bytes) return '';
        var text='';
        for (var i=0;i<bytes.length && bytes[i]!==0;i++) {
            if (bytes[i]<32 || bytes[i]>126) return '';
            text+=String.fromCharCode(bytes[i]);
        }
        return /^\/cgi-bin\/micromsg-bin\/[a-zA-Z0-9_./-]+$/.test(text) ? text : '';
    }
    try {
        var address=task.add(0x18).readPointer();
        var direct=value(address);
        if (direct) return direct;
        return value(address.readPointer());
    } catch (error) {return '';}
}
function safeBytes(address, length) {
    if (!address || address.isNull() || length<1 || length>65536) return null;
    try {return new Uint8Array(address.readByteArray(length));} catch (error) {return null;}
}
function executable(base, size, raw) {
    if (typeof raw!=='string' || !/^0x[0-9a-fA-F]+$/.test(raw)) throw new Error('invalid version offset');
    var offset=parseInt(raw,16);
    if (!Number.isSafeInteger(offset) || offset<0 || offset>=size) throw new Error('offset outside wechat module');
    var address=base.add(offset);
    var range=Process.findRangeByAddress(address);
    if (!range || range.protection.indexOf('x')<0) throw new Error('hook address is not executable');
    return address;
}
function finish() {
    handles.forEach(function (handle) {try {handle.detach();} catch (error) {}});
    handles=[];running=false;
    return status();
}
function status() {
    return {running:running,expires_at:expiresAt,hook_count:handles.length,
        native_task_count:Object.keys(nativeTasks).length,request_candidates:captureCount,
        responses:responseCount,command_counts:commandCounts,cgi_counts:cgiCounts,
        writer_calls:writerCalls,serializer_calls:serializerCalls,matched_serializer_calls:matchedSerializerCalls,
        target:'filehelper',read_only:true};
}

rpc.exports = {
    start:function (config) {
        if (running) throw new Error('probe already started');
        if (Process.arch!=='arm64') throw new Error('only the verified arm64 build is supported');
        var modules=Process.enumerateModules().filter(function (module) {return module.name==='wechat.dylib' && module.size>50000000;});
        modules.sort(function (a,b) {return b.size-a.size;});
        if (!modules.length) throw new Error('real wechat.dylib module not found');
        var module=modules[0];
        var offsets=config.offsets;
        var start=executable(module.base,module.size,offsets.sendFuncAddr).add(0x10);
        var enter=executable(module.base,module.size,offsets.req2bufEnterAddr);
        var leave=executable(module.base,module.size,offsets.req2bufExitAddr);
        var writer=executable(module.base,module.size,offsets.autoBufferWriteFunc);
        var responder=executable(module.base,module.size,offsets.buf2RespAddr);
        expiresAt=Date.now()+Math.min(Math.max(Number(config.seconds)||600,30),900)*1000;
        running=true;
        try {
            handles.push(Interceptor.attach(start, {onEnter:function () {
                if (!active()) return;
                try {
                    var task=this.context.x1;
                    var id=task.readU32();
                    var command=task.add(4).readU32();
                    if (command>0 && command<100000 && Object.keys(commandCounts).length<64) commandCounts[command]=(commandCounts[command]||0)+1;
                    if (command<=0 || command>=100000) return;
                    var cgi=publicCgi(task);
                    if (cgi && Object.keys(cgiCounts).length<64) cgiCounts[cgi]=(cgiCounts[cgi]||0)+1;
                    var now=Date.now();
                    Object.keys(nativeTasks).forEach(function (key) {if (now-nativeTasks[key].at>30000) delete nativeTasks[key];});
                    nativeTasks[id]={id:id,command:command,cgi:cgi,thread:this.threadId,at:now};
                    if (command===68 || /emoji|emoticon/.test(cgi)) send({kind:'native-task',task_id:id,command_id:command,cgi:cgi});
                } catch (error) {}
            }}));
            handles.push(Interceptor.attach(enter, {onEnter:function () {
                if (!active()) return;
                var id=this.context.x1.toUInt32();
                serializerCalls++;
                delete serializerTasks[this.threadId];
                if (!nativeTasks[id]) return;
                matchedSerializerCalls++;
                nativeTasks[id].thread=this.threadId;
                nativeTasks[id].at=Date.now();
                serializerTasks[this.threadId]=nativeTasks[id];
            }}));
            handles.push(Interceptor.attach(leave, {onEnter:function () {
                delete serializerTasks[this.threadId];
            }}));
            handles.push(Interceptor.attach(writer, {onEnter:function (args) {
                if (!active() || captureCount>=maximumFrames) return;
                writerCalls++;
                var task=selectRecentTask(serializerTasks,this.threadId,Date.now());
                if (!task) return;
                var bytes=safeBytes(args[1],args[2].toUInt32());
                if (!bytes) return;
                var metadata=candidateMetadata(bytes);
                if (!metadata) return;
                capturedTasks[task.id]=task;captureCount++;
                send({kind:'request-candidate',task_id:task.id,command_id:task.command,cgi:task.cgi,
                    bytes:bytes.length,has_filehelper:true,md5_count:metadata.md5s.length,
                    correlation:'task-id-at-req2buf-and-same-serializer-thread',
                    native_send_verified:false},bytes.buffer);
            }}));
            handles.push(Interceptor.attach(responder, {onEnter:function () {
                if (!active()) return;
                try {
                    var id=this.context.sp.add(0x140).readU32();
                    if (!capturedTasks[id]) return;
                    var bytes=safeBytes(this.context.x20,this.context.x0.toInt32());
                    if (!bytes) return;
                    responseCount++;
                    send({kind:'response',task_id:id,command_id:capturedTasks[id].command,cgi:capturedTasks[id].cgi,bytes:bytes.length,native_send_verified:false},bytes.buffer);
                } catch (error) {}
            }}));
        } catch (error) {finish();throw error;}
        return status();
    },
    status:status,
    finish:finish
};
