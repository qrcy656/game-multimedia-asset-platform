const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const http = require('node:http');
const source = fs.readFileSync(path.join(__dirname, '../community.js'), 'utf8');

function browser(storage = new Map()) {
  const context = {window: {dispatchEvent() {}}, location: {pathname:'/'},
    CustomEvent: class {constructor(type, options) {this.type = type; this.detail = options.detail;}},
    localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key,value)}};
  vm.createContext(context); vm.runInContext(source, context);
  return {api: context.window.FantasyModels, context, storage};
}

test('one browser separates each account and another browser has independent choices', () => {
  const {api,storage} = browser();
  api.setUser('alice'); api.set('knowledge', 'llm', 'personal-a');
  assert.equal(api.selection('knowledge', 'llm'), 'personal-a');
  api.setUser('bob'); assert.equal(api.selection('knowledge','llm'), undefined);
  api.set('knowledge', 'llm', 'global-b');
  api.setUser('alice'); assert.equal(api.selection('knowledge','llm'), 'personal-a');
  assert.equal(api.headers()['X-Fantasy-Model-Account'], 'alice');
  assert.equal(JSON.parse(api.headers()['X-Fantasy-Model-Selection']).knowledge.llm, 'personal-a');
  const other = browser().api; other.setUser('alice');
  assert.equal(other.selection('knowledge','llm'), undefined);
  assert.equal(storage.size, 2);
  api.setUser(null); assert.equal(Object.keys(api.headers()).length,0);
});

test('explicit disable differs from following global defaults and other modules are retained', () => {
  const {api} = browser(); api.setUser('alice');
  api.set('knowledge','llm',null); api.set('animation','vision','vision-a');
  assert.equal(api.selection('knowledge','llm'), null);
  api.set('knowledge','llm',undefined);
  assert.equal(api.selection('knowledge','llm'), undefined);
  assert.equal(api.selection('animation','vision'),'vision-a');
  const cloned = api.selections(); cloned.animation.vision='tampered';
  assert.equal(api.selection('animation','vision'),'vision-a');
});

test('storage disabled still permits current-tab selection without leaking to the next account', () => {
  const {api,context} = browser();
  context.localStorage.getItem = () => {throw new Error('blocked');};
  context.localStorage.setItem = () => {throw new Error('blocked');};
  api.setUser('alice'); api.set('music','llm','text-a');
  assert.equal(api.selection('music','llm'),'text-a');
  api.setUser('bob'); assert.equal(api.selection('music','llm'),undefined);
});

test('module GET status and POST generation automatically carry the same actor choices', async () => {
  const calls = [];
  const context = {window: {dispatchEvent() {}, fetch: async (url, options) => {
    calls.push([String(url), options]);
    return {ok: true, json: async () => ({user:{id:'alice'}, csrf:'csrf-a'})};
  }}, location: {pathname:'/knowledge/', href:'https://example.test/knowledge/', origin:'https://example.test'},
  URL, Headers, Request, CustomEvent: class {}, localStorage: {getItem: () => '{"knowledge":{"llm":"personal-a"}}', setItem() {}}};
  vm.createContext(context); vm.runInContext(source, context);
  await context.window.fetch('/api/status');
  await context.window.fetch('/api/chat', {method:'POST', body:'{}'});
  const [getURL,get] = calls[1], [postURL,post] = calls[2];
  assert.equal(getURL,'https://example.test/knowledge/api/status');
  assert.equal(get.headers.get('X-Fantasy-Model-Account'),'alice');
  assert.equal(JSON.parse(get.headers.get('X-Fantasy-Model-Selection')).knowledge.llm,'personal-a');
  assert.equal(postURL,'https://example.test/knowledge/api/chat');
  assert.equal(post.headers.get('X-Community-CSRF'),'csrf-a');
  assert.equal(post.headers.get('X-Fantasy-Model-Selection'),get.headers.get('X-Fantasy-Model-Selection'));
});

test('both effects wrappers preserve sized POST uploads over local HTTP/1.1', async () => {
  const seen = [];
  const server = http.createServer(async (request, response) => {
    let body = ''; for await (const chunk of request) body += chunk;
    seen.push({path:request.url, headers:request.headers, body});
    response.writeHead(200, {'Content-Type':'application/json'}); response.end('{}');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const origin='http://127.0.0.1:'+server.address().port;
    const location={pathname:'/effects/',href:origin+'/effects/',origin};
    class BrowserRequest extends Request {
      constructor(input,init){super(typeof input==='string'?new URL(input,location.href):input,init);}
    }
    const storage={getItem:()=>null,setItem(){}};
    const context={window:{location,localStorage:storage,dispatchEvent(){},addEventListener(){},setInterval(){},
      FantasyEffectsSession:{user:{id:'alice',name:'Alice'},csrf:'effects-csrf',storage_key:'alice',api_base:'/effects'},
      fetch:async (input,init)=>input==='/api/session'?new Response(JSON.stringify({user:{id:'alice'},csrf:'portal-csrf'})):fetch(input,init)},
      document:{readyState:'loading',getElementById:()=>null,addEventListener(){}},location,localStorage:storage,
      URL,Headers,Request:BrowserRequest,CustomEvent:class{},setTimeout,clearTimeout};
    vm.createContext(context);vm.runInContext(source,context);
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../effects/frontend/integration.js'),'utf8'),context);
    for(const route of ['/effects/api/save','/api/save']) {
      await context.window.fetch(route,{method:'POST',headers:{'Content-Type':'application/json'},body:'{"name":"ice"}'});
    }
    for(const request of seen){
      assert.equal(request.path,'/effects/api/save');
      assert.equal(request.body,'{"name":"ice"}');
      assert.equal(request.headers['content-length'],String(Buffer.byteLength(request.body)));
      assert.equal(request.headers['transfer-encoding'],undefined);
      assert.equal(request.headers['x-community-csrf'],'portal-csrf');
      assert.equal(request.headers['x-effects-owner'],'alice');
      assert.equal(request.headers['x-effects-csrf'],'effects-csrf');
    }
    assert.equal(seen.length,2);
  } finally {server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
});
