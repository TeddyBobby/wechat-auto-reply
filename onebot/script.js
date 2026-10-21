var targetPath = "/Applications/WeChat.app/Contents/MacOS/WeChat";
var module = Process.enumerateModules().find(function(m) {
    return m.path === targetPath || m.name === "WeChat";
});
if (!module) {
    throw new Error("[-] Cannot find module: " + targetPath);
}
var moduleBase = module.base;
console.log("[+] WeChat module base: " + moduleBase);

// 基址解析(2026-09-20 E类事故修正): 模块表优先。真身 wechat.dylib 是全进程唯一
// >50MB 的同名模块(Frameworks/下是16KB stub), 基址确定无竞态。
// 原 "req2buf字符串+>100MB range" 扫描存在竞态误命中: 堆里 MallocHelperZone
// (合并range>100MB, rw-) 也有 "req2buf" 字符串拷贝, 扫描回调先到就赢 →
// 基址定到堆上, 全部 hook 挂空, 登录后零事件(静默死亡, 无报错)。
var searchSize = 1000 * 1024 * 1024;
var searchEnd = moduleBase.add(searchSize);
var _req2bufSearchAddr = null;
var baseAddr = null;

// 结构版本开关: 4.1.12 起上传完成结构 +0x08、下载任务结构 +0x18、寄存器漂移。
// JSON 里 "structVer": "2" = 4.1.12 布局; 旧版 JSON 无此键(渲染为 <no value>),
// 不等于 "2", 自动走 4.1.11 及以前的原路径。
var structVer = "{{.structVer}}";

function resolveBaseFromModuleTable() {
    var wechatModules = Process.enumerateModules().filter(function(m) {
        return m.name === "wechat.dylib";
    });
    wechatModules.sort(function(a, b) { return b.size - a.size; });
    if (wechatModules.length > 0 && wechatModules[0].size > 50 * 1024 * 1024) {
        return wechatModules[0];
    }
    return null;
}

// 必须 setImmediate 派发: initAddresses 内部依赖脚本中部 var 全局的初始化
// (fakeVtable 等)。同步调用会抢在 var 初始化前执行, var 随后又把已赋值的
// 全局重置回 ptr(0) —— 2026-09-20 D类 文本发送崩溃(fakeVtable=0 虚调用)即此因。
setImmediate(function () {
    var realModule = resolveBaseFromModuleTable();
    if (realModule) {
        baseAddr = realModule.base;
        console.log("[+] 基址解析(模块表): " + realModule.path + " base=" + baseAddr + " size=" + realModule.size);
        initAddresses();
    } else {
        console.log("[!] 模块表未找到真身 wechat.dylib, 回退 req2buf 字符串扫描");
        resolveBaseByScan();
    }
});

function resolveBaseByScan() {
var ranges = Process.enumerateRanges("r--").filter(function(r) {
    var rangeEnd = r.base.add(r.size);
    return r.base.compare(searchEnd) < 0 && rangeEnd.compare(moduleBase) > 0;
});

console.log("[+] Found " + ranges.length + " readable ranges within 1000MB window");

var pending = ranges.length;
if (pending === 0) {
    throw new Error("[-] No readable ranges found within 1000MB from module base");
}

ranges.forEach(function(r) {
    Memory.scan(r.base, r.size, "72 65 71 32 62 75 66", {
        onMatch: function(address, size) {
            if (_req2bufSearchAddr === null) {
                var rangeInfo = Process.findRangeByAddress(address);
                if (rangeInfo) {
                    // 必须是可执行映射: 排除堆区(MallocHelperZone等)里的字符串拷贝
                    if (rangeInfo.size > 100 * 1024 * 1024 && rangeInfo.protection.indexOf("x") !== -1) {
                        _req2bufSearchAddr = address;
                        console.log("[+] Range size > 100MB & executable, accepted as base address");
                    }
                }
            }
        },
        onError: function(reason) {
            // skip unreadable sub-pages
        },
        onComplete: function() {
            pending--;
            if (pending === 0) {
                if (_req2bufSearchAddr === null) {
                    throw new Error("[-] Cannot find 'req2buf' keyword in an executable range > 100MB");
                }

                var foundRange = Process.findRangeByAddress(_req2bufSearchAddr);
                baseAddr = foundRange.base;
                console.log("[+] Base address from range: " + baseAddr);
                console.log("[+] Range size: " + foundRange.size);

                initAddresses();
            }
        }
    });
});
}

function initAddresses() {
    // 文本消息全局变量 (new_text.js approach)
    blrX8Addr = baseAddr.add({{.blrX8Addr}});
    autoBufferWriteFunc = baseAddr.add({{.autoBufferWriteFunc}});

    // 双方公共使用的地址
    req2bufEnterAddr = baseAddr.add({{.req2bufEnterAddr}});
    req2bufExitAddr = baseAddr.add({{.req2bufExitAddr}});
    sendFuncAddr = baseAddr.add({{.sendFuncAddr}});
    buf2RespAddr = baseAddr.add({{.buf2RespAddr}});

    uploadImageAddr = baseAddr.add({{.uploadImageAddr}});
    cndOnCompleteAddr = baseAddr.add({{.cndOnCompleteAddr}});
    // 冷启动 CdnManager 解析(可选, 旧版本 JSON 无此键则保持 ptr(0), 走 hook 捕获老路)
    {{if .cdnGetServiceAddr}}cdnGetServiceAddr = baseAddr.add({{.cdnGetServiceAddr}});{{end}}
    {{if .cdnManagerGetterAddr}}cdnManagerGetterAddr = baseAddr.add({{.cdnManagerGetterAddr}});{{end}}

    uploadGetCallbackWrapperAddr = baseAddr.add({{.uploadGetCallbackWrapperAddr}});
    uploadGetCallbackWrapperFuncAddr = baseAddr.add({{.uploadGetCallbackWrapperFuncAddr}});
    uploadOnCompleteAddr = baseAddr.add({{.uploadOnCompleteAddr}});
    uploadOnCompleteFuncAddr = baseAddr.add({{.uploadOnCompleteFuncAddr}});
    downloadImagAddr = baseAddr.add({{.downloadImagAddr}});
    startDownloadMedia = baseAddr.add({{.startDownloadMedia}});
    downloadFileAddr = baseAddr.add({{.downloadFileAddr}});
    downloadVideoAddr = baseAddr.add({{.downloadVideoAddr}});

	sendMessageCallbackFunc = baseAddr.add(0x0);
	imgMessageCallbackFunc = baseAddr.add(0x0);
	videoMessageCallbackFunc = baseAddr.add(0x0);
    replyMessageCallbackFunc = baseAddr.add(0x0);
    voiceMessageCallbackFunc = baseAddr.add(0x0);

    setupRetOneStub();  // 必须同步先执行，初始化fakeVtable
    setImmediate(setupSendTextMessageDynamic);
    setImmediate(setupSendFileMessageDynamic);
    setImmediate(setupSendFileUploadMessageDynamic);
    setImmediate(setupSendAppAttachMessageDynamic);
    setImmediate(attachBlrX8Hook);
    setImmediate(AttachSendFunc);
    setImmediate(attachReq2buf);
    setImmediate(setupSendImgMessageDynamic);
    setImmediate(attachUploadMedia);
    setImmediate(patchCdnOnComplete);
    setImmediate(attachGetCallbackFromWrapper);
    setImmediate(setupSendReplyMessageDynamic);
    setImmediate(setupDownloadFileDynamic);
    setImmediate(setReceiver);
}

// -------------------------基础函数分区-------------------------
function hexToByteArray(hexStr) {
    var bytes = [];
    for (var i = 0; i < hexStr.length; i += 2) {
        bytes.push(parseInt(hexStr.substr(i, 2), 16));
    }
    return bytes;
}

function patchString(addr, plainStr) {
    const bytes = [];
    for (let i = 0; i < plainStr.length; i++) {
        bytes.push(plainStr.charCodeAt(i));
    }

    addr.writeByteArray(bytes);
    addr.add(bytes.length).writeU8(0);
}

function generateAESKey() {
    const chars = 'abcdef0123456789';
    let key = '';
    for (let i = 0; i < 32; i++) {
        key += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return key;
}

const MAX_FRIDA_MESSAGE_BYTES = 4 * 1024 * 1024;

function isReadablePointer(addr) {
    try {
        if (!addr || addr.isNull()) {
            return false;
        }
        const range = Process.findRangeByAddress(addr);
        return range !== null && range.protection.indexOf('r') !== -1;
    } catch (e) {
        return false;
    }
}

function readPointerIfReadable(addr) {
    try {
        if (!isReadablePointer(addr)) {
            return ptr(0);
        }
        const value = addr.readPointer();
        if (!isReadablePointer(value)) {
            return ptr(0);
        }
        return value;
    } catch (e) {
        return ptr(0);
    }
}

function readUtf8StringIfReadable(addr) {
    try {
        if (!isReadablePointer(addr)) {
            return "";
        }
        return addr.readUtf8String();
    } catch (e) {
        return "";
    }
}

function readByteArrayIfReadable(addr, len) {
    try {
        if (len <= 0 || !isReadablePointer(addr)) {
            return null;
        }
        return addr.readByteArray(len);
    } catch (e) {
        return null;
    }
}

function sendDownloadChunks(dataPtr, dataLen, fileId, cdnUrl) {
    if (!cdnUrl || dataLen <= 0) {
        return;
    }

	if (dataLen > 0 && dataLen <= 10 * 1024 * 1024) {
		var buffer = dataPtr.readByteArray(dataLen);
		var uint8Array = new Uint8Array(buffer);

		send({
			type: "download",
			media: Array.from(uint8Array),
			file_id: fileId,
			cdn_url: cdnUrl,
		})
	}
}

// mars::cdn::CdnManager 单例解析: 上传(uploadGlobalX0)/下载(downloadGlobalX0)共用的 this。
// 2026-09-02 静态分析 4.1.10: 上传/下载分发链(0x4e5a6e4/0x4e5a7f4)都走
// GetService("default")[0x4ca2130] -> 按类型名 "N4mars3cdn10CdnManagerE" getter[0x4e59dec]
// -> [ctx+0x40]。该单例登录后即注册进全局服务表, 不需要先发一张图片触发。
// ⚠️ 2026-09-03 事故教训: 登录未完成时服务表锁被登录流程持有, 此时在 Frida 线程调
// GetService 会与微信主线程死锁, 微信整个冻结。因此必须有"登录稳定门禁":
// 只在 (收到过任意同步消息 = 确已登录) 或 (脚本已跑 60s) 之后才允许解析。
var scriptLoadTime = Date.now();
var incomingTrafficSeen = false;
function loginSettled() {
    if (incomingTrafficSeen) return true;
    if (Date.now() - scriptLoadTime > 60 * 1000) return true;
    return false;
}
function resolveCdnManager() {
    if (cdnGetServiceAddr.equals(ptr(0)) || cdnManagerGetterAddr.equals(ptr(0))) {
        return ptr(0);
    }
    if (!loginSettled()) {
        console.log("[!] 登录尚未稳定, 暂缓 CdnManager 解析(防死锁), 稍后任务重试");
        return ptr(0);
    }
    try {
        // libc++ SSO 短字符串: 数据在 +0, 长度写在 +0x17 (对照 wechat.dylib std::string ctor)
        var strDefault = Memory.alloc(24);
        strDefault.writeUtf8String("default");
        strDefault.add(0x17).writeU8(7);

        var getService = new NativeFunction(cdnGetServiceAddr, 'pointer', ['pointer']);
        var svc = getService(strDefault);
        if (!isReadablePointer(svc)) {
            console.error("[!] GetService(\"default\") 返回不可读: " + svc);
            return ptr(0);
        }
        var getCtx = new NativeFunction(cdnManagerGetterAddr, 'pointer', ['pointer']);
        var ctx = getCtx(svc);
        if (!isReadablePointer(ctx)) {
            console.error("[!] CdnManager getter 返回不可读: " + ctx);
            return ptr(0);
        }
        var mgr = readPointerIfReadable(ctx.add(0x40));
        if (!isReadablePointer(mgr)) {
            console.error("[!] ctx+0x40 管理器指针不可读: ctx=" + ctx);
            return ptr(0);
        }
        return mgr;
    } catch (e) {
        console.error("[!] resolveCdnManager 异常: " + e);
        return ptr(0);
    }
}

// CdnManager 兜底: 1) hook 已捕获的互回填(同一单例) 2) 都没有则走服务定位器解析
function ensureCdnManagerX0() {
    if (uploadGlobalX0.equals(ptr(0)) && downloadGlobalX0) {
        uploadGlobalX0 = downloadGlobalX0;
        console.log("[+] downloadGlobalX0 回填 uploadGlobalX0: " + uploadGlobalX0);
    }
    if (!downloadGlobalX0 && !uploadGlobalX0.equals(ptr(0))) {
        downloadGlobalX0 = uploadGlobalX0;
        console.log("[+] uploadGlobalX0 回填 downloadGlobalX0: " + downloadGlobalX0);
    }
    if (uploadGlobalX0.equals(ptr(0))) {
        var mgr = resolveCdnManager();
        if (!mgr.equals(ptr(0))) {
            uploadGlobalX0 = mgr;
            if (!downloadGlobalX0) {
                downloadGlobalX0 = mgr;
            }
            console.log("[+] 冷启动服务定位器解析 CdnManager: " + mgr);
        }
    }
    return !uploadGlobalX0.equals(ptr(0));
}

function fillUploadX1AndStart(idAddr, pathAddr, x1Buffer, receiver, md5, filePath, payloadHex) {
    if (uploadGlobalX0.equals(ptr(0))) {
        ensureCdnManagerX0();
    }
    if (uploadGlobalX0.equals(ptr(0))) {
        console.error("[!] uploadGlobalX0 尚未初始化，请等待 hook 捕获");
        return "fail";
    }

    const payload = hexToByteArray(payloadHex);
    patchString(idAddr, receiver + "_" + String(Math.floor(Date.now() / 1000)) + "_" + Math.floor(Math.random() * 1001) + "_1");
    patchString(md5Addr, md5);
    patchString(uploadAesKeyAddr, generateAESKey());
    patchString(pathAddr, filePath);

    x1Buffer.writeByteArray(payload);
    x1Buffer.writePointer(uploadFunc1Addr);
    x1Buffer.add(0x08).writePointer(uploadFunc2Addr);
    x1Buffer.add(0x48).writePointer(idAddr);
    x1Buffer.add(0x68).writeUtf8String(receiver);
    x1Buffer.add(0xa8).writePointer(md5Addr);
    x1Buffer.add(0xe8).writePointer(pathAddr);
    x1Buffer.add(0x118).writePointer(pathAddr);
    x1Buffer.add(0x148).writePointer(pathAddr);
    x1Buffer.add(0x200).writePointer(uploadAesKeyAddr);

    const startUploadMedia = new NativeFunction(uploadImageAddr, 'int64', ['pointer', 'pointer']);
    return startUploadMedia(uploadGlobalX0, x1Buffer);
}

// -------------------------基础函数分区-------------------------

// -------------------------全局变量分区-------------------------

// 文本消息全局变量 (new_text.js approach)
var blrX8Addr;
var autoBufferWriteFunc;
var textCgiAddr = ptr(0);
var sendTextMessageAddr = ptr(0);
var textMessageAddr = ptr(0);
var sendMessageCallbackFunc;
var retOneStub = ptr(0);
var fakeVtable = ptr(0);
// 等待buf2resp的任务表: taskId -> { addr, msgType, timerId }
// 支持多任务并存 + 超时兜底: ack迟迟不来时提前清零 X24+0x60, 避免mars
// 回收死任务时对伪造结构体做虚调用/delete导致SIGSEGV (2026-08-17 crash)
var pendingBuf2RespTasks = {};
// 正常ack在1s内返回; 3s未回视为失败。必须赶在mars短链CGI失败窗口(~5s)之前
// 复原原始指针: 8/20崩溃即任务无ack, ~5s失败回调在协程线程erase任务map时踩坏
// 节点, 10s兜底来不及。original指针本就是sendFunc构造的合法消息, 提前复原=
// 回到原生行为; 迟到ack仍能命中(entry保留30s), 只是entry.addr已空不再清理
var PENDING_CLEANUP_TIMEOUT_MS = 3 * 1000;
var textProtoDataAddr = ptr(0);


// 双方公共使用的地址
var triggerX1Payload;
var ownedTaskPayloads = {};
var activeDirectTask = { taskId: 0, deadline: 0 };
var triggerX0;
var req2bufEnterAddr;
var req2bufExitAddr;
var sendFuncAddr;
var insertMsgAddr = ptr(0);
var originalInsertMsgPtr = ptr(0);  // hook前X24+0x60的原始消息指针, 超时兜底时复原
var sendMsgType = "";
var buf2RespAddr;

var uploadImageAddr;
var cdnGetServiceAddr = ptr(0);      // GetService(std::string) 服务定位器, 冷启动解析 CdnManager 用
var cdnManagerGetterAddr = ptr(0);   // 按类型名 "N4mars3cdn10CdnManagerE" 取 ctx 的 getter
var cndOnCompleteAddr;
var imgMessageCallbackFunc;
var videoMessageCallbackFunc;

var uploadGetCallbackWrapperAddr;
var uploadGetCallbackWrapperFuncAddr;
var uploadOnCompleteAddr;
var uploadOnCompleteFuncAddr;
var downloadImagAddr;
var startDownloadMedia;
var downloadFileAddr;
var downloadVideoAddr;

var downloadGlobalX0;
var downloadFileX1 = ptr(0)
var fileIdAddr = ptr(0)
var downloadAesKeyAddr = ptr(0)
var filePathAddr = ptr(0)
var fileCdnUrlAddr = ptr(0)
var uploadImageX1 = ptr(0);
var imgCgiAddr = ptr(0);
var sendImgMessageAddr = ptr(0);
var imgMessageAddr = ptr(0);
var uploadGlobalX0 = ptr(0)
var uploadFunc1Addr = ptr(0)
var uploadFunc2Addr = ptr(0)
var imageIdAddr = ptr(0)
var md5Addr = ptr(0)
var uploadAesKeyAddr = ptr(0)
var ImagePathAddr1 = ptr(0)
var uploadCallback = ptr(0)

var videoCgiAddr = ptr(0);
var sendVideoMessageAddr = ptr(0);
var videoMessageAddr = ptr(0);
var uploadVideoX1 = ptr(0);
var videoIdAddr = ptr(0);
var videoPathAddr1 = ptr(0)

// 语音消息全局变量
var voiceMessageCallbackFunc;
var voiceCgiAddr = ptr(0);
var sendVoiceMessageAddr = ptr(0);
var voiceMessageAddr = ptr(0);
var uploadVoiceX1 = ptr(0);
var voiceIdAddr = ptr(0);
var voicePathAddr1 = ptr(0);
var voiceProtoHexGlobal = "";
var voiceDurationGlobal = 0;
var voiceSilkDataLenGlobal = 0;
var voiceAudioDataAddr = ptr(0);


// 发送消息的全局变量
var taskIdGlobal = 0x20000090 // 最好比较大，不和原始的微信消息重复

// 文本消息protobuf全局变量 (从Go直接传入hex编码)
var textProtoHexGlobal = "";
// 图片消息protobuf全局变量 (从Go直接传入hex编码)
var imgProtoHexGlobal = "";
// 视频消息protobuf全局变量 (从Go直接传入hex编码)
var videoProtoHexGlobal = "";
// 回复消息protobuf全局变量 (从Go直接传入hex编码)
var replyProtoHexGlobal = "";
// 文件消息protobuf全局变量 (从Go直接传入hex编码)
var fileProtoHexGlobal = "";
var fileUploadProtoHexGlobal = "";
// uploadappattach protobuf全局变量 (从Go直接传入hex编码)
var appAttachProtoHexGlobal = "";

// 文件消息全局变量
var fileCgiAddr = ptr(0);
var sendFileMessageAddr = ptr(0);
var fileMessageAddr = ptr(0);
var uploadFileIdAddr = ptr(0);
var uploadFileX1 = ptr(0);

// sendfileuploadmsg 全局变量
var fileUploadCgiAddr = ptr(0);
var sendFileUploadMessageAddr = ptr(0);
var fileUploadMessageAddr = ptr(0);

// uploadappattach 全局变量
var appAttachCgiAddr = ptr(0);
var sendAppAttachMessageAddr = ptr(0);
var appAttachMessageAddr = ptr(0);

// 回复消息全局变量
var replyMessageCallbackFunc;
var replyCgiAddr = ptr(0);
var sendReplyMessageAddr = ptr(0);
var replyMessageAddr = ptr(0);

// -------------------------全局变量分区-------------------------


// -------------------------发送文本消息分区-------------------------
// 初始化进行内存的分配
function setupSendTextMessageDynamic() {
    // 动态分配内存

    textCgiAddr = Memory.alloc(128);
    sendTextMessageAddr = Memory.alloc(256);
    textMessageAddr = Memory.alloc(256);
    textProtoDataAddr = Memory.alloc(64 * 1024); // 支持 50KB 分片(uploadappattach)的 protobuf

    // A. 写入字符串内容
    patchString(textCgiAddr, "/cgi-bin/micromsg-bin/newsendmsg");

    // B. 构建 sendTextMessageAddr 结构体 (X24 基址位置)
    sendTextMessageAddr.add(0x00).writeU64(0);
    sendTextMessageAddr.add(0x08).writeU64(0);
    sendTextMessageAddr.add(0x10).writeU64(0);
    sendTextMessageAddr.add(0x18).writeU64(1);
    sendTextMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendTextMessageAddr.add(0x28).writePointer(textMessageAddr);

    // C. 构建 Message 结构体
    textMessageAddr.add(0x00).writePointer(fakeVtable);
    textMessageAddr.add(0x08).writeU32(taskIdGlobal);
    textMessageAddr.add(0x0c).writeU32(0x20a);
    textMessageAddr.add(0x10).writeU64(0x3);
    textMessageAddr.add(0x18).writePointer(textCgiAddr);
    textMessageAddr.add(0x20).writeU64(uint64("0x20"));

    console.log("[+] Dynamic Text Message Setup Complete.");
}


// -------------------------发送文件消息分区-------------------------
function setupSendFileMessageDynamic() {
    fileCgiAddr = Memory.alloc(128);
    sendFileMessageAddr = Memory.alloc(256);
    fileMessageAddr = Memory.alloc(256);
    uploadFileIdAddr = Memory.alloc(128);
    uploadFileX1 = Memory.alloc(1024);
    patchString(uploadFileIdAddr, "file_upload_not_init");

    patchString(fileCgiAddr, "/cgi-bin/micromsg-bin/sendappmsg");

    sendFileMessageAddr.add(0x00).writeU64(0);
    sendFileMessageAddr.add(0x08).writeU64(0);
    sendFileMessageAddr.add(0x10).writeU64(0);
    sendFileMessageAddr.add(0x18).writeU64(1);
    sendFileMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendFileMessageAddr.add(0x28).writePointer(fileMessageAddr);

    fileMessageAddr.add(0x00).writePointer(fakeVtable);
    fileMessageAddr.add(0x08).writeU32(taskIdGlobal);
    fileMessageAddr.add(0x0c).writeU32(0x6e);
    fileMessageAddr.add(0x10).writeU64(0x3);
    fileMessageAddr.add(0x18).writePointer(fileCgiAddr);
    fileMessageAddr.add(0x20).writeU64(0x20);
    fileMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    fileMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));
}

function triggerSendFileMessage(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "file");
}

function triggerUploadFile(receiver, md5, filePath, payloadHex) {
    return fillUploadX1AndStart(uploadFileIdAddr, ImagePathAddr1, uploadFileX1, receiver, md5, filePath, payloadHex);
}

// -------------------------sendfileuploadmsg分区-------------------------
function setupSendFileUploadMessageDynamic() {
    fileUploadCgiAddr = Memory.alloc(128);
    sendFileUploadMessageAddr = Memory.alloc(256);
    fileUploadMessageAddr = Memory.alloc(256);

    patchString(fileUploadCgiAddr, "/cgi-bin/micromsg-bin/sendfileuploadmsg");

    sendFileUploadMessageAddr.add(0x00).writeU64(0);
    sendFileUploadMessageAddr.add(0x08).writeU64(0);
    sendFileUploadMessageAddr.add(0x10).writeU64(0);
    sendFileUploadMessageAddr.add(0x18).writeU64(1);
    sendFileUploadMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendFileUploadMessageAddr.add(0x28).writePointer(fileUploadMessageAddr);

    fileUploadMessageAddr.add(0x00).writePointer(fakeVtable);
    fileUploadMessageAddr.add(0x08).writeU32(taskIdGlobal);
    fileUploadMessageAddr.add(0x0c).writeU32(0x6e);
    fileUploadMessageAddr.add(0x10).writeU64(0x3);
    fileUploadMessageAddr.add(0x18).writePointer(fileUploadCgiAddr);
    fileUploadMessageAddr.add(0x20).writeU64(0x20);
    fileUploadMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    fileUploadMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));
}

function triggerSendFileUploadMessage(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "fileupload");
}

// -------------------------uploadappattach分区-------------------------
function setupSendAppAttachMessageDynamic() {
    appAttachCgiAddr = Memory.alloc(128);
    sendAppAttachMessageAddr = Memory.alloc(256);
    appAttachMessageAddr = Memory.alloc(256);

    patchString(appAttachCgiAddr, "/cgi-bin/micromsg-bin/uploadappattach");

    sendAppAttachMessageAddr.add(0x00).writeU64(0);
    sendAppAttachMessageAddr.add(0x08).writeU64(0);
    sendAppAttachMessageAddr.add(0x10).writeU64(0);
    sendAppAttachMessageAddr.add(0x18).writeU64(1);
    sendAppAttachMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendAppAttachMessageAddr.add(0x28).writePointer(appAttachMessageAddr);

    appAttachMessageAddr.add(0x00).writePointer(fakeVtable);
    appAttachMessageAddr.add(0x08).writeU32(taskIdGlobal);
    appAttachMessageAddr.add(0x0c).writeU32(0x6e);
    appAttachMessageAddr.add(0x10).writeU64(0x3);
    appAttachMessageAddr.add(0x18).writePointer(appAttachCgiAddr);
    appAttachMessageAddr.add(0x20).writeU64(0x25);
    appAttachMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    appAttachMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));
}

function triggerUploadAppAttach(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "appattach");
}

// -------------------------发送文件消息分区-------------------------



// 创建一个只返回1的小函数stub
function setupRetOneStub() {
    retOneStub = Memory.alloc(Process.pageSize);
    Memory.patchCode(retOneStub, 8, code => {
        // MOV W0, #1 = 0x52800020, RET = 0xD65F03C0 (little-endian)
        code.writeByteArray([0x20, 0x00, 0x80, 0x52, 0xC0, 0x03, 0x5F, 0xD6]);
    });
    console.log("[+] Return-1 stub created at: " + retOneStub);

    // 构造假vtable：所有槽位指向retOneStub，这样mars对我们伪造结构做虚调用时不会崩溃
    fakeVtable = Memory.alloc(512);
    for (var i = 0; i < 64; i++) {
        fakeVtable.add(i * 8).writePointer(retOneStub);
    }
    console.log("[+] Fake vtable created at: " + fakeVtable);
}

function attachBlrX8Hook() {
    console.log("[+] Hooking BLR X8 at: " + blrX8Addr);

    var nativeAutoBufferWrite = new NativeFunction(autoBufferWriteFunc, 'int', ['pointer', 'pointer', 'int']);

    Interceptor.attach(blrX8Addr, {
        onEnter: function(args) {
            var currentTaskId = this.context.x20.toUInt32();
            if (currentTaskId !== taskIdGlobal) {
                return;
            }

            console.log("[+] BLR X8 命中! taskId=" + currentTaskId + " sendMsgType=" + sendMsgType);

            var autoBuffer = this.context.x1;
            var protoHex = "";

            if (sendMsgType === "text") {
                protoHex = textProtoHexGlobal;
            } else if (sendMsgType === "img") {
                protoHex = imgProtoHexGlobal;
            } else if (sendMsgType === "video") {
                protoHex = videoProtoHexGlobal;
            } else if (sendMsgType === "reply") {
                protoHex = replyProtoHexGlobal;
            } else if (sendMsgType === "file") {
                protoHex = fileProtoHexGlobal;
            } else if (sendMsgType === "fileupload") {
                protoHex = fileUploadProtoHexGlobal;
            } else if (sendMsgType === "appattach") {
                protoHex = appAttachProtoHexGlobal;
            } else if (sendMsgType === "voice") {
                protoHex = voiceProtoHexGlobal;
            }

            if (!protoHex || protoHex.length === 0) {
                console.error("[!] protoHex 为空, sendMsgType=" + sendMsgType);
                return;
            }

            var finalPayload = hexToByteArray(protoHex);
            textProtoDataAddr.writeByteArray(finalPayload);

            // 调用 autoBufferWrite(autoBuffer, data, len) 填充 v133
            nativeAutoBufferWrite(autoBuffer, textProtoDataAddr, finalPayload.length);
            console.log("[+] autoBufferWrite 调用完成, protobuf长度: " + finalPayload.length);

            // 将 X8 指向 retOneStub，这样 BLR X8 只会返回1，不执行原始逻辑
            this.context.x8 = retOneStub;
        }
    });
}


function triggerSendTextMessage(taskId, receiver, content, atUser, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, "", receiver, protoHex, payloadHex, "text");
}

// Unknown/malformed protobuf responses must never be treated as successful delivery.
function isCurrentDirectAck(taskId, now) {
    return taskId === activeDirectTask.taskId && now <= activeDirectTask.deadline;
}

function readWireFields(data) {
    var pos = 0;
    var fields = {};
    function readVarint() {
        var value = 0n;
        for (var i = 0; i < 10 && pos < data.length; i++) {
            var b = data[pos++];
            value |= BigInt(b & 127) << BigInt(i * 7);
            if (!(b & 128)) return value;
        }
        throw new Error("invalid protobuf varint");
    }
    while (pos < data.length) {
        var tag = Number(readVarint());
        var field = tag >>> 3;
        var wire = tag & 7;
        if (!field) throw new Error("invalid protobuf field");
        var value;
        if (wire === 0) value = readVarint();
        else if (wire === 2 || wire === 1 || wire === 5) {
            var size = wire === 2 ? Number(readVarint()) : (wire === 1 ? 8 : 4);
            if (size < 0 || !Number.isSafeInteger(size) || pos + size > data.length) {
                throw new Error("truncated protobuf field");
            }
            value = data.slice(pos, pos + size);
            pos += size;
        } else throw new Error("unsupported protobuf wire type");
        if (!fields[field]) fields[field] = { wire: wire, value: value };
    }
    return fields;
}

function summarizeSendAck(data, msgType) {
    var fields = readWireFields(data);
    var base = fields[msgType === "voice" ? 10 : 1];
    if (!base || base.wire !== 2) throw new Error("missing BaseResponse");
    var ret = readWireFields(base.value)[1];
    if (!ret || ret.wire !== 0) throw new Error("missing explicit server ret");
    var result = { ret: Number(BigInt.asIntN(32, ret.value)), serverMessageId: "" };
    if (msgType === "text" && result.ret === 0) {
        var body = fields[3];
        if (!body || body.wire !== 2) throw new Error("missing text message receipt");
        var id = readWireFields(body.value)[8];
        if (!id || id.wire !== 0 || id.value === 0n) throw new Error("missing server message id");
        result.serverMessageId = id.value.toString();
    }
    return result;
}

function AttachSendFunc() {
    Interceptor.attach(sendFuncAddr.add(0x10), {
        onEnter: function (args) {

            // x0 是 STNManager；x1 属于微信，返回后可能被释放，绝不能复用/覆写。
            triggerX0 = this.context.x0;
            console.log(`[+] 捕获到 StartTask manager: ${triggerX0}`);
        }
    })
}


// -------------------------发送文本消息分区-------------------------


// -------------------------buf2resp超时兜底分区-------------------------
// req2bufExit后登记待ack任务: 命中buf2resp时清理指针并取消timer;
// 超时未命中则复原X24+0x60的原始指针(而不是清零/留着伪造结构体),
// 任务回到未注入的合法状态, mars无论重试重序列化还是超时回收delete都安全
function armPendingBuf2RespTask(taskId, addr, msgType, originalPtr) {
    pendingBuf2RespTasks[taskId] = {
        addr: addr,
        msgType: msgType,
        originalPtr: originalPtr || ptr(0),
        timerId: setTimeout(function () {
            fallbackCleanupPendingTask(taskId);
        }, PENDING_CLEANUP_TIMEOUT_MS),
    };
}

// ack命中: 取消timer并移除登记, 返回entry供调用方读取msgType
function finishPendingBuf2RespTask(taskId) {
    var entry = pendingBuf2RespTasks[taskId];
    if (!entry) {
        return null;
    }
    delete pendingBuf2RespTasks[taskId];
    if (entry.timerId !== null) {
        clearTimeout(entry.timerId);
        entry.timerId = null;
    }
    return entry;
}

// 超时兜底: 复原X24+0x60为原始消息指针(与成功路径写0不同, 此时任务
// 可能仍被mars重试, 必须留合法对象)。entry再保留30s, 迟到的ack
// 仍能匹配并把响应转发给Go (此时entry.addr已空, 只转发不再清理)
function fallbackCleanupPendingTask(taskId) {
    var entry = pendingBuf2RespTasks[taskId];
    if (!entry) {
        return;
    }
    entry.timerId = null;
    try {
        if (!entry.originalPtr.isNull()) {
            entry.addr.writePointer(entry.originalPtr);
            console.log("[!] buf2resp超时兜底: 已复原原始消息指针, msgType=" + entry.msgType + " taskId=" + taskId);
        } else {
            entry.addr.writeU64(0x0);
            console.log("[!] buf2resp超时兜底: 原始指针不可用, 已清零 insertMsgAddr, msgType=" + entry.msgType + " taskId=" + taskId);
        }
    } catch (e) {
        console.error("[!] buf2resp超时兜底清理失败: taskId=" + taskId + " err=" + e);
    }
    entry.addr = ptr(0);
    setTimeout(function () {
        delete pendingBuf2RespTasks[taskId];
    }, 30 * 1000);
}

// -------------------------Req2Buf公共部分分区-------------------------
function attachReq2buf() {
    Interceptor.attach(req2bufEnterAddr, {
        onEnter: function (args) {
            if (!this.context.x1.equals(taskIdGlobal)) {
                return;
            }

            const x24_base = this.context.x24;
            insertMsgAddr = x24_base.add(0x60);
            // 保存hook前的原始消息指针(校验过可读), 超时兜底时复原,
            // 让任务回到未注入的合法状态, mars重试/回收/delete都不会踩到伪造结构体
            originalInsertMsgPtr = readPointerIfReadable(insertMsgAddr);

            if (sendMsgType === "text") {
                insertMsgAddr.writePointer(sendTextMessageAddr);
                console.log("[+] 发送文本消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendTextMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "img") {
                insertMsgAddr.writePointer(sendImgMessageAddr);
                console.log("[+] 发送图片消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendImgMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "video") {
                insertMsgAddr.writePointer(sendVideoMessageAddr);
                console.log("[+] 发送视频消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendVideoMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "reply") {
                insertMsgAddr.writePointer(sendReplyMessageAddr);
                console.log("[+] 发送回复消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendReplyMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "voice") {
                insertMsgAddr.writePointer(sendVoiceMessageAddr);
                console.log("[+] 发送语音消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendVoiceMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "file") {
                insertMsgAddr.writePointer(sendFileMessageAddr);
                console.log("[+] 发送文件消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendFileMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "fileupload") {
                insertMsgAddr.writePointer(sendFileUploadMessageAddr);
                console.log("[+] 发送fileUploadMsg成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendFileUploadMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            } else if (sendMsgType === "appattach") {
                insertMsgAddr.writePointer(sendAppAttachMessageAddr);
                console.log("[+] 发送uploadAppAttach成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendAppAttachMessageAddr +
                    "[+] Req2Buf 写入后内存预览: " + insertMsgAddr);
            }
        }
    });

    // 在出口处拦截req2buf，记录insertMsgAddr等buf2resp回调后再清理
    Interceptor.attach(req2bufExitAddr, {
        onEnter: function (args) {
            if (!this.context.x25.equals(taskIdGlobal)) {
                return;
            }
            // 不立即清除insertMsgAddr，让mars能路由buf2resp回调
            // 用fakeVtable保护结构体，防止中间被访问时崩溃
            // 登记任务并挂超时兜底timer: ack超时则复原X24+0x60原始指针
            armPendingBuf2RespTask(taskIdGlobal, insertMsgAddr, sendMsgType, originalInsertMsgPtr);
            taskIdGlobal = 0;
        }
    });
}


// -------------------------Req2Buf公共部分分区-------------------------

// -------------------------发送图片消息分区-------------------------

// 初始化进行内存的分配
function setupSendImgMessageDynamic() {

    // 1. 动态分配内存块（按需分配大小）
    // 分配原则：字符串给 64-128 字节，结构体按实际大小分配
    imgCgiAddr = Memory.alloc(128);
    sendImgMessageAddr = Memory.alloc(256);
    imgMessageAddr = Memory.alloc(256);
    uploadFunc1Addr = Memory.alloc(24);
    uploadFunc2Addr = Memory.alloc(24);
    uploadCallback = Memory.alloc(128);
    imageIdAddr = Memory.alloc(256);
    md5Addr = Memory.alloc(256);
    uploadAesKeyAddr = Memory.alloc(256);
    ImagePathAddr1 = Memory.alloc(256);
    uploadImageX1 = Memory.alloc(1024);

    // 图片数据写入
    patchString(imgCgiAddr, "/cgi-bin/micromsg-bin/uploadmsgimg");

    sendImgMessageAddr.add(0x00).writeU64(0);
    sendImgMessageAddr.add(0x08).writeU64(0);
    sendImgMessageAddr.add(0x10).writeU64(0);
    sendImgMessageAddr.add(0x18).writeU64(1);
    sendImgMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendImgMessageAddr.add(0x28).writePointer(imgMessageAddr);

    imgMessageAddr.add(0x00).writePointer(fakeVtable);
    imgMessageAddr.add(0x08).writeU32(taskIdGlobal);
    imgMessageAddr.add(0x0c).writeU32(0x6e);
    imgMessageAddr.add(0x10).writeU64(0x3);
    imgMessageAddr.add(0x18).writePointer(imgCgiAddr);
    imgMessageAddr.add(0x20).writeU64(0x22);
    imgMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    imgMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));

    // 视频数据写入
    videoCgiAddr = Memory.alloc(128);
    sendVideoMessageAddr = Memory.alloc(256);
    videoMessageAddr = Memory.alloc(256);
    videoIdAddr = Memory.alloc(256);
    videoPathAddr1 = Memory.alloc(256);
    uploadVideoX1 = Memory.alloc(1024);

    patchString(videoCgiAddr, "/cgi-bin/micromsg-bin/uploadvideo");

    sendVideoMessageAddr.add(0x00).writeU64(0);
    sendVideoMessageAddr.add(0x08).writeU64(0);
    sendVideoMessageAddr.add(0x10).writeU64(0);
    sendVideoMessageAddr.add(0x18).writeU64(1);
    sendVideoMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendVideoMessageAddr.add(0x28).writePointer(videoMessageAddr);

    videoMessageAddr.add(0x00).writePointer(fakeVtable);
    videoMessageAddr.add(0x08).writeU32(taskIdGlobal);
    videoMessageAddr.add(0x0c).writeU32(0x6e);
    videoMessageAddr.add(0x10).writeU64(0x3);
    videoMessageAddr.add(0x18).writePointer(videoCgiAddr);
    videoMessageAddr.add(0x20).writeU64(0x21);
    videoMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    videoMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));

    // 语音数据写入
    voiceCgiAddr = Memory.alloc(128);
    sendVoiceMessageAddr = Memory.alloc(256);
    voiceMessageAddr = Memory.alloc(256);
    voiceIdAddr = Memory.alloc(256);
    voicePathAddr1 = Memory.alloc(256);
    uploadVoiceX1 = Memory.alloc(1024);
    voiceAudioDataAddr = Memory.alloc(5 * 1024 * 1024); // 预分配5MB

    patchString(voiceCgiAddr, "/cgi-bin/micromsg-bin/uploadvoice");

    sendVoiceMessageAddr.add(0x00).writeU64(0);
    sendVoiceMessageAddr.add(0x08).writeU64(0);
    sendVoiceMessageAddr.add(0x10).writeU64(0);
    sendVoiceMessageAddr.add(0x18).writeU64(1);
    sendVoiceMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendVoiceMessageAddr.add(0x28).writePointer(voiceMessageAddr);

    voiceMessageAddr.add(0x00).writePointer(fakeVtable);
    voiceMessageAddr.add(0x08).writeU32(taskIdGlobal);
    voiceMessageAddr.add(0x0c).writeU32(0x6e);
    voiceMessageAddr.add(0x10).writeU64(0x3);
    voiceMessageAddr.add(0x18).writePointer(voiceCgiAddr);
    voiceMessageAddr.add(0x20).writeU64(0x21);
    voiceMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    voiceMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));
}



function triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, msgType) {
    if (!taskId || !receiver) {
        console.error("[!] " + msgType + ": taskId or receiver is empty!");
        return "fail";
    }

    if (!triggerX0 || triggerX0.isNull()) {
        console.error("[!] StartTask manager 尚未就绪，等待微信产生一次网络任务后再发送");
        return "fail";
    }

    var msgAddrInfo = {
        "text":  { messageAddr: textMessageAddr,  sendMessageAddr: sendTextMessageAddr,  cgiAddr: textCgiAddr,  protoHexSetter: function(h) { textProtoHexGlobal = h; } },
        "img":   { messageAddr: imgMessageAddr,   sendMessageAddr: sendImgMessageAddr,   cgiAddr: imgCgiAddr,   protoHexSetter: function(h) { imgProtoHexGlobal = h; } },
        "video": { messageAddr: videoMessageAddr, sendMessageAddr: sendVideoMessageAddr, cgiAddr: videoCgiAddr, protoHexSetter: function(h) { videoProtoHexGlobal = h; } },
        "reply": { messageAddr: replyMessageAddr, sendMessageAddr: sendReplyMessageAddr, cgiAddr: replyCgiAddr, protoHexSetter: function(h) { replyProtoHexGlobal = h; } },
        "voice": { messageAddr: voiceMessageAddr, sendMessageAddr: sendVoiceMessageAddr, cgiAddr: voiceCgiAddr, protoHexSetter: function(h) { voiceProtoHexGlobal = h; } },
        "file":  { messageAddr: fileMessageAddr,  sendMessageAddr: sendFileMessageAddr,  cgiAddr: fileCgiAddr,  protoHexSetter: function(h) { fileProtoHexGlobal = h; } },
        "fileupload": { messageAddr: fileUploadMessageAddr, sendMessageAddr: sendFileUploadMessageAddr, cgiAddr: fileUploadCgiAddr, protoHexSetter: function(h) { fileUploadProtoHexGlobal = h; } },
        "appattach": { messageAddr: appAttachMessageAddr, sendMessageAddr: sendAppAttachMessageAddr, cgiAddr: appAttachCgiAddr, protoHexSetter: function(h) { appAttachProtoHexGlobal = h; } },
    };

    var info = msgAddrInfo[msgType];
    if (!info) {
        console.error("[!] unknown msgType: " + msgType);
        return "fail";
    }

    info.protoHexSetter(protoHex);
    taskIdGlobal = taskId;

    info.messageAddr.add(0x08).writeU32(taskIdGlobal);
    info.sendMessageAddr.add(0x20).writeU32(taskIdGlobal);

    const payloadData = hexToByteArray(payloadHex);
    if (payloadData.length !== 0x1a0) {
        console.error("[!] invalid task payload length: " + payloadData.length);
        return "fail";
    }
    // 每次发送独立的零内存。模板包含 taskId，整体从 +0 写入，不能再偏移 4 字节。
    triggerX1Payload = Memory.alloc(1024);
    ownedTaskPayloads[taskId] = triggerX1Payload;
    setTimeout(function () { delete ownedTaskPayloads[taskId]; }, 60 * 1000);
    triggerX1Payload.writeByteArray(payloadData);
    triggerX1Payload.add(0x18).writePointer(info.cgiAddr);
    triggerX1Payload.add(0xb8).writePointer(triggerX1Payload.add(0xc0));
    triggerX1Payload.add(0x190).writePointer(triggerX1Payload.add(0x198));
    sendMsgType = msgType;
    activeDirectTask = { taskId: taskId, deadline: Date.now() + 14000 };

    const MMStartTask = new NativeFunction(sendFuncAddr, 'int64', ['pointer', 'pointer']);

    try {
        MMStartTask(triggerX0, triggerX1Payload);
        return "1";
    } catch (e) {
        console.error("[!] Error trigger " + msgType + " MMStartTask: " + e);
        return "fail";
    }
}

function triggerSendImgMessage(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "img");
}

function triggerSendVideoMessage(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "video");
}


function triggerUploadImg(receiver, md5, imagePath, payloadHex) {
    return fillUploadX1AndStart(imageIdAddr, ImagePathAddr1, uploadImageX1, receiver, md5, imagePath, payloadHex);
}

function triggerUploadVideo(receiver, md5, videoPath, payloadHex) {
    return fillUploadX1AndStart(videoIdAddr, videoPathAddr1, uploadVideoX1, receiver, md5, videoPath, payloadHex);
}

function triggerUploadVoice(receiver, voicePath, payloadHex, audioDataHex, durationMs) {
    if (uploadGlobalX0.equals(ptr(0))) {
        ensureCdnManagerX0();
    }
    if (uploadGlobalX0.equals(ptr(0))) {
        console.error("[!] uploadGlobalX0 尚未初始化，请等待 hook 捕获");
        return "fail";
    }

    voiceDurationGlobal = durationMs;
    const payload = hexToByteArray(payloadHex);

    // 解码音频二进制数据，写入预分配的5MB内存
    const audioBytes = hexToByteArray(audioDataHex);
    const audioLen = audioBytes.length;
    voiceSilkDataLenGlobal = audioLen;
    voiceAudioDataAddr.writeByteArray(audioBytes);

    const voiceIdStr = receiver + "_" + String(Math.floor(Date.now() / 1000)) + "_" + Math.floor(Math.random() * 1001) + "_1";
    patchString(voiceIdAddr, voiceIdStr);
    patchString(voicePathAddr1, voicePath);

    uploadVoiceX1.writeByteArray(payload);
    uploadVoiceX1.writePointer(uploadFunc1Addr);
    uploadVoiceX1.add(0x08).writePointer(uploadFunc2Addr);
    uploadVoiceX1.add(0x48).writePointer(voiceIdAddr);
    uploadVoiceX1.add(0x50).writeU64(voiceIdStr.length);
    uploadVoiceX1.add(0x58).writeU64(uint64("0x8000000000000000").add(voiceIdStr.length + 1));
    uploadVoiceX1.add(0x68).writeUtf8String(receiver);
    // 音频二进制数据: 0x100=指针, 0x108=长度, 0x110=容量(长度+1)|高位
    uploadVoiceX1.add(0x100).writePointer(voiceAudioDataAddr);
    uploadVoiceX1.add(0x108).writeU64(audioLen);
    uploadVoiceX1.add(0x110).writeU64(uint64("0x8000000000000000").add(audioLen + 1));

    const startUploadMedia = new NativeFunction(uploadImageAddr, 'int64', ['pointer', 'pointer']);

    return startUploadMedia(uploadGlobalX0, uploadVoiceX1);
}

function attachUploadMedia() {
    Interceptor.attach(uploadImageAddr.add(0x10), {
        onEnter: function (args) {
			uploadGlobalX0 = this.context.x0;
			if (!downloadGlobalX0) {
				// 上传/下载共用同一 mars::cdn::CdnManager 单例, 顺手回填
				downloadGlobalX0 = this.context.x0;
				console.log("[+] 上传hook回填 downloadGlobalX0: " + downloadGlobalX0);
			}
		}
    })
}



// 视频上传成功钥匙缓存: cdnKey -> { aesKey, md5Key, videoId }
// CDN 秒传去重的响应不带 aesKey, 按 cdnKey 命中回填 (见 patchCdnOnComplete)
var cdnVideoKeyCache = {};

// Go 启动时回灌持久化(./cdn_video_keys.json)的钥匙, 解决缓存跨进程丢失:
// onebot 重启后同一视频首次上传必撞秒传去重(响应无 aesKey), 内存缓存为空
// 就只能 abort → send timeout (2026-09-07 四次实锤)
function hydrateCdnVideoCache(jsonStr) {
    var persisted = JSON.parse(jsonStr);
    var n = 0;
    for (var k in persisted) {
        if (persisted.hasOwnProperty(k) && persisted[k] && persisted[k].aesKey && !cdnVideoKeyCache[k]) {
            cdnVideoKeyCache[k] = persisted[k];
            n++;
        }
    }
    console.log("[+] hydrateCdnVideoCache: 回灌 " + n + " 条视频钥匙");
    return n;
}

function patchCdnOnComplete() {
    Interceptor.attach(cndOnCompleteAddr, {
        onEnter: function (args) {

            try {
                const x2 = this.context.x2;
                // 4.1.12(structVer=2): 完成结构整体 +0x08 (DIAG 实证: fileId 0x20→0x28,
                // cdnKey 0x60→0x68, aesKey 0x78→0x80, md5Key 0x90→0x98, targetId 0x40→0x48);
                // 且 videoId 字段整个消失(宽扫 0x00-0x260 无候选, 见 docs/version-upgrade.md
                // 铁律9), structVer=2 直接传空串
                const cndShift = (structVer === "2") ? 0x08 : 0;
                const currentFileId = x2.add(0x20 + cndShift).readPointer().readUtf8String();
                const imageFileId = imageIdAddr.readUtf8String();
                const videoFileId = videoIdAddr.readUtf8String();
                const voiceFileId = voiceIdAddr.readUtf8String();
                const fileUploadFileId = uploadFileIdAddr.readUtf8String();
                if (currentFileId !== imageFileId && currentFileId !== videoFileId && currentFileId !== voiceFileId && currentFileId !== fileUploadFileId) {
                    console.log("[-] CndOnComplete x2: " + x2 + " currentFileId: " + currentFileId +
                        " imageFileId: " + imageFileId + " videoFileId:" + videoFileId + " voiceFileId:" + voiceFileId + " fileUploadFileId:" + fileUploadFileId);
                    return;
                }

                const cdnKey = x2.add(0x60 + cndShift).readPointer().readUtf8String();
                const aesKey = x2.add(0x78 + cndShift).readPointer().readUtf8String();
                const md5Key = x2.add(0x90 + cndShift).readPointer().readUtf8String();
                const videoId = (structVer === "2") ? "" : x2.add(0xf0).readPointer().readUtf8String();
                const targetId = x2.add(0x40 + cndShift).readUtf8String();

                console.log("cndOnComplete x2: " + x2 + " cdnKey: " + cdnKey + " aesKey: " + aesKey + " md5Key: " + md5Key + " videoId: " + videoId + " targetId: " + targetId);

                if (cdnKey !== "" && cdnKey != null && aesKey !== "" && aesKey != null) {

                    // 判断是语音、视频、文件还是图片
                    if (currentFileId === voiceFileId) {
                        // 语音
                        send({
                            type: "upload_voice_finish",
                            target_id: targetId,
                            cdn_key: cdnKey,
                            aes_key: aesKey,
                            voice_duration: voiceDurationGlobal,
                            silk_data_len: voiceSilkDataLenGlobal
                        });
                    } else if (currentFileId === fileUploadFileId) {
                        // 文件
                        var attachId = "@cdn_" + cdnKey + "_" + aesKey + "_1";
                        send({
                            type: "upload_file_finish",
                            target_id: targetId,
                            cdn_key: cdnKey,
                            aes_key: aesKey,
                            md5_key: md5Key,
                            attach_id: attachId,
                            file_upload_token: "",
                            overwrite_msg_id: ""
                        });
                    } else if (currentFileId === videoFileId) {
                        // 视频: 缓存成功上传的钥匙, 供秒传去重时回填
                        // videoId || "" 兜底: null 会让 Go 侧 videoId.(string) panic
                        // (被 main.go recover 吞掉, 表现为 HTTP 超时假象); proto3 空 bytes
                        // 字段会被省略, 4.1.12 实测服务端 ack、视频可播放
                        cdnVideoKeyCache[cdnKey] = { aesKey: aesKey, md5Key: md5Key, videoId: videoId || "" };
                        send({
                            type: "upload_video_finish",
                            target_id: targetId,
                            cdn_key: cdnKey,
                            aes_key: aesKey,
                            md5_key: md5Key,
                            video_id: videoId || ""
                        });
                    } else {
                        // 图片
                        send({
                            type: "upload_image_finish",
                            target_id: targetId,
                            cdn_key: cdnKey,
                            aes_key: aesKey,
                            md5_key: md5Key
                        });
                    }
                } else if (currentFileId === videoFileId && cdnKey !== "" && cdnKey != null && cdnVideoKeyCache[cdnKey]) {
                    // CDN 秒传去重: 同一视频重复上传时服务端直接返回已有 filekey
                    // (cdnKey 相同), 但响应不带 aesKey/md5Key (2026-08-27 先发个人再发群,
                    // 群发送三次全部死在 "cdnKey or aesKey 为空")。文件就是上次我们自己传的,
                    // 回填缓存钥匙即可正确解密; videoId 优先用本次响应里的(秒传响应会带)。
                    var cached = cdnVideoKeyCache[cdnKey];
                    console.log("[+] cndOnComplete 秒传命中, 回填缓存钥匙 cdnKey: " + cdnKey);
                    send({
                        type: "upload_video_finish",
                        target_id: targetId,
                        cdn_key: cdnKey,
                        aes_key: cached.aesKey,
                        md5_key: cached.md5Key,
                        video_id: (videoId !== "" && videoId != null) ? videoId : (cached.videoId || "")
                    });
                } else {
                    console.error("cdnKey or aesKey 为空");
                }
            } catch (e) {
                console.error("[-] CdnOnComplete error: " + e);
            }
        }
    });
}


function attachGetCallbackFromWrapper() {
    Interceptor.attach(uploadGetCallbackWrapperAddr, {
        onEnter: function (args) {
            try {
                const tmpFileId = this.context.x1.readPointer().readUtf8String();
                const imageFileId = imageIdAddr.readUtf8String();
                const videoFileId = videoIdAddr.readUtf8String();
                const voiceFileId = voiceIdAddr.readUtf8String();
                const fileUploadFileId = uploadFileIdAddr.readUtf8String();
                if (tmpFileId !== imageFileId && tmpFileId !== videoFileId && tmpFileId !== voiceFileId && tmpFileId !== fileUploadFileId) {
                    console.log("[+] GetCallbackFromWrapper tmpFileId: " + tmpFileId + " imageFileId: " + imageFileId + " videoFileId:" + videoFileId + " voiceFileId:" + voiceFileId + " fileUploadFileId:" + fileUploadFileId);
                    return
                }

                uploadCallback.add(0x10).writePointer(uploadGetCallbackWrapperFuncAddr);
                this.context.x8 = uploadCallback;
            } catch (e) {
                console.error("[-] GetCallbackFromWrapper error: " + e);
            }
        }
    })

    Interceptor.attach(uploadOnCompleteAddr, {
        onEnter: function (args) {
            try {
                const tmpFileId = this.context.x1.readPointer().readUtf8String();
                const imageFileId = imageIdAddr.readUtf8String();
                const videoFileId = videoIdAddr.readUtf8String();
                const voiceFileId = voiceIdAddr.readUtf8String();
                const fileUploadFileId = uploadFileIdAddr.readUtf8String();
                if (tmpFileId !== imageFileId && tmpFileId !== videoFileId && tmpFileId !== voiceFileId && tmpFileId !== fileUploadFileId) {
                    console.log("[+] OnComplete tmpFileId: " + tmpFileId + " imageFileId: " + imageFileId + " videoFileId:" + videoFileId + " voiceFileId:" + voiceFileId + " fileUploadFileId:" + fileUploadFileId);
                    return
                }

                uploadCallback.add(0x30).writePointer(uploadOnCompleteFuncAddr);
                this.context.x8 = uploadCallback;
            } catch (e) {
                console.error("[-] OnComplete error: " + e);
            }
        }
    })
}


// -------------------------发送回复消息分区-------------------------
function setupSendReplyMessageDynamic() {
    replyCgiAddr = Memory.alloc(128);
    sendReplyMessageAddr = Memory.alloc(256);
    replyMessageAddr = Memory.alloc(256);

    patchString(replyCgiAddr, "/cgi-bin/micromsg-bin/sendappmsg");

    sendReplyMessageAddr.add(0x00).writeU64(0);
    sendReplyMessageAddr.add(0x08).writeU64(0);
    sendReplyMessageAddr.add(0x10).writeU64(0);
    sendReplyMessageAddr.add(0x18).writeU64(1);
    sendReplyMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendReplyMessageAddr.add(0x28).writePointer(replyMessageAddr);

    replyMessageAddr.add(0x00).writePointer(fakeVtable);
    replyMessageAddr.add(0x08).writeU32(taskIdGlobal);
    replyMessageAddr.add(0x0c).writeU32(0x6e);
    replyMessageAddr.add(0x10).writeU64(0x3);
    replyMessageAddr.add(0x18).writePointer(replyCgiAddr);
    replyMessageAddr.add(0x20).writeU64(0x20);
    replyMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    replyMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));

    console.log("[+] Reply message setup complete. CgiAddr: " + replyCgiAddr + " SendAddr: " + sendReplyMessageAddr);
}


function triggerSendReplyMessage(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "reply");
}

// -------------------------发送回复消息分区-------------------------

// -------------------------发送语音消息分区-------------------------
function triggerSendVoiceMessage(taskId, sender, receiver, protoHex, payloadHex) {
    return triggerSendMediaMessage(taskId, sender, receiver, protoHex, payloadHex, "voice");
}
// -------------------------发送语音消息分区-------------------------

rpc.exports = {
    hydrateCdnVideoCache: hydrateCdnVideoCache,
    triggerSendImgMessage: triggerSendImgMessage,
    triggerUploadImg: triggerUploadImg,
    triggerSendTextMessage: triggerSendTextMessage,
    triggerDownload: triggerDownload,
    triggerUploadVideo: triggerUploadVideo,
    triggerSendVideoMessage: triggerSendVideoMessage,
    triggerSendReplyMessage: triggerSendReplyMessage,
    triggerUploadVoice: triggerUploadVoice,
    triggerSendVoiceMessage: triggerSendVoiceMessage,
    triggerSendFileMessage: triggerSendFileMessage,
    triggerSendFileUploadMessage: triggerSendFileUploadMessage,
    triggerUploadFile: triggerUploadFile,
    triggerUploadAppAttach: triggerUploadAppAttach,
};

// -------------------------发送图片消息分区-------------------------

// -------------------------接收消息分区-------------------------
function setupDownloadFileDynamic() {
    downloadFileX1 = Memory.alloc(1624)
    fileIdAddr = Memory.alloc(128)
    downloadAesKeyAddr = Memory.alloc(128)
    filePathAddr = Memory.alloc(256)
    fileCdnUrlAddr = Memory.alloc(256)

}


function setReceiver() {
	Interceptor.attach(buf2RespAddr, {
		onEnter: function (args) {
			// 通过 SP+0x140 读取当前 buf2resp 对应的 taskId
			var respTaskId = this.context.sp.add(0x140).readS32();
			const currentPtr = this.context.x20;
			const x2 = this.context.x0.toInt32();
            // 先处理我们发送任务的ack: 无论响应数据是否可读, 清理动作都必须执行
            // (错误响应往往指针不可读, 在校验前早退会跳过清理留下悬空伪造指针)
            var pendingEntry = finishPendingBuf2RespTask(respTaskId);
            if (pendingEntry && !pendingEntry.addr.isNull()) {
                // 成功路径同样复原原始消息指针, 而不是写0: 已完成的任务若带 NULL
                // 消息指针留在 mars 任务 map 里, 后续(数秒~数天后)清理 erase 时会
                // 踩坏红黑树 (2026-09-02 02:22 crash: 文本成功后留下 NULL 节点,
                // 图片失败的清理路径踩雷)。originalPtr 是 sendFunc 构造的合法消息,
                // 复原后任务全程处于原生合法状态, OnTaskEnd 按原生流程回收即可
                try {
                    if (pendingEntry.originalPtr && !pendingEntry.originalPtr.isNull()) {
                        pendingEntry.addr.writePointer(pendingEntry.originalPtr);
                        console.log("[+] buf2resp: 已复原原始消息指针, msgType=" + pendingEntry.msgType + " taskId=" + respTaskId);
                    } else {
                        pendingEntry.addr.writeU64(0x0);
                        console.log("[+] buf2resp: 原始指针不可用, 已清零 insertMsgAddr, msgType=" + pendingEntry.msgType + " taskId=" + respTaskId);
                    }
                } catch (e) {
                    console.error("[!] buf2resp 清理异常: taskId=" + respTaskId + " err=" + e);
                }
            }

            if (!isReadablePointer(currentPtr) || x2 < 4 || x2 > MAX_FRIDA_MESSAGE_BYTES) {
                if (pendingEntry) {
                    console.log("[+] buf2resp: ack响应数据不可读, 已跳过数据转发, taskId=" + respTaskId);
                } else {
                    console.error("[-] buf2resp: pointer 不可读 或 x2 大小不正确, ptr=" + currentPtr + " x2=" + x2);
                }
				return;
            }

            // 判断是否是我们发送的消息的 ack
            if (pendingEntry) {
                // 读取响应数据
				var respData = x2 >= 4 && x2 <= MAX_FRIDA_MESSAGE_BYTES ? readByteArrayIfReadable(currentPtr, x2) : null;
				if (respData) {
					var bytes = new Uint8Array(respData);
                    if (!isCurrentDirectAck(respTaskId, Date.now())) {
                        console.warn("[direct-send] ignoring late ack taskId=" + respTaskId);
                        return;
                    }
                    try {
                        var receipt = summarizeSendAck(bytes, pendingEntry.msgType);
                        console.log("[direct-send] server_ack taskId=" + respTaskId +
                            " ret=" + receipt.ret + " server_message_id=" + receipt.serverMessageId);
                    } catch (e) {
                        console.error("[direct-send] invalid ack taskId=" + respTaskId + " error=" + e);
                        return;
                    }
					console.log("[+] buf2resp: 收到响应, msgType=" + pendingEntry.msgType + " taskId=" + respTaskId + " len=" + x2);
					send({
						type: "buf2resp",
						msg_type: pendingEntry.msgType,
						data: Array.from(bytes),
					});
				}
				return
            }

            const mem = readByteArrayIfReadable(currentPtr, x2);
            if (!mem) {
                console.warn("[skip] protobuf_msg memory read failed, length=" + x2);
                return;
            }
            const uint8Array = new Uint8Array(mem);
            // 与已验证稳定的旧版本保持一致，只做最宽松的消息候选判断。
            // 具体结构交给 Go 解析，宁可产生误判日志，也不要在 JS 层漏掉消息。
            if (uint8Array[0] !== 0x08) {
                return;
            }

            // 任何同步消息到达 = 微信已登录, 解锁 CdnManager 解析门禁
            incomingTrafficSeen = true;
            send({
                type: "protobuf_msg",
                data: Array.from(uint8Array),
            })
        },
    });

    // Exact-version passive capture: do not patch middle instructions or mutate CDN tasks.
    if (structVer === "2") {
        try { installPassiveMediaCapture(baseAddr, downloadFileAddr, downloadImagAddr, structVer); }
        catch (error) { console.error("[media] passive capture unavailable: " + error.message); }
        return;
    }

    Interceptor.attach(startDownloadMedia, {
        onEnter: function (args) {
            downloadGlobalX0 = this.context.x0;
            if (uploadGlobalX0.equals(ptr(0))) {
                // 上传/下载共用同一 mars::cdn::CdnManager 单例, 顺手回填
                uploadGlobalX0 = this.context.x0;
                console.log("[+] 下载hook回填 uploadGlobalX0: " + uploadGlobalX0);
            }
            var fileIDAddr = readPointerIfReadable(this.context.x1.add(0x40));
            var fileId = readUtf8StringIfReadable(fileIDAddr);
            if (!fileId || !isReadablePointer(this.context.x1.add(0xA0))) {
                return;
            }
            const t = this.context.x1.add(0xA0).readU32()
            if (t === 3) {
                if (fileId.endsWith("_1")) {
                    this.context.x1.add(0xA0).writeU32(0x02);
                }
                if (fileId.endsWith("_31")) {
                    this.context.x1.add(0xA0).writeU32(0x04);
                }
            }
        }
    })

    // 4.1.12(structVer=2) 下载链路漂移(DIAG 实证, 详见 docs/version-upgrade.md):
    // - file/imag 数据寄存器 x22→x21 (寄存器分配漂移, JSON hook 点即 mov x1,xN 指令)
    // - 任务结构 +0x18: fileId 0x2E0→0x2F8, cdnUrl 0x2F8→0x310
    // - 视频数据变为 libc++ std::string(x20+0x178), 长度必须读结构体(4.1.12 x23=0)
    var dlIsV2 = (structVer === "2");
    var dlFileIdOff = dlIsV2 ? 0x2F8 : 0x2E0;
    var dlCdnUrlOff = dlIsV2 ? 0x310 : 0x2F8;

    Interceptor.attach(downloadFileAddr, {
        onEnter: function (args) {
			var dataPtr = dlIsV2 ? this.context.x21 : this.context.x22;
			var dataLen = this.context.x2.toInt32();
			var fileId = readUtf8StringIfReadable(readPointerIfReadable(this.context.x19.add(dlFileIdOff)));
			var cdnUrl = readUtf8StringIfReadable(readPointerIfReadable(this.context.x19.add(dlCdnUrlOff)));

            sendDownloadChunks(dataPtr, dataLen, fileId, cdnUrl);
        }
    });

    Interceptor.attach(downloadImagAddr, {
        onEnter: function (args) {
            var dataPtr = dlIsV2 ? this.context.x21 : this.context.x22;
            var dataLen = this.context.x2.toInt32();
            var fileId = readUtf8StringIfReadable(readPointerIfReadable(this.context.x19.add(dlFileIdOff)));
            var cdnUrl = readUtf8StringIfReadable(readPointerIfReadable(this.context.x19.add(dlCdnUrlOff)));

            sendDownloadChunks(dataPtr, dataLen, fileId, cdnUrl);
        }
    });

    Interceptor.attach(downloadVideoAddr, {
        onEnter: function (args) {
            var dataPtr, dataLen;
            if (dlIsV2) {
                // libc++ std::string at x20+0x178: 数据指针 [+0], 长度 [+8],
                // SSO 旗标 [+0x17]&0x80 (短串数据内联在对象里)
                var sObj = this.context.x20.add(0x178);
                try {
                    var ssoFlag = sObj.add(0x17).readU8();
                    if (ssoFlag & 0x80) {
                        dataPtr = sObj;
                        dataLen = ssoFlag & 0x7f;
                    } else {
                        dataPtr = readPointerIfReadable(sObj);
                        dataLen = sObj.add(8).readU64().toUInt32();
                    }
                } catch (e) { dataPtr = ptr(0); dataLen = 0; }
            } else {
			    dataPtr = readPointerIfReadable(this.context.x20.add(0x178));
			    dataLen = this.context.x23.toInt32();
            }
			var fileId = readUtf8StringIfReadable(readPointerIfReadable(this.context.x19.add(dlFileIdOff)));
			var cdnUrl = readUtf8StringIfReadable(readPointerIfReadable(this.context.x19.add(dlCdnUrlOff)));

            sendDownloadChunks(dataPtr, dataLen, fileId, cdnUrl);
        }
    });
}


// fileType:  HdImage => 1,Image => 2, thumbImage => 3, Video => 4, File => 5,
function triggerDownload(receiver, cdnUrl, aesKey, filePath, fileType) {
    if (structVer === "2") {
        console.error("[media] active native download disabled; wait for WeChat native download");
        return "fail";
    }
    if (!downloadGlobalX0) {
        ensureCdnManagerX0();
    }
    if (!downloadGlobalX0) {
        console.error("[!] downloadGlobalX0 尚未初始化，请等待 hook 捕获");
        return "fail";
    }

    const downloadMediaPayload = [
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x00
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x10
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x20
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x30
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xF0, 0xB6, 0x4C, 0xFC, 0x0A, 0x00, 0x00, 0x00, // 0x40
        0x24, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
        0x80, 0x10, 0x4B, 0xFA, 0x0A, 0x00, 0x00, 0x00, // 0x58
        0xB2, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xB8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
        0xF0, 0xB3, 0x4C, 0xFC, 0x0A, 0x00, 0x00, 0x00, // 0x70
        0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80,
        0x60, 0xC4, 0x2D, 0xFE, 0x0A, 0x00, 0x00, 0x00, // 0x88
        0xC8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x90
        0xD0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x98
        0x03, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, // 0xa0
        0x00, 0x00, 0x00, 0x00, 0x01, 0xAA, 0xAA, 0xAA, // 0xa8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xb0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xc0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xd0
        0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xd8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xe0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xf0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x100
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x110
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x02, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00, // 0x128
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x11, 0x28, 0x28, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x148
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x02, 0x00, 0x00, 0xAA, 0xAA, 0xAA, // 0x170
        0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00, // 0x180
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x1E, 0x00, 0x00, 0x00, 0xAA, 0xAA, 0xAA, 0xAA, // 0x1a0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0xAA, 0xAA, 0xAA, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x22, 0x1A, 0xFE, 0x0A, 0x00, 0x00, 0x00, // 0x1d0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1f0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x200
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x288
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x298
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2a0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
        0x00, 0x4F, 0x56, 0xFC, 0x0A, 0x00, 0x00, 0x00, // 0x2c0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x300
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x01, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00, // 0x318
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x340
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x01, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00, // 0x378
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x03, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x80, 0x3F, 0x00, 0x00, 0x00, 0x00, // 0x3e0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ];

    patchString(fileIdAddr, receiver + "_" + String(Math.floor(Date.now() / 1000)) + "_" + Math.floor(Math.random() * 1001) + "_1");
    patchString(fileCdnUrlAddr, cdnUrl)
    patchString(downloadAesKeyAddr, aesKey)
    patchString(filePathAddr, filePath);

    downloadFileX1.writeByteArray(downloadMediaPayload);
    downloadFileX1.add(0x40).writePointer(fileIdAddr);
    downloadFileX1.add(0x58).writePointer(fileCdnUrlAddr);
    downloadFileX1.add(0x70).writePointer(downloadAesKeyAddr);
    downloadFileX1.add(0x88).writePointer(filePathAddr);
    downloadFileX1.add(0xa0).writeU32(fileType);

    const startDwMedia = new NativeFunction(startDownloadMedia, 'int64', ['pointer', 'pointer']);
    return startDwMedia(downloadGlobalX0, downloadFileX1);
}

// -------------------------接收消息分区-------------------------

// Verified-entry passive media capture helper
// Candidate for the exact 4.1.12.53 (build 269365) code shape.
// Capture at a real function entry, never relocate the BL call sites.
var passiveMediaCaptureRuntime = null;
function readPassiveMediaKey(address, limit) {
    var value = '';
    for (var i = 0; i < limit; i++) {
        var byte = address.add(i).readU8();
        if (byte === 0) return value;
        if (byte < 32 || byte > 126) return '';
        value += String.fromCharCode(byte);
    }
    return ''; // No terminator inside the allowed metadata size.
}
function installPassiveMediaCapture(base, fileSite, imageSite, version) {
    if (Process.arch !== 'arm64' || version !== '2') throw new Error('Unsupported media capture version');
    if (!fileSite.equals(base.add(0x562d95c)) || !imageSite.equals(base.add(0x568e5d0))) {
        throw new Error('Unsupported media call-site signature');
    }
    function branchTarget(site) {
        var instruction = Instruction.parse(site);
        var match = /^#?(0x[0-9a-f]+)$/i.exec(instruction.opStr.trim());
        if (instruction.mnemonic !== 'bl' || !match) throw new Error('Invalid media BL signature');
        return ptr(match[1]);
    }
    var target = branchTarget(imageSite);
    if (!target.equals(branchTarget(fileSite)) || !target.equals(base.add(0x5851af0))) {
        throw new Error('Unexpected media branch target');
    }
    var prologue = [
        ['sub', 'sp,sp,#0x30'],
        ['stp', 'x20,x19,[sp,#0x10]'],
        ['stp', 'x29,x30,[sp,#0x20]']
    ];
    prologue.forEach(function (expected, i) {
        var instruction = Instruction.parse(target.add(i * 4));
        if (instruction.mnemonic !== expected[0] || instruction.opStr.replace(/\s/g, '') !== expected[1]) {
            throw new Error('Unexpected media entry signature');
        }
    });
    var forward = makePassiveMediaForward();
    return attachPassiveMediaEntry(target, fileSite.add(4), imageSite.add(4), forward);
}

function makePassiveMediaForward(emit) {
    var emitPacket = typeof emit === 'function' ? emit : send;
    return new NativeCallback(function (buffer, length, owner) {
        try {
            if (length && typeof length.toNumber === 'function') length = length.toNumber();
            if (!Number.isSafeInteger(length) || length <= 0 || length > 8 * 1024 * 1024 ||
                !isReadablePointer(buffer) || !isReadablePointer(owner)) return;
            var fileIdPtr = readPointerIfReadable(owner.add(0x2f8));
            var cdnPtr = readPointerIfReadable(owner.add(0x310));
            if (!isReadablePointer(fileIdPtr) || !isReadablePointer(cdnPtr)) return;
            var fileId = readPassiveMediaKey(fileIdPtr, 512);
            var cdnUrl = readPassiveMediaKey(cdnPtr, 4096);
            if (!fileId || fileId.length > 512 || !cdnUrl || cdnUrl.length > 4096) return;
            var data = readByteArrayIfReadable(buffer, length);
            if (!data) return;
            emitPacket({type: 'download', media: Array.from(new Uint8Array(data)), file_id: fileId, cdn_url: cdnUrl});
        } catch (error) {
            // Do not leak native pointers, opaque CDN authorization, or keys.
            console.warn('[media] passive buffer skipped: unreadable or invalid data');
        }
    }, 'void', ['pointer', 'ulong', 'pointer']);
}

// Kept separate so the exact native listener can be exercised in an isolated host.
function attachPassiveMediaEntry(target, fileReturn, imageReturn, forward) {
    var code = '#include <gum/guminterceptor.h>\n' +
        'extern void forward_payload(gpointer data, gsize length, gpointer owner);\n' +
        'void on_enter(GumInvocationContext *ic) {\n' +
        ' gpointer ra = gum_invocation_context_get_return_address(ic);\n' +
        ' if (ra != (gpointer)' + imageReturn + 'ULL && ra != (gpointer)' + fileReturn + 'ULL) return;\n' +
        ' gsize length = (gsize) gum_invocation_context_get_nth_argument(ic, 2);\n' +
        ' gpointer data = gum_invocation_context_get_nth_argument(ic, 1);\n' +
        ' gpointer owner = (gpointer) ic->cpu_context->x[19];\n' +
        ' if (data == NULL || owner == NULL || length == 0 || length > 8388608) return;\n' +
        ' forward_payload(data, length, owner);\n' +
        '}\n';
    var nativeModule = new CModule(code, {forward_payload: forward});
    var listener = Interceptor.attach(target, {onEnter: nativeModule.on_enter});
    // Keep C code, callback and listener alive for the complete script lifetime.
    passiveMediaCaptureRuntime = {module: nativeModule, callback: forward, listener: listener};
    console.log('[media] passive entry capture installed; original media tasks unchanged');
    return passiveMediaCaptureRuntime;
}
