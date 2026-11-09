'use strict';
const $=id=>document.getElementById(id);
const token=document.querySelector('meta[name="monitor-token"]').content;
const labels={starting:'首次查询中',watching:'观测中',retrying:'查询失败 · 等待重试',stopping:'正在停止',stopped:'已停止',completed:'观测结束',interrupted:'服务曾中断',error:'观测异常'};
let selected='', snapshot=null, busy=false, historySignature='', renderedRows='', connected=false;
function num(v){return typeof v==='number'?v.toLocaleString('zh-CN'):'—';}
function delta(v){return typeof v==='number'?(v>0?'+':'')+num(v):'—';}
function stamp(v){return v?new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';}
function cell(text,cls){const td=document.createElement('td');td.textContent=text;if(cls)td.className=cls;return td;}
function error(err){$('error').hidden=false;$('error').textContent=err.message || String(err);}
async function api(path,data){const response=await fetch(path,{method:data?'POST':'GET',headers:{'X-Monitor-Token':token,...(data?{'Content-Type':'application/json'}:{})},...(data?{body:JSON.stringify(data)}:{})});const result=await response.json();if(!response.ok)throw new Error(result.error || `HTTP ${response.status}`);return result;}
function renderTable(){
  if(!snapshot)return;const s=snapshot.session, filter=$('only-changes').checked;
  const signature=`${s?.id}:${s?.sample_count}:${filter}`;if(signature===renderedRows)return;renderedRows=signature;
  const rows=snapshot.samples.filter(r=>!filter || !r.ok || r.delta_previous!==0).slice().reverse();
  const fragment=document.createDocumentFragment();
  rows.forEach(row=>{const tr=document.createElement('tr');if(row.delta_previous>0)tr.className='increased';
    tr.append(cell(stamp(row.observed_at)),cell(num(row.view)),cell(delta(row.delta_baseline),row.delta_baseline>0?'positive':row.delta_baseline<0?'negative':''),cell(delta(row.delta_previous),row.delta_previous>0?'positive':row.delta_previous<0?'negative':''),cell(row.ok?`成功 · ${row.latency_ms} ms`:row.error,'result'));fragment.append(tr);});
  if(!rows.length){const tr=document.createElement('tr'),td=cell(filter?'尚无播放量变化或错误记录。':'等待首次查询结果…','empty');td.colSpan=5;tr.append(td);fragment.append(tr);}
  $('rows').replaceChildren(fragment);$('row-count').textContent=`显示 ${rows.length} 条 / 共 ${s?.sample_count || 0} 次查询`;
}
function render(data){
  snapshot=data;const s=data.session;
  $('start').disabled=data.running || busy;$('stop').disabled=!data.running || data.active_id!==s?.id || s?.status==='stopping';
  $('export-csv').disabled=$('export-json').disabled=!s;
  const signature=data.history.map(h=>h.id+':'+h.status).join('|');
  if(signature!==historySignature){historySignature=signature;const option=document.createElement('option');option.value='';option.textContent='当前 / 最近一次观测';$('history').replaceChildren(option);data.history.forEach(h=>{const item=document.createElement('option');item.value=h.id;item.textContent=`${h.bvid || '待解析'} · ${h.started_at.slice(5,16).replace('T',' ')}`;$('history').append(item);});$('history').value=selected;}
  if(!s)return;
  $('video-title').textContent=s.title || '正在获取视频信息…';
  $('video-info').textContent=`${s.bvid || s.query_value}${s.owner?' · UP主 '+s.owner:''} · 每 ${s.interval_seconds} 秒查询`;
  $('status').textContent=labels[s.status] || s.status;
  $('baseline').textContent=num(s.baseline_view);$('baseline-at').textContent=stamp(s.baseline_at);
  $('latest').textContent=num(s.latest_view);$('latest-at').textContent=stamp(s.latest_at);
  $('delta').textContent=delta(s.delta_baseline);$('delta').className=s.delta_baseline>0?'positive':s.delta_baseline<0?'negative':'';
  $('success').textContent=num(s.success_count);$('attempts').textContent=`查询 ${s.sample_count} 次 · 失败 ${s.error_count} 次`;
  $('verdict').textContent=s.success_count<2?'至少两次成功查询后比较':s.delta_baseline>0?'观测到公开播放量增加':s.delta_baseline<0?'公开数值低于基线':s.first_increase_at?'当前回到基线，期间曾增加':'当前观测窗口内尚无变化';
  $('last-error').hidden=!s.last_error;$('last-error').textContent=s.last_error?`${s.last_error}。保留上次有效数据，失败不计为 0。`:'';
  $('end-at').textContent=s.ended_at?`结束于 ${stamp(s.ended_at)}`:`计划结束 ${stamp(s.expected_end_at)}`;
  $('storage').textContent=`保存目录：${data.data_dir}\\${s.id} · samples.csv / samples.jsonl / session.json`;
  renderTable();renderClock();
}
function renderClock(){if(!snapshot?.session)return;const s=snapshot.session;
  if(!connected){$('freshness').textContent='本地服务断开，页面数据未更新';return;}
  if(s.next_sample_at && s.status!=='stopping'){
    const left=Math.ceil((new Date(s.next_sample_at)-Date.now())/1000);$('next').textContent=left>0?`下一次查询：${left} 秒后`:'正在查询…';
  }else $('next').textContent=s.status==='stopping'?'停止中，等待当前请求返回…':'下一次查询 —';
  $('freshness').textContent=s.latest_at?`有效数据距今 ${Math.max(0,Math.floor((Date.now()-new Date(s.latest_at))/1000))} 秒`:'尚无有效数据';
}
async function poll(){try{const data=await api('/api/status'+(selected?'?session='+encodeURIComponent(selected):''));connected=true;$('connection').textContent='本地服务已连接';render(data);}catch(err){connected=false;$('connection').textContent='连接中断 · 正在重试';$('start').disabled=$('stop').disabled=true;renderClock();}finally{setTimeout(poll,2000);}}
$('start-form').addEventListener('submit',async event=>{event.preventDefault();if(busy)return;busy=true;$('start').disabled=true;$('error').hidden=true;
  try{await api('/api/start',{video:$('video').value,interval:Number($('interval').value),minutes:Number($('minutes').value)});selected='';$('history').value='';renderedRows='';render(await api('/api/status'));}catch(err){error(err);}finally{busy=false;}});
$('stop').addEventListener('click',async()=>{try{await api('/api/stop',{});render(await api('/api/status'+(selected?'?session='+encodeURIComponent(selected):'')));}catch(err){error(err);}});
$('history').addEventListener('change',async()=>{selected=$('history').value;renderedRows='';try{render(await api('/api/status'+(selected?'?session='+encodeURIComponent(selected):'')));}catch(err){error(err);}});
$('only-changes').addEventListener('change',renderTable);
async function download(format){if(!snapshot?.session)return;try{const response=await fetch(`/api/export?session=${snapshot.session.id}&format=${format}`,{headers:{'X-Monitor-Token':token}});if(!response.ok)throw new Error((await response.json()).error);const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`${snapshot.session.bvid || 'video'}-${snapshot.session.id}.${format}`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(err){error(err);}}
$('export-csv').addEventListener('click',()=>download('csv'));$('export-json').addEventListener('click',()=>download('json'));
setInterval(renderClock,1000);poll();
