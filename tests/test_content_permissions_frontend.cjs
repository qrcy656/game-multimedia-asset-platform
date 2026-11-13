const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const media = fs.readFileSync(path.join(__dirname,'../media/static/app.js'),'utf8');
const narrative = fs.readFileSync(path.join(__dirname,'../narrative/src/api.js'),'utf8');

function client(kind, responses) {
  const calls=[], widgets=new Map();
  const context = {Headers,AbortController,setTimeout,clearTimeout,
    state:{csrf:'original-test-token',config:null},
    $:name=>{if(!widgets.has(name))widgets.set(name,{classList:{add(){},remove(){this.revealed=true}}});return widgets.get(name)},
    fetch:async(url,options)=>{calls.push({url,options});const reply=responses.shift();if(!reply)throw new Error('Unexpected request');return {status:reply.status,ok:reply.status<400,json:async()=>reply.body}}};
  vm.createContext(context);
  const code=kind==='media'?media.slice(media.indexOf('async function api('),media.indexOf('function navigate(')):
    narrative.slice(0,narrative.indexOf('export const uid')).replaceAll('export ','');
  vm.runInContext(code,context);
  return {api:context.api,calls,widgets,state:context.state};
}
for(const kind of ['media','narrative']) {
  test(kind+' permission denial is not retried or labelled as logout',async()=>{
    const run=client(kind,[{status:403,body:{detail:'仅创建者或管理员可以修改此内容'}}]);
    await assert.rejects(run.api('/api/projects/other',{method:'DELETE'}),error=>error.status===403&&/创建者/.test(error.message));
    assert.equal(run.calls.length,1);
    assert.equal(run.state.csrf,'original-test-token');
    assert.equal(run.widgets.get('auth-error')?.classList.revealed,undefined);
  });
  test(kind+' retries only explicit expired CSRF once',async()=>{
    const expired={status:403,body:{detail:'页面验证已失效，请刷新后重试。'}};
    const run=client(kind,[expired,{status:200,body:{csrf:'renewed-test-token'}},expired]);
    await assert.rejects(run.api('/api/projects/mine',{method:'PUT',body:'{}'}),error=>error.status===403);
    assert.equal(run.calls.length,3);
    assert.match(run.calls[1].url,/\/api\/(session|config)$/);
  });
}
test('media permissions use immutable account ID, never display name or missing owner',()=>{
  const context={state:{config:{user:{id:'bob',name:'Same name',admin:false}}}};
  vm.createContext(context);vm.runInContext(media.match(/^function own\(job\).*$/m)[0],context);
  assert.equal(context.own({owner:{id:'alice',name:'Same name'}}),false);
  assert.equal(context.own({owner:{name:'Same name'}}),false);
  assert.equal(context.own({owner:{id:'bob'}}),true);
  context.state.config.user.admin=true;
  assert.equal(context.own({owner:{id:'alice'}}),true);
});
