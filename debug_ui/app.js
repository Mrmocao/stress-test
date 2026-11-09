'use strict';
const $ = id => document.getElementById(id);
let token = document.querySelector('meta[name="inspector-token"]').content;
let tokenRefresh = null;
const statusLabels = {idle:'等待启动',preparing:'准备中',running:'运行中',waiting:'等待 5 秒',pausing:'等待暂停',paused:'已暂停',completed:'已完成',completed_with_errors:'执行结束（有失败）',failed:'失败',stopped:'已停止'};
let cursor = 0, latest = null, selectedEvent = null, selectedScope = '', events = [], changes = [], consoles = [];
let scopes = new Map(), sources = new Map(), sourceFile = '', sourceLine = 0, lastRun = '', lastState = null;
let currentTab = 'console', busy = false;
let sourceRequest = 0, browserHistoryTruncated = false;
let logSignature = '', roundSignature = '', laneSignature = '';
let keyLogs = [];
let durationRangeSupported = false;
let efficiencyControlsSupported = false;
let periodicHeartbeatSupported = false;
let deviceProfiles = [], deviceProfilesReady = false, runActive = false;

function renderDevicePreview() {
  const live = $('mode').value === 'live';
  const selected = deviceProfiles.find(profile => profile.id === $('device-profile').value);
  const shown = selected ? [selected] : deviceProfiles.filter(profile => profile.synthetic);
  $('device-preview').textContent = shown.map(profile => `${profile.label}\n${JSON.stringify({app_version:profile.app_version,android_version:profile.android_version,...profile.common,UserAgent:profile.user_agent},null,2)}`).join('\n\n');
  const fixtures = deviceProfiles.filter(profile => profile.synthetic);
  const versions = [...new Set(fixtures.map(profile => profile.app_version))].join(' / ');
  $('device-note').textContent = `共 ${fixtures.length} 套测试配置，版本 ${versions}。运行时随机获取设备会在每轮启动时抽取整套配置，本轮保持一致，可能重复。${live?'真实播放将使用选中的参数请求实际接口；生成参数不代表已验证的真实客户端。':'离线演示使用本地响应。'}`;
  const needsConfirmation = live && selected?.id === 'original-tv';
  $('device-confirmation').hidden = !needsConfirmation;
  $('device-confirmed').required = needsConfirmation;
}
function refreshDeviceOptions() {
  const previous = $('device-profile').value;
  const live = $('mode').value === 'live';
  const options = deviceProfiles.map(profile => [profile.id, profile.label]);
  options.unshift(['random-device', '运行时随机获取设备（每轮从 10 套中抽取）']);
  $('device-profile').replaceChildren(...options.map(([value, label]) => {const option = element('option', '', label); option.value = value; return option;}));
  $('device-profile').value = options.some(([value]) => value === previous) ? previous : (live ? 'original-tv' : 'random-device');
  $('device-confirmed').checked = false;
  renderDevicePreview();
}

async function refreshSession(staleToken) {
  if(token !== staleToken)return;
  if(!tokenRefresh)tokenRefresh=(async()=>{
    const response=await fetch('/',{cache:'no-store',credentials:'same-origin',redirect:'error'});
    if(!response.ok)throw new Error('服务重新连接失败，请稍后重试。');
    const doc=new DOMParser().parseFromString(await response.text(),'text/html');
    const fresh=doc.querySelector('meta[name="inspector-token"]')?.content;
    if(!fresh || fresh===staleToken)throw new Error('页面会话已失效，请刷新页面后重试。');
    token=fresh;
    document.querySelector('meta[name="inspector-token"]').content=fresh;
    // A restarted server has a new event sequence. Keep the user's form inputs.
    cursor=0;lastRun='';sources.clear();clearRun();
  })();
  try { await tokenRefresh; } finally { tokenRefresh=null; }
}
async function api(path, data, retryAuth=true) {
  const requestToken=token;
  const response = await fetch(path, {method:data ? 'POST':'GET', headers:{'X-Inspector-Token':requestToken,...(data?{'Content-Type':'application/json'}:{})},...(data?{body:JSON.stringify(data)}:{})});
  const result = await response.json();
  // These exact 403 responses are emitted before request-body handling. Only
  // retry an explicitly rejected request, never a timeout or an unknown outcome.
  if(retryAuth && response.status===403 && ['Invalid inspector token','Invalid host or token'].includes(result.error)){
    await refreshSession(requestToken);
    const retryPath=path.startsWith('/api/snapshot?')?path.replace(/([?&])after=\d+/, '$1after=0'):path;
    return api(retryPath,data,false);
  }
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function showError(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; }
function format(value, pretty = false) { return value === undefined ? '∅' : JSON.stringify(value, null, pretty ? 2 : 0); }
function timeLabel(at) { return new Date(at*1000).toLocaleTimeString('zh-CN',{hour12:false}); }
function element(tag, cls, text) { const el = document.createElement(tag); if(cls)el.className=cls; if(text !== undefined)el.textContent=text; return el; }
function short(value, max=180) { const text=format(value);return text.length>max?text.slice(0,max)+'…':text; }

function keyMessage(event, total) {
  if(event.kind==='trace' || event.kind==='process_exit')return '';
  if(event.kind==='launch')return `任务启动 · ${event.concurrency || 1} 路 × 每路 ${event.repeat || 1} 轮 · 时长 ${event.seconds_min ?? event.seconds}～${event.seconds_max ?? event.seconds} 秒，每轮独立抽取 · 周期心跳${event.heartbeat_interval?` ${event.heartbeat_interval} 秒（试验）`:'关闭'}`;
  if(event.kind==='round')return event.phase==='start'?`开始执行第 ${event.round} / ${event.total} 轮${event.target_seconds!==undefined?` · 本轮目标 ${event.target_seconds} 秒`:''}`:`本轮${statusLabels[event.status] || event.status}`;
  if(event.kind==='proxy')return [event.message,event.exit_ip?`公网 IP=${event.exit_ip}`:'',event.endpoint?`代理=${event.endpoint}`:'',event.remaining!=null?`剩余额度=${event.remaining}`:''].filter(Boolean).join(' · ');
  if(event.kind==='proxy_check')return `出口检测 ${event.service} · ${event.status==='ok'?'通过':'失败'} · ${event.phase} · ${event.elapsed_ms} ms${event.error_category?' · '+event.error_category:''}${event.cause_type?' / '+event.cause_type:''} · 已读 ${event.bytes_read || 0} 字节`;
  if(event.kind==='network')return `${event.method || 'GET'} ${event.path}${event.heartbeat_phase?` · ${{start:'开始',periodic:'周期',end:'结束'}[event.heartbeat_phase] || event.heartbeat_phase}心跳 · 实际进度 ${event.played_time} 秒`:''} · HTTP ${event.http_status ?? '—'} · code=${event.code ?? '—'} · ${event.elapsed_ms ?? '—'} ms${event.identity_update?' · 待修正重试':''}${event.error_message?' · '+event.error_message:event.error_type?' · '+event.error_type:''}${event.attempt?` · 第 ${event.attempt}/${event.max_attempts} 次`:''}${event.retry_in?` · 等待 ${event.retry_in} 秒后重新连接` : ''}`;
  if(event.kind==='cooldown')return event.message;
  if(event.kind==='lifecycle')return ['completed','completed_with_errors'].includes(event.status)?`任务执行结束 · 计划 ${total} 次${event.failed?` · 失败 ${event.failed} 次`:''}`:event.status==='failed'?'任务异常终止':'';
  if(event.kind==='control')return statusLabels[event.status] || event.status;
  if(event.kind==='console'){
    if(event.message?.includes('first decoded frame'))return '首帧解码完成，开始调用已完成';
    const playback=event.message?.match(/: (completed|failed), decoded=([\d.]+)s/);
    if(playback)return `播放${playback[1]==='completed'?'完成':'失败'} · 已解码 ${playback[2]} 秒`;
  }
  return event.message || '';
}
function renderKeyLogs(logs, total) {
  const signature=`${logs[0]?.id}:${logs.at(-1)?.id}:${total}`;
  if(signature===logSignature)return;
  logSignature=signature;
  const lines=logs.map(event=>{const message=keyMessage(event,total);return message?`[${timeLabel(event.at)}]${event.lane?` [第 ${event.lane} 路 / 第 ${event.round} 轮]`:''} ${message}`:'';}).filter(Boolean);
  $('console').textContent=lines.join('\n') || '等待启动：这里显示每轮 IP、接口返回码、耗时和执行结果。';
  if(currentTab==='console' && $('follow').checked)$('console').scrollTop=$('console').scrollHeight;
}
function renderRounds(batch, running, workers) {
  const rows=[...batch.rounds];
  workers.forEach(worker=>{if(running && worker.round && !rows.some(row=>row.lane===worker.lane&&row.number===worker.round))rows.push({lane:worker.lane,number:worker.round,status:worker.status,exit_ip:worker.proxy.exit_ip,requests:worker.metrics.request_count || 0,errors:worker.metrics.request_errors || 0,ip_attempts:worker.metrics.ip_attempts || 0});});
  rows.sort((a,b)=>a.lane-b.lane || a.number-b.number);
  const signature=JSON.stringify(rows);if(signature===roundSignature)return;roundSignature=signature;
  const fragment=document.createDocumentFragment();
  rows.forEach(row=>{const tr=element('tr'),result=element('td',`result-${row.status}`,row.status==='failed' && row.progress?.media_status==='completed'?'播放完成 · 上报未确认':statusLabels[row.status] || row.status);if(row.reason){const reason=element('small','failure-reason',row.reason);reason.title=row.reason;result.append(reason);}tr.append(element('td','',`${row.lane} 路 / ${row.number} 轮`),element('td','ip-cell',row.exit_ip || '—'),element('td','',`${row.requests} 次${row.errors?` · 异常 ${row.errors}`:''}`),element('td','',`${row.ip_attempts || 0} 次`),result);fragment.append(tr);});
  if(!rows.length){const tr=element('tr'),td=element('td','','启动后显示每轮结果');td.colSpan=5;tr.append(td);fragment.append(tr);}
  $('rounds').replaceChildren(fragment);
}
function renderLanes(workers, batch) {
  const signature=JSON.stringify([batch.repeat,Math.floor(Date.now()/1000),workers.map(w=>[w.lane,w.round,w.status,w.proxy,w.progress])]);
  if(signature===laneSignature)return;laneSignature=signature;
  const fragment=document.createDocumentFragment();
  workers.forEach(worker=>{
    const tr=element('tr'),p=worker.progress || {},cell=element('td','playback-progress');
    const target=Number(p.target_seconds) || 0,decoded=Math.max(0,Number(p.decoded_seconds) || 0);
    const percent=target>0?Math.min(100,decoded/target*100):0;
    const terminal=['completed','failed','stopped','waiting'].includes(worker.status);
    const stages={resolving:'获取播放信息',connecting:'连接视频，等待首帧',playing:'播放中',finishing:'解码结束，完成接口调用',completed:'播放完成',failed:'播放失败'};
    const proxyStages={acquiring:'获取新 IP',verifying:'验证代理出口',retrying:'等待 IP 重试',failed:'代理失败'};
    let stage=terminal?(statusLabels[worker.status] || worker.status):['paused','pausing'].includes(worker.status)?statusLabels[worker.status]:(stages[p.stage] || proxyStages[worker.proxy.status] || '准备播放');
    if(worker.status==='failed' && p.media_status==='completed')stage='播放完成 · 上报未确认';
    if(target>0){
      cell.append(element('div','progress-numbers',`${decoded.toFixed(1)} / ${target.toFixed(1)} 秒 · ${percent.toFixed(1)}%`));
      const bar=element('progress');bar.max=target;bar.value=Math.min(decoded,target);bar.setAttribute('aria-label',`第 ${worker.lane} 路实际播放进度`);cell.append(bar);
      const age=p.at?Math.max(0,Math.floor(Date.now()/1000-p.at)):0;
      cell.append(element('small','progress-note',`${p.frames || 0} 帧${!terminal && age>=5?` · ${age} 秒未收到新进度`:''}`));
    }else cell.append(element('span','progress-note',terminal?'本轮暂无解码进度':'等待实际解码数据'));
    if(worker.proxy.expires_at_epoch && !terminal){cell.append(element('small','progress-note',`代理剩余 ${Math.max(0,Math.floor(worker.proxy.expires_at_epoch-Date.now()/1000))} 秒`));}
    tr.append(element('td','',`第 ${worker.lane} 路`),element('td','',`${worker.round} / ${batch.repeat}`),element('td','ip-cell',worker.proxy.exit_ip || (worker.proxy.enabled?'尚未验证':'未启用代理')),cell,element('td',`result-${worker.status}`,stage));fragment.append(tr);
  });
  if(!workers.length){const tr=element('tr'),td=element('td','','启动后显示各路状态和实际解码进度');td.colSpan=5;tr.append(td);fragment.append(tr);}
  $('lanes').replaceChildren(fragment);
  const select=$('debug-lane'),value=select.value;
  if(select.options.length!==Math.max(1,workers.length)){
    select.replaceChildren(...(workers.length?workers:[{lane:1}]).map(w=>{const option=element('option','',`第 ${w.lane} 路`);option.value=w.lane;return option;}));
    if([...select.options].some(o=>o.value===value))select.value=value;
  }
}

async function showSource(file, line = 0) {
  const request = ++sourceRequest;
  if (!sources.has(file)) sources.set(file, (await api(`/api/source?file=${encodeURIComponent(file)}`)).text);
  if (request !== sourceRequest) return;
  if (file !== sourceFile) {
    sourceFile = file;sourceLine = 0;
    $('source-file').value = file;
    const fragment = document.createDocumentFragment();
    sources.get(file).split('\n').forEach((text, index) => {
      const row=element('div','code-line');row.dataset.line=index+1;
      row.append(element('span','line-no',index+1),element('span','code-text',text || ' '));fragment.append(row);
    });
    $('source').replaceChildren(fragment);
  }
  if (line !== sourceLine) {
    $('source').querySelector('.active')?.classList.remove('active');
    const row=$('source').querySelector(`[data-line="${line}"]`);row?.classList.add('active');
    sourceLine=line;
  }
  const active=$('source').querySelector(`[data-line="${line}"]`);
  if(active && $('source').clientHeight>0 && (active.offsetTop < $('source').scrollTop || active.offsetTop > $('source').scrollTop+$('source').clientHeight-45)) {
    $('source').scrollTop=Math.max(0,active.offsetTop-$('source').clientHeight/3);
  }
}
function inspectedEvent() { return selectedEvent || (selectedScope ? scopes.get(selectedScope) : latest); }
function valueView(value, before, expand) {
  const text=format(value,true), cls=before?'before':'';
  if(value && typeof value==='object' && text.length>180){
    const details=element('details',cls), keys=Object.keys(value), summary=element('summary','',`${before?'此前 · ':''}${keys.length} ${Array.isArray(value)?'项':'字段'} · ${keys.slice(0,5).join(', ')}${keys.length>5?'…':''}`);
    details.open=expand;details.append(summary,element('pre','',text));return details;
  }
  return element('pre',cls,(before?'此前 ':'')+text);
}
function renderVariables() {
  const event=inspectedEvent();if(!event)return;
  const filter=$('variable-filter').value.toLowerCase();
  const entries=Object.entries(event.locals || {}).filter(([name])=>name.toLowerCase().includes(filter));
  $('variable-count').textContent=`${entries.length} 个`;
  const changed=new Map((event.changes || []).map(change=>[change.name,change]));
  const fragment=document.createDocumentFragment();
  entries.sort(([a],[b])=>Number(changed.has(b))-Number(changed.has(a)) || a.localeCompare(b)).forEach(([name,value])=>{
    const change=changed.get(name), row=element('div','variable'+(change?' changed':''));
    const title=element('div','name');title.append(element('span','',name),element('span','type',Array.isArray(value)?'array':value===null?'null':typeof value));row.append(title);
    if(change?.op==='changed')row.append(valueView(change.before,true,false));
    row.append(valueView(value,false,!!filter));fragment.append(row);
  });
  if(!entries.length)fragment.append(element('div','empty','当前作用域没有匹配的变量。'));
  $('variables').replaceChildren(fragment);
  $('location').textContent=`${event.function}() : ${event.line}`;
  $('position').textContent=`${selectedEvent?'回看':'实时'} · ${event.file}:${event.line} · ${event.event}`;
  $('thread').textContent=event.thread;
  if($('follow').checked || selectedEvent || selectedScope)showSource(event.file,event.line).catch(showError);
}
function selectEvent(event) {
  selectedEvent=event;selectedScope='';$('scope').value='';$('follow').checked=false;
  renderVariables();renderEvents();
}
function describeEvent(event) {
  if(event.kind==='trace')return `${event.function}() · ${event.file}:${event.line}${event.changes.length?' · '+event.changes.map(c=>c.name).join(', '):''}${event.exception?' · '+event.exception.type:''}`;
  return event.message || `${event.status || event.path || event.video || ''}${event.code!==undefined?' · code='+event.code:''}${event.returncode!==undefined?' · exit='+event.returncode:''}`;
}
function renderEvents() {
  const filter=$('event-filter').value.toLowerCase(), fragment=document.createDocumentFragment();
  events.filter(e=>(describeEvent(e)+' '+e.kind).toLowerCase().includes(filter)).slice(-350).reverse().forEach(event=>{
    const row=element('button','event-row'+(selectedEvent?.id===event.id?' active':''));row.type='button';
    row.append(element('time','',timeLabel(event.at)),element('span','etype',event.event || event.kind),element('span','',describeEvent(event)));
    row.title=describeEvent(event);row.addEventListener('click',()=>{if(event.kind==='trace')selectEvent(event);});fragment.append(row);
  });
  $('events').replaceChildren(fragment);
}
function renderChanges() {
  const fragment=document.createDocumentFragment();
  changes.slice(-80).reverse().forEach(({event,change})=>{
    const row=element('button','change-row');row.type='button';
    const name=element('span','change-name',change.name);name.append(element('span','operation',{created:'新建',changed:'修改',removed:'移除'}[change.op]));
    const value=change.op==='created'?short(change.after):`${short(change.before)} → ${short(change.after)}`;
    row.append(element('time','',timeLabel(event.at)),name,element('span','change-value',value));
    row.title=`${event.function} @ ${event.file}:${event.previous_line || event.line}\n${value}`;
    row.addEventListener('click',()=>selectEvent(event));fragment.append(row);
  });
  if(!changes.length)fragment.append(element('p','empty','启动后将实时显示变化记录。'));
  $('changes').replaceChildren(fragment);
}
function renderScopes(items) {
  const signature=items.map(i=>i.scope).join('|');if($('scope').dataset.signature===signature)return;
  $('scope').dataset.signature=signature;
  const initial=element('option','','当前执行作用域');initial.value='';$('scope').replaceChildren(initial);
  items.forEach(item=>{const option=element('option','',`${item.file} / ${item.function} #${item.scope.split(':').pop().slice(-5)}`);option.value=item.scope;$('scope').append(option);});
  $('scope').value=selectedScope;
}
function clearRun() {
  events=[];changes=[];consoles=[];scopes.clear();selectedEvent=null;selectedScope='';latest=null;browserHistoryTruncated=false;
  $('scope').dataset.signature='';$('variables').replaceChildren(element('div','empty','等待本次运行的变量…'));
  $('follow').checked=true;$('console').textContent='等待程序输出…';$('history-note').textContent='';
  logSignature='';roundSignature='';laneSignature='';keyLogs=[];
}
async function poll() {
  try {
    const data=await api(`/api/snapshot?after=${cursor}&detail=${$('debug-details').checked?1:0}&lane=${$('debug-lane').value}`);
    durationRangeSupported = data.duration_range_supported === true;
    efficiencyControlsSupported = data.efficiency_controls_supported === true;
    periodicHeartbeatSupported = data.periodic_heartbeat_supported === true;
    $('connection').textContent='本地服务已连接';$('connection').style.color='';
    if(data.run_dir!==lastRun){lastRun=data.run_dir;clearRun();if(data.run_dir){$('mode').value=data.mode;if(data.batch?.ip_limit!==undefined)$('ip-limit').value=data.batch.ip_limit;if(data.batch?.failure_limit!==undefined)$('failure-limit').value=data.batch.failure_limit;if(data.batch?.seconds_min!==undefined){$('seconds-min').value=data.batch.seconds_min;$('seconds-max').value=data.batch.seconds_max;}$('mode').dispatchEvent(new Event('change'));}}
    cursor=data.cursor;
    keyLogs=data.logs_incremental?[...new Map([...keyLogs,...(data.logs || [])].map(e=>[e.id,e])).values()].slice(-2000):(data.logs || []);
    const dispatch=keyLogs.filter(e=>e.kind==='dispatch').at(-1);
    const dispatchPaused=data.dispatch_paused ?? dispatch?.paused ?? false;
    data.events.forEach(event=>{
      events.push(event);
      if(event.kind==='trace' && event.lane===Number($('debug-lane').value)){
        scopes.set(event.scope,event);
        event.changes.forEach(change=>changes.push({event,change}));
      }
      if(['console','failure','notice','launch','lifecycle','network','control','process_exit','proxy'].includes(event.kind))consoles.push(`[${timeLabel(event.at)}] ${event.kind} ${describeEvent(event)}`);
    });
    if(events.length>6000)browserHistoryTruncated=true;
    events=events.slice(-6000);changes=changes.slice(-300);consoles=consoles.slice(-400);
    latest=data.current;
    if(latest)scopes.set(latest.scope,latest);
    renderScopes(data.scopes);
    if($('debug-details').checked && !selectedEvent)renderVariables();
    if(data.events.length && $('debug-details').checked){renderChanges();if(currentTab==='events')renderEvents();}
    $('status').textContent=data.running && dispatchPaused?'已暂停派发':statusLabels[data.status] || data.status;
    $('pid').textContent=data.pid?`PID ${data.pid} · ${data.mode==='demo'?'离线演示':'真实播放'}`:'独立 Python 子进程';
    const proxy=data.proxy || {status:'disabled'};
    const proxyLabels={disabled:'本次代理未启用',acquiring:'正在提取新代理…',verifying:'正在验证公网出口…',retrying:'IP 失败，等待重试',ready:'已验证公网出口',failed:'本轮代理失败',cancelled:'已取消'};
    $('proxy-status').textContent=[proxyLabels[proxy.status] || proxy.status,proxy.exit_ip?`公网 IP：${proxy.exit_ip}`:'',proxy.endpoint?`代理：${proxy.endpoint}`:'',proxy.remaining!==undefined&&proxy.remaining!==null?`剩余额度：${proxy.remaining}`:'',proxy.message || ''].filter(Boolean).join(' · ');
    $('proxy-status').dataset.status=proxy.status;
    $('new-ip').disabled=data.running || $('mode').value!=='live';
    const m=data.metrics, item=data.state?.attempts?Object.values(data.state.attempts)[0]:null;
    const batch=data.batch || {total:1,repeat:1,current:0,completed:0,failed:0,stopped:0,requests:0,errors:0,rounds:[]};
    $('ip-used').textContent=batch.ip_used ?? (data.run_dir?'—':0);
    $('ip-used-note').textContent=batch.ip_used===undefined && data.run_dir?'本批结束后重启服务启用统计':'本批成功提取，含重试';
    // Older running inspectors already relay these events; allow a page refresh
    // to show live progress without interrupting their active task for a restart.
    const workers=(data.workers || []).map(worker=>{
      if(worker.progress!==undefined)return worker;
      const progress=[...(data.logs || [])].reverse().find(event=>event.kind==='progress' && event.lane===worker.lane && event.round===worker.round);
      return {...worker,progress:progress || {}};
    });
    $('current-ip').textContent=proxy.exit_ip || (proxy.enabled?'获取中…':'—');
    $('current-ip-label').textContent='当前公网 IP';
    $('current-proxy').textContent=proxy.endpoint?`代理 ${proxy.endpoint}`:proxy.enabled?(proxyLabels[proxy.status] || proxy.status):'未启用代理';
    if(proxy.status==='failed' || proxy.status==='cancelled')$('current-ip').textContent='—';
    if(workers.length>1 && workers.some(w=>w.proxy.enabled)){
      const ready=workers.filter(w=>w.proxy.status==='ready').length;
      $('current-ip-label').textContent='已验证出口 / 并发路';
      $('current-ip').textContent=`${ready} / ${workers.length} 路`;
      $('current-proxy').textContent='各路公网出口见下表';
      $('proxy-status').textContent=`${workers.length} 路独立获取代理 · 提取尝试 ${batch.ip_attempts} 次 · 获取或验证失败 ${batch.ip_failures} 次（已记录并继续）`;
      $('proxy-status').dataset.status='disabled';
    }
    else if(workers.length>1){$('proxy-status').textContent=`并发 ${workers.length} 路 · 本次未启用代理`;$('proxy-status').dataset.status='disabled';}
    const finished=batch.completed+batch.failed+(batch.stopped || 0);
    $('batch-progress').textContent=`${finished} / ${batch.total}`;
    $('task-progress').max=Math.max(1,batch.total);$('task-progress').value=finished;
    $('batch-summary').textContent=`成功 ${batch.completed} · 失败 ${batch.failed} · ${data.status==='stopped' || batch.quota_exhausted?'已取消':'待执行'} ${Math.max(0,batch.total-batch.current)}`;
    $('limits-status').textContent=!efficiencyControlsSupported?'当前服务尚未启用消耗保护；本批结束后重启服务生效。':`${batch.ip_limit?`提取 ${batch.ip_attempts} / 上限 ${batch.ip_limit} 次`:'未设置提取上限'}${batch.quota_exhausted?' · 已达上限，后续轮次已取消':''}${dispatchPaused?' · '+(batch.dispatch_message || '已暂停派发'):''}`;
    renderRounds(batch,data.running,workers);renderLanes(workers,batch);renderKeyLogs(keyLogs,batch.total);
    $('pause').textContent=data.trace_enabled && batch.concurrency===1 && !proxy.enabled?'暂停调试':'暂停派发';
    $('debug-note').textContent=data.run_dir&&!data.trace_enabled?'本次仅记录关键日志；下次启动前打开「详细调试」可同时记录源码与变量。':'';
    if(data.trace_capped)$('debug-note').textContent='详细追踪已达到本批 32 MiB 上限；关键日志继续记录。';
    const selectedProgress=workers.find(w=>w.lane===Number($('debug-lane').value))?.progress || {};
    const decoded=selectedProgress.decoded_seconds ?? m.decoded_seconds ?? item?.decoded_seconds ?? m.decoded, wall=selectedProgress.wall_seconds ?? m.wall_seconds ?? item?.wall_seconds ?? m.elapsed;
    $('decoded').textContent=typeof decoded==='number'?decoded.toFixed(2):'—';
    $('frames').textContent=selectedProgress.frames ?? m.frames ?? item?.frames ?? '—';$('api-count').textContent=batch.requests;
    $('target').textContent=`目标时长 ${selectedProgress.target_seconds ?? m.target ?? data.state?.seconds ?? '—'} 秒`;
    $('wall').textContent=`解码墙钟时间 ${typeof wall==='number'?wall.toFixed(2):'—'} 秒`;
    $('event-count').textContent=`接口异常 ${batch.errors} 次 · IP 失败 ${batch.ip_failures || 0} 次`;
    $('run-dir').textContent=data.run_dir || '每次启动保留独立运行目录';
    lastState=data.state;$('state').textContent=lastState?format(lastState,true):'尚未生成 state.json';
    runActive=Boolean(data.running);
    $('device-effective').textContent=workers.filter(w=>w.device_profile).map(w=>`第 ${w.lane} 路 / 第 ${w.round} 轮：${w.device_profile.label} · ${['random-device','random-offline'].includes(w.device_profile.selection)?'随机抽取':'指定配置'} · ${w.device_profile.common.model}`).join('；') || '尚未生成本轮设备配置';
    $('device-profile').disabled=data.running || !deviceProfilesReady;
    $('device-confirmed').disabled=data.running;
    $('start').disabled=data.running || busy || !deviceProfilesReady;
    ['mode','video','seconds-min','seconds-max','repeat','concurrency','start-paused','periodic-heartbeat'].forEach(id=>$(id).disabled=data.running || (id==='video' && $('mode').value!=='live'));
    if(data.running)$('periodic-heartbeat').checked=Boolean(data.batch?.heartbeat_interval);
    $('ip-retries').disabled=data.running || $('mode').value!=='live';
    $('ip-limit').disabled=data.running || $('mode').value!=='live';
    $('failure-limit').disabled=data.running;
    $('debug-details').disabled=(data.running?batch.concurrency:Number($('concurrency').value))>1;
    ['pause','resume','step','stop'].forEach(id=>$(id).disabled=!data.running);
    if(data.history_truncated || browserHistoryTruncated)$('history-note').textContent='完整关键日志见 key-events.jsonl';
    setTimeout(poll,data.latest_id>cursor?30:1000);
  } catch(error) {
    $('connection').textContent='连接中断';$('connection').style.color='#b55350';
    setTimeout(poll,1500);
  }
}
$('launch').addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;busy=true;$('start').disabled=true;$('error').hidden=true;
  try{
    if(!durationRangeSupported)throw new Error('当前服务尚未启用时长区间，请在当前批次结束后重启观察服务并刷新页面。');
    if(!efficiencyControlsSupported)throw new Error('请在当前批次结束后重启观察服务，以启用消耗保护。');
    if($('periodic-heartbeat').checked&&!periodicHeartbeatSupported)throw new Error('当前服务尚未支持周期心跳，请在当前批次结束后重启观察服务并刷新页面。');
    await api('/api/start',{mode:$('mode').value,device_profile:$('device-profile').value,device_confirmed:$('device-confirmed').checked,video:$('video').value,seconds_min:Number($('seconds-min').value),seconds_max:Number($('seconds-max').value),repeat:Number($('repeat').value),concurrency:Number($('concurrency').value),ip_retries:Number($('ip-retries').value),ip_limit:Number($('ip-limit').value),failure_limit:Number($('failure-limit').value),heartbeat_interval:$('periodic-heartbeat').checked?15:0,debug:$('debug-details').checked,paused:$('debug-details').checked&&$('start-paused').checked,new_ip:$('mode').value==='live'&&$('new-ip').checked});
  }
  catch(error){showError(error);}finally{busy=false;$('start').disabled=runActive || !deviceProfilesReady;}
});
['pause','resume','step','stop'].forEach(command=>$(command).addEventListener('click',async()=>{
  $('error').hidden=true;try{await api('/api/control',{command});if(command==='resume'){selectedEvent=null;selectedScope='';$('scope').value='';$('follow').checked=true;}}
  catch(error){showError(error);}
}));
$('mode').addEventListener('change',()=>{
  const live=$('mode').value==='live';
  const total=Number($('concurrency').value)*Number($('repeat').value);
  $('mode-note').textContent=`${$('concurrency').value} 路 × 每路 ${$('repeat').value} 轮 = ${total} 次。每轮在 ${$('seconds-min').value}～${$('seconds-max').value} 秒内独立随机，两端相同则固定。失败等待 5 秒后继续；暂停派发会让当前轮完成。${live?'':'离线模式不访问哔哩哔哩。'}`;
  $('video').disabled=!live;
  $('new-ip').disabled=!live || $('start').disabled;
  $('ip-retries').disabled=!live || $('start').disabled;
  $('ip-limit').disabled=!live || $('start').disabled;
  $('proxy-note').textContent=live?'提取与验证失败按重试次数处理；代理剩余寿命须覆盖播放与收尾。重新提取可能消耗额度。':'离线演示不提取 IP、不消耗额度；切换到真实播放后可启用。';
});
$('mode').dispatchEvent(new Event('change'));
['seconds-min','seconds-max'].forEach(id=>$(id).addEventListener('input',()=>{
  $('seconds-max').setCustomValidity(Number($('seconds-min').value)>Number($('seconds-max').value)?'最长时长不能小于最短时长':'');
  $('mode').dispatchEvent(new Event('change'));
}));
$('mode').addEventListener('change',()=>{if(deviceProfilesReady)refreshDeviceOptions();});
$('device-profile').addEventListener('change',()=>{$('device-confirmed').checked=false;renderDevicePreview();});
api('/api/device-profiles').then(data=>{
  deviceProfiles=data.profiles;deviceProfilesReady=true;refreshDeviceOptions();
  $('device-profile').disabled=runActive;$('start').disabled=runActive || busy;
}).catch(showError);
['repeat','concurrency'].forEach(id=>$(id).addEventListener('input',()=>{
  $('mode').dispatchEvent(new Event('change'));
  if(Number($('concurrency').value)>1 && $('debug-details').checked){$('debug-details').checked=false;$('debug-details').dispatchEvent(new Event('change'));}
  $('debug-details').disabled=Number($('concurrency').value)>1;
}));
$('debug-details').addEventListener('change',()=>{
  document.body.classList.toggle('compact',!$('debug-details').checked);
  cursor=0;events=[];changes=[];scopes.clear();selectedEvent=null;selectedScope='';
  if(!$('debug-details').checked)document.querySelector('[data-tab="console"]').click();
  else showSource('tv_playback.py').catch(showError);
});
$('debug-lane').addEventListener('change',()=>{cursor=0;events=[];changes=[];scopes.clear();selectedEvent=null;selectedScope='';latest=null;$('scope').dataset.signature='';});
$('follow').addEventListener('change',()=>{if($('follow').checked){selectedEvent=null;selectedScope='';$('scope').value='';renderVariables();}});
$('scope').addEventListener('change',()=>{selectedScope=$('scope').value;selectedEvent=null;$('follow').checked=!selectedScope;renderVariables();});
$('source-file').addEventListener('change',()=>{$('follow').checked=false;showSource($('source-file').value).catch(showError);});
$('variable-filter').addEventListener('input',renderVariables);
$('event-filter').addEventListener('input',renderEvents);
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{
  currentTab=button.dataset.tab;
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('selected',b===button));
  document.querySelectorAll('.tab-view').forEach(view=>view.hidden=view.id!==`${currentTab}-view`);
  if(currentTab==='events')renderEvents();
  if(currentTab==='source' && inspectedEvent())showSource(inspectedEvent().file,inspectedEvent().line).catch(showError);
}));
poll();
