const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../debug_ui/app.js'), 'utf8');
const apiSource = source.slice(source.indexOf('async function refreshSession('), source.indexOf('function showError('));
const response = (status, body) => ({status, ok:status>=200 && status<300,
  json:async()=>body, text:async()=>body});
const denied = () => response(403, {error:'Invalid host or token'});
const freshPage = () => response(200, '<meta name="inspector-token" content="fresh">');

function setup(handler) {
  const calls = [];
  const meta = {content:'old'};
  const context = vm.createContext({
    fetch:async(url, options)=>{calls.push({url, options});return handler(url, options);},
    document:{querySelector:()=>meta},
    DOMParser:class {parseFromString(html){return {querySelector:()=>({content:html.match(/content="([^"]+)"/)?.[1]})};}},
  });
  vm.runInContext("let token='old', tokenRefresh=null, cursor=900, lastRun='old-run', clears=0; const sources=new Map([['old',1]]); function clearRun(){clears++;}", context);
  vm.runInContext(apiSource, context);
  return {calls, meta, run:code=>vm.runInContext(code, context)};
}

test('rejected start refreshes once; only the authenticated request is accepted', async()=>{
  let accepted=0;
  const env=setup((url, options)=>{
    if(url==='/')return freshPage();
    if(options.headers['X-Inspector-Token']==='old')return denied();
    accepted++;
    return response(200,{run_dir:'new-run'});
  });
  const result=await env.run("api('/api/start',{mode:'demo',repeat:2})");
  assert.equal(result.run_dir,'new-run');
  assert.equal(accepted,1);
  const posts=env.calls.filter(c=>c.url==='/api/start');
  assert.equal(posts.length,2);
  assert.equal(posts[0].options.body,posts[1].options.body);
  assert.equal(env.meta.content,'fresh');
  assert.equal(env.run('cursor'),0);
  assert.equal(env.run('sources.size'),0);
});

test('snapshot cursor resets after server restart', async()=>{
  const env=setup((url, options)=>url==='/'?freshPage():
    options.headers['X-Inspector-Token']==='old'?denied():response(200,{cursor:1}));
  await env.run("api('/api/snapshot?after=900&detail=0&lane=2')");
  assert.equal(env.calls.at(-1).url,'/api/snapshot?after=0&detail=0&lane=2');
});

test('concurrent stale requests share a single token refresh', async()=>{
  const env=setup(async(url, options)=>{
    if(url==='/'){await new Promise(resolve=>setImmediate(resolve));return freshPage();}
    return options.headers['X-Inspector-Token']==='old'?denied():response(200,{ok:true});
  });
  await env.run("Promise.all([api('/api/snapshot?after=900'),api('/api/device-profiles')])");
  assert.equal(env.calls.filter(c=>c.url==='/').length,1);
  assert.equal(env.run('clears'),1);
});

test('network loss and server errors never replay a possibly accepted start', async()=>{
  for(const fail of [()=>{throw new Error('connection lost');},()=>response(500,{error:'server failed'})]){
    const env=setup(fail);
    await assert.rejects(env.run("api('/api/start',{mode:'demo'})"));
    assert.equal(env.calls.length,1);
  }
});

test('origin rejection is not treated as token expiry', async()=>{
  const env=setup(()=>response(403,{error:'Invalid origin'}));
  await assert.rejects(env.run("api('/api/start',{mode:'demo'})"),/Invalid origin/);
  assert.equal(env.calls.length,1);
});

test('unchanged token and repeated rejection stop rather than looping', async()=>{
  for(const fresh of ['old','fresh']){
    const env=setup(url=>url==='/'?response(200,`<meta name="inspector-token" content="${fresh}">`):denied());
    await assert.rejects(env.run("api('/api/start',{mode:'demo'})"));
    assert.equal(env.calls.filter(c=>c.url==='/api/start').length,fresh==='old'?1:2);
    assert.equal(env.calls.filter(c=>c.url==='/').length,1);
  }
});
