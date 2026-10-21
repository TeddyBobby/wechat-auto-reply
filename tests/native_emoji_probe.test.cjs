const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const sourcePath = path.join(__dirname, '../spikes/native-emoji/probe.js');
function load() {
  assert.ok(fs.existsSync(sourcePath), 'read-only native emoji probe is not implemented');
  const source = fs.readFileSync(sourcePath, 'utf8');
  const context = { rpc: {exports: {}}, Uint8Array, Date, Object, JSON, Math };
  vm.createContext(context);
  vm.runInContext(source, context);
  return {source, context};
}
test('candidate recognition requires filehelper and an actual MD5 token', () => {
  const {context} = load();
  const bytes = Buffer.from('\x0afilehelper\x12 0123456789abcdef0123456789abcdef\x00');
  const matches = context.candidateMetadata(new Uint8Array(bytes));
  assert.equal(matches.has_filehelper, true);
  assert.equal(matches.md5s[0], '0123456789abcdef0123456789abcdef');
  assert.equal(context.candidateMetadata(new Uint8Array(Buffer.from('private target 0123456789abcdef0123456789abcdef'))), null);
  assert.equal(context.candidateMetadata(new Uint8Array(Buffer.from('filehelper ordinary plain text'))), null);
});
test('only same-thread, recent command 68 tasks are correlated', () => {
  const {context} = load();
  const tasks = {10: {id:10, thread:4, at:1000}, 20: {id:20, thread:8, at:1000}};
  assert.equal(context.selectRecentTask(tasks, 4, 1001).id, 10);
  assert.equal(context.selectRecentTask(tasks, 9, 1001), null);
  assert.equal(context.selectRecentTask(tasks, 4, 32001), null);
});
test('observation does not assume modern emoji still uses legacy command 68', () => {
  const {source, context} = load();
  assert.doesNotMatch(source, /if \(command!==68\) return;/);
  assert.equal(context.rpc.exports.status().writer_calls, 0);
});
test('probe cannot write process memory, call native sending, or replace functions', () => {
  const {source, context} = load();
  assert.doesNotMatch(source, /\.write(?:Pointer|U\d+|S\d+|ByteArray|Utf8String)|NativeFunction|Interceptor\.replace|detachAll/);
  assert.doesNotMatch(source, /console\.(?:log|warn|error)/);
  assert.deepEqual(Object.keys(context.rpc.exports).sort(), ['finish', 'start', 'status']);
});
