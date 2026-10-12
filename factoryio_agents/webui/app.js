'use strict';
const $=id=>document.getElementById(id);
const token=document.querySelector('meta[name="dashboard-token"]').content;
const roles={planner:'Agent1 · 规划',scene:'Agent2 · 场景',vision:'Agent3 · 视觉',plc:'PLC Agent',tools:'工具',supervisor:'监督 Agent'};
const phases={spec:'需求拆解与物品选择',execution:'供料与控制流程',measurement_signals:'定义观测信号',measurement_probes:'设计测试操作',measurement_checks:'设计验收规则',acceptance:'规划验收',planning:'规划需求',layout_proposal:'提出布局方案',layout_review:'规划审核布局',layout:'安排物品坐标',layout_constraints:'组织相对布局',configuration_io:'配置组件与 I/O',scene_review:'检查场景布局',scene:'检查场景布局',runtime_review:'检查运行流程',runtime:'检查运行流程',plc_generation:'生成 PLC 代码',native_geometry:'读取组件几何',environment_preflight:'检查环境',deployment_preflight:'检查部署环境',compile:'编译部署 PLC',connection:'连接 Modbus',runtime_control:'控制仿真运行'};
const statuses={waiting:'等待手动执行',running:'执行中',completed:'已返回',released:'已放行',failed:'失败',cancelled:'已取消',cancelling:'取消后续，等待当前调用返回',finished:'流程已结束',history:'历史记录',passed:'验收通过',awaiting_scene_review:'场景文件生成，待验证',needs_attention:'需要处理',needs_configuration:'配置问题',repair_limit_reached:'达到修改上限',archived_review_completed:'历史图片复查已返回'};
Object.assign(phases,{scene_load:'加载生成场景',scene_geometry:'检查物体放置与输送连接',scene_geometry_capability:'检查几何工具能力',device_binding:'匹配 PLC 通道'});
let bootstrap=null,currentSession=null,currentStep=null,currentTab='request',lastRevision=-1,lastDetailKey='',busy=false;
const fmt=v=>JSON.stringify(v,null,2);
function logText(value){return value==null?'':Array.isArray(value)?value.map(v=>typeof v==='string'?v:fmt(v)).join('\n'):typeof value==='string'?value:fmt(value);}
function clock(value){return value?new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Asia/Shanghai'}).format(new Date(value)):'—';}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,5500);}
async function api(path,body){const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':token},body:JSON.stringify(body)};const res=await fetch(path,options);const data=await res.json();if(!res.ok)throw new Error(data.error||'请求失败');return data;}
function button(text,cls,handler){const element=document.createElement('button');element.textContent=text;element.className=cls;element.addEventListener('click',handler);return element;}
function badge(id,status){$(id).textContent=statuses[status]||status;$(id).className='badge '+status;}
async function refresh(){try{bootstrap=await api('/api/bootstrap');$('connection').textContent='本地服务已连接';renderLists();}catch(error){$('connection').textContent='连接中断';toast(error.message);}}
function renderLists(){
  $('sessions').replaceChildren();for(const session of bootstrap.sessions){const item=button(session.name,'session-item'+(session.id===currentSession?' selected':''),()=>selectSession(session.id));const small=document.createElement('small');const status=session.status==='finished'?(session.result?.status||session.status):session.status;small.textContent=(statuses[status]||status)+' · '+session.steps.length+' 步';item.append(small);$('sessions').append(item);}
  $('history').replaceChildren();const query=$('history-search').value.toLowerCase();for(const session of bootstrap.history.filter(s=>s.name.toLowerCase().includes(query))){const item=button(session.name,'session-item'+(session.id===currentSession?' selected':''),()=>selectSession(session.id));$('history').append(item);}
  $('models').replaceChildren();for(const [role,model] of Object.entries(bootstrap.models)){const row=document.createElement('div');const name=document.createElement('strong');name.textContent=roles[role]+'：';row.append(name,document.createTextNode(model||'未配置'));$('models').append(row);}
}
async function selectSession(id){currentSession=id;currentStep=null;lastRevision=-1;lastDetailKey='';$('welcome').hidden=true;$('session-view').hidden=false;renderLists();await poll();history.replaceState(null,'','?session='+encodeURIComponent(id));}
async function poll(){if(!currentSession)return;try{const session=await api('/api/sessions/'+currentSession);if(session.id!==currentSession)return;window.sessionState=session;
  $('session-mode').textContent=session.historical?'历史运行 · 只读':({vision:'历史截图复查 · 手动控制',prepare:'仅生成任务与场景文件 · 手动控制',run:'完整联机流程 · 手动控制',resume:'恢复已有任务 · 手动控制'}[session.mode]||'现有 Agent 框架 · 手动控制');$('session-title').textContent=session.name;$('output-path').textContent=session.output;badge('session-status',session.status==='finished'?(session.result?.status||session.status):session.status);
  $('cancel').hidden=session.historical||['finished','failed','cancelled','history'].includes(session.status);$('cancel').disabled=session.status==='cancelling';$('history-actions').hidden=!session.historical;
  renderContinuation(session);
  $('step-count').textContent=session.steps.length+' 步';renderTimeline(session);$('result-panel').hidden=!session.result;$('result').textContent=fmt(session.result);
  $('events').textContent=session.events.map(e=>clock(e.timestamp)+' '+(roles[e.active_role]||'')+' '+(e.event||'')+'\n'+logText(e.text||e.summary||e.error)).join('\n\n');
  if(!currentStep&&session.steps.length)currentStep=session.pending_id||session.steps.at(-1).id;
  if(currentStep){const item=session.steps.find(s=>s.id===currentStep);const key=currentStep+'-'+session.revision;if(item&&key!==lastDetailKey){lastDetailKey=key;await loadStep(currentStep);}}
  lastRevision=session.revision;
}catch(error){toast(error.message);}}
function renderContinuation(session){
  $('pending-banner').hidden=!!session.historical;
  const next=session.steps.find(s=>s.id===session.pending_id&&s.status==='waiting');
  const ended=['finished','failed','cancelled'].includes(session.status);
  const recoverable=ended&&!session.historical&&['run','resume'].includes(session.mode)&&
    ['needs_configuration','needs_attention','failed','repair_limit_reached','cancelled'].includes(session.result?.status)&&
    !!(session.result?.phase||session.result?.status==='cancelled');
  $('recover-workflow').hidden=!recoverable;
  $('recover-workflow').disabled=busy;
  $('view-pending').hidden=!next;
  $('continue-next').disabled=busy||!next;
  $('continue-next').dataset.stepId=next?.id||'';
  $('new-full-workflow').hidden=!ended;
  if(next){
    $('continuation-title').textContent='下一步正在等待你';
    $('pending-label').textContent='第 '+next.number+' 步 · '+(roles[next.role]||next.role)+' / '+(phases[next.phase]||next.phase);
    $('continuation-hint').textContent='先查看下一步的请求和图片。点击“继续下一步”只放行这一项，后续步骤仍需手动继续。';
    $('continue-next').textContent='继续下一步';
  }else if(ended){
    const visionDone=session.mode==='vision'&&session.status==='finished';
    const failure=session.result?.error;
    const geometryBlocked=session.result?.phase==='scene_geometry';
    $('continuation-title').textContent=visionDone?'本次视觉复查已结束':geometryBlocked?'场景已加载，几何检查未通过':failure&&session.result.phase==='environment_preflight'?'环境检查失败，尚未进入规划 Agent':failure?'流程中断，需要处理':'本轮流程已结束';
    $('pending-label').textContent=statuses[session.result?.status||session.status]||session.result?.status||session.status;
    const services=session.result?.diagnostics?.services||[];
    const blockers=services.filter(s=>s.status!=='ok'||!s.service_running);
    const issues=session.result?.diagnostics?.issues||[];
    const issueText={'native conveyor component geometry missing':'桥接未返回输送方向与表面几何','native voxel registration failed: overlap or out-of-world placement':'未通过原生放置检查：可能越界或重叠','emission origin is outside conveyor XZ footprint':'供料位置不在输送带覆盖范围内','sensor ray cannot reach conveyor XZ footprint':'传感器检测范围未覆盖输送带'};
    const geometryReason=geometryBlocked?[session.result.diagnostics?.spatial_checks_applied===false?'几何工具未完成检查，Vision 与 PLC 的后续步骤尚未执行。':'物体放置检查发现问题，需由场景 Agent 修正。',...issues.map(i=>(i.object_id||'场景')+(i.prefab?' ('+i.prefab+')':'')+'：'+(issueText[i.error]||i.error))].join('\n'):'';
    const reason=geometryReason|| (blockers.length?blockers.map(s=>s.service+'：'+(s.state||'未知状态')+'；'+(s.windows_error===5?'Windows 拒绝启动服务，当前进程没有启动权限。':s.error||'服务未运行。')).join('\n'):failure||'');
    const advice=geometryBlocked&&session.result.diagnostics?.scope==='missing_conveyor_geometry'?'先更新 Factory I/O 内的桥接工具，再恢复任务；恢复时重新读取组件几何，并把放置问题反馈给场景 Agent。':blockers.some(s=>s.windows_error===5)?'先通过 CODESYS 的 Runtime 控制器或管理员 PowerShell 启动服务，再点击“恢复此任务，手动继续”。':'';
    $('continuation-hint').textContent=visionDone?'此任务只进行视觉检查，没有场景生成、修改或 PLC 后续步骤。要逐步运行全部 Agent，请新建完整流程任务。':recoverable?[reason,advice,'恢复会保留原始需求和记录，复用通过检查的产物，并重新检查环境。下一项仍由你手动放行。'].filter(Boolean).join('\n'):failure||'本轮没有待执行的请求。可以查看流程结果，或新建完整流程任务。';
    $('continue-next').textContent='没有待执行步骤';
  }else{
    const running=session.steps.findLast(s=>s.status==='running');
    $('continuation-title').textContent=session.status==='cancelling'?'正在取消后续步骤':running?'当前步骤执行中':'正在准备下一步';
    $('pending-label').textContent=running?'第 '+running.number+' 步 · '+(roles[running.role]||running.role)+' / '+(phases[running.phase]||running.phase):'流程正在整理上下文或执行已放行的操作';
    $('continuation-hint').textContent='下一项请求准备完成后，“继续下一步”按钮会启用。';
    $('continue-next').textContent='等待本步返回';
  }
}
// Older servers record permission and execution separately. Derive the display from
// the bounded workflow events without rewriting the original step or claiming acceptance.
function toolStep(step,session){
  if(step.kind!=='tool'||!session||!['released','running'].includes(step.status))return step;
  const start=Date.parse(step.request?.details?.timestamp||step.created_at);
  if(!Number.isFinite(start))return step;
  const index=session.steps.findIndex(s=>s.id===step.id);
  if(index<0)return step;
  const end=Date.parse(session.steps[index+1]?.created_at)||Infinity;
  const events=(session.events||[]).filter(e=>Date.parse(e.timestamp)>=start&&Date.parse(e.timestamp)<end);
  const completion=events.find(e=>e.event==='tool_completed'&&(!step.request?.tool||e.tool===step.request.tool)&&e.phase===step.phase);
  if(!completion)return step;
  const failed=completion.ready===false||completion.compiled===false||!!completion.error;
  const transition=failed?events.find(e=>e.event==='workflow_transition'&&e.phase===step.phase&&e.error):null;
  return {...step,status:failed?'failed':'completed',finished_at:completion.timestamp,
    response:{...completion,result_origin:'workflow_trace',...(transition?{error:transition.error,diagnostics:transition.diagnostics}:{})},
    raw_response:null,error:failed?(transition?.error||completion.error||'工具返回未通过，请查看执行结果。'):null};
}
function renderTimeline(session){$('timeline').replaceChildren();for(const saved of session.steps){const step=toolStep(saved,session);const item=button('','timeline-step '+step.status+(step.id===currentStep?' selected':''),async()=>{currentStep=step.id;lastDetailKey='';renderTimeline(session);await loadStep(step.id);});const dot=document.createElement('span');dot.className='step-dot';dot.textContent=step.status==='completed'?'✓':String(step.number);const box=document.createElement('div');const title=document.createElement('strong');title.textContent=phases[step.phase]||step.phase;const desc=document.createElement('small');desc.textContent=(roles[step.role]||step.role)+' · '+(statuses[step.status]||step.status)+(step.image_count?' · '+step.image_count+' 张图片':'');box.append(title,desc);item.append(dot,box);$('timeline').append(item);}}
function readable(step){const value=step.response;if(!value)return step.raw_response||'';if(value.checks){return value.checks.map(c=>'对象：'+(c.object_ids||[]).join('、')+'\n结论：'+({ok:'正常',issue:'存在问题',uncertain:'不确定'}[c.status]||c.status)+'\n观察：'+c.observed+'\n预期：'+c.expected+'\n理由：'+c.reason+'\n建议：'+c.suggestion).join('\n\n');}if(value.findings){return (value.summary||value.verdict||'')+'\n\n'+value.findings.map(f=>'对象：'+(f.object_ids||[]).join('、')+'\n理由：'+(f.reason||f.observed||'')+'\n建议：'+(f.suggestion||'')).join('\n\n');}if(value.objects){return (value.scene_goal||value.requirement||'')+'\n\n'+value.objects.map(o=>[o.id+' · '+(o.type||o.prefab||o.template_id||''),o.function||o.purpose||'',o.expected_position?fmt(o.expected_position):o.position?'位置：'+fmt(o.position):''].filter(Boolean).join('\n')).join('\n\n');}return fmt(value);}
async function loadStep(id){const sessionId=currentSession;const saved=await api('/api/sessions/'+sessionId+'/steps/'+id);if(sessionId!==currentSession||id!==currentStep)return;const step=toolStep(saved,window.sessionState);$('empty-step').hidden=true;$('step-view').hidden=false;$('step-role').textContent=roles[step.role]||step.role;$('step-title').textContent='第 '+step.number+' 步 · '+(phases[step.phase]||step.phase);$('step-time').textContent=clock(step.created_at)+(step.finished_at?' → '+clock(step.finished_at):'');badge('step-status',step.status);
  $('response-tab').textContent=step.kind==='tool'?'工具结果':'模型回复';$('readable-title').textContent=step.kind==='tool'?'工具执行结果 · 工作流日志':'可读回复';$('raw-response-panel').hidden=step.kind==='tool';
  $('approve').hidden=step.status!=='waiting'||window.sessionState.historical;$('approve').textContent=step.kind==='tool'?'放行当前工具操作':'发送此请求';
  const info=step.request.provider||{};$('provider').textContent=step.kind==='tool'?'工具：'+step.request.tool+' / '+step.request.action:[info.provider,info.model||info.requested_model,info.response_format?.type].filter(Boolean).join(' · ');
  $('system-prompt').textContent=step.request.system||'工具操作，无模型提示词';$('user-payload').textContent=fmt(step.request.payload||step.request.details);$('wire-messages').textContent=fmt({messages:step.request.messages,provider:step.request.provider});
  $('response-empty').hidden=!!step.response||!!step.raw_response||!!step.error;$('response-empty').textContent=step.status==='waiting'?'本步尚未发送。你可以先检查提示词和图片，再决定是否执行。':'正在等待本步返回。';$('response-content').hidden=$('response-empty').hidden===false;
  $('readable-response').textContent=readable(step);$('raw-response').textContent=step.raw_response||'';$('structured-response').textContent=fmt(step.response);$('step-error').hidden=!step.error;$('step-error').textContent=typeof step.error==='string'?step.error:fmt(step.error);
  renderReferences($('step-knowledge'),step.request.payload?.component_reference);
  const images=step.request.images||[];$('image-count').textContent=images.length?'('+images.length+')':'';$('image-grid').replaceChildren();if(!images.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='本步没有发送图片。';$('image-grid').append(empty);}for(const image of images){const card=document.createElement('div');card.className='image-card';const url='/api/sessions/'+sessionId+'/images/'+encodeURIComponent(image.asset_id);const picture=document.createElement('img');picture.src=url;picture.alt=image.image_id;picture.loading='lazy';const open=button('','',()=>{$('large-image').src=url;$('large-image-title').textContent=image.image_id+' · '+image.name;$('image-dialog').showModal();});open.append(picture);const meta=document.createElement('div');meta.className='image-info';const title=document.createElement('strong');title.textContent=image.image_id;const digest=document.createElement('small');digest.textContent='SHA-256 '+image.sha256;const size=document.createElement('small');size.textContent=image.size_bytes?Math.round(image.size_bytes/1024)+' KB':'';meta.append(title,digest,size);card.append(open,meta);$('image-grid').append(card);}showTab(currentTab);
}
function showTab(tab){currentTab=tab;document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));for(const name of ['request','response','images','knowledge','events'])$('tab-'+name).hidden=name!==tab;}
const bases={official_text:'官方文字',curated_summary_of_official_text:'官方文字摘要',official_image_observation:'官方图片观察',text_and_image_interpretation:'文字与图片推断',official_text_and_usage_guidance:'官方文字与使用说明'};
function sourceLink(url,label){const link=document.createElement('a');link.textContent=label;if(url?.startsWith('https://docs.factoryio.com/')){link.href=url;link.target='_blank';link.rel='noopener noreferrer';}return link;}
function referenceCard(card){const box=document.createElement('section');box.className='knowledge-card';const title=document.createElement('strong');title.textContent=card.official_name;const names=document.createElement('small');names.textContent=(card.prefabs||[]).join(' · ');const text=document.createElement('p');text.textContent=card.function;box.append(title,names,text);
  if(card.appearance){const appearance=document.createElement('p');appearance.textContent=(bases[card.appearance.basis]||card.appearance.basis)+'：'+card.appearance.text;box.append(appearance);}
  for(const marker of card.markers||[]){const row=document.createElement('p');row.className='muted';row.textContent=(bases[marker.basis]||marker.basis)+'：'+marker.text;box.append(row);}
  for(const source of card.sources||[])box.append(sourceLink(source.url,'官方来源 ↗'));
  return box;
}
function renderReferences(container,reference){container.replaceChildren();if(!reference){const empty=document.createElement('p');empty.className='muted';empty.textContent='本步没有组件检索记录。历史请求保留当时发送的原始内容。';container.append(empty);return;}
  const summary=document.createElement('p');summary.className='muted';summary.textContent=reference.requested_prefabs.length+' 种组件类型 / '+reference.components.length+' 份参考资料';container.append(summary);
  for(const card of reference.components)container.append(referenceCard(card));
  if(reference.missing_prefabs?.length){const missing=document.createElement('p');missing.className='error-box';missing.textContent='暂无明确官方映射：'+reference.missing_prefabs.join('、');container.append(missing);}
}
async function searchKnowledge(){try{const data=await api('/api/knowledge?q='+encodeURIComponent($('knowledge-query').value));$('knowledge-summary').textContent=data.stats.source_count+' 个官方页面 · '+data.stats.chapter_count+' 个章节 · '+data.stats.prefab_count+' 种已映射组件';$('knowledge-results').replaceChildren();
  if(!data.results.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='没有匹配结果，请使用组件名称或较短的关键词。';$('knowledge-results').append(empty);}
  for(const row of data.results){const section=document.createElement('section');section.className='knowledge-result';section.append(sourceLink(row.url,row.title+' ↗'));for(const card of row.components)section.append(referenceCard(card));const detail=document.createElement('details');const label=document.createElement('summary');label.textContent='官方章节摘录';const excerpt=document.createElement('pre');excerpt.textContent=row.excerpt;detail.append(label,excerpt);section.append(detail);$('knowledge-results').append(section);}}
  catch(error){toast(error.message);}
}
async function createTask(body){const data=await api('/api/sessions',body);await refresh();await selectSession(data.id);}
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>showTab(b.dataset.tab)));
document.querySelectorAll('.copy').forEach(b=>b.addEventListener('click',async event=>{event.preventDefault();event.stopPropagation();await navigator.clipboard.writeText($(b.dataset.copy).textContent);toast('已复制');}));
for(const id of ['new-task','welcome-new'])$(id).onclick=()=>$('new-dialog').showModal();$('close-dialog').onclick=()=>$('new-dialog').close();$('close-image').onclick=()=>$('image-dialog').close();$('refresh').onclick=refresh;$('history-search').oninput=()=>bootstrap&&renderLists();
$('new-form').onsubmit=async event=>{event.preventDefault();if(busy)return;busy=true;try{await createTask({mode:$('mode').value,prompt:$('prompt').value});$('new-dialog').close();}catch(error){toast(error.message);}finally{busy=false;}};
async function releaseStep(id){
  const session=window.sessionState,sessionId=currentSession;
  if(busy||!id||!session||session.historical||session.pending_id!==id)return;
  busy=true;$('approve').disabled=true;renderContinuation(session);
  currentStep=id;currentTab='response';lastDetailKey='';
  try{await api('/api/sessions/'+sessionId+'/approve',{step_id:id});if(currentSession===sessionId)await poll();}
  catch(error){toast(error.message);}
  finally{busy=false;$('approve').disabled=false;if(window.sessionState)renderContinuation(window.sessionState);}
}
$('approve').onclick=()=>releaseStep(currentStep);
$('continue-next').onclick=()=>{if(!$('continue-next').disabled)return releaseStep($('continue-next').dataset.stepId);};
$('new-full-workflow').onclick=()=>{$('mode').value='run';$('new-dialog').showModal();};
$('recover-workflow').onclick=async()=>{if(busy||$('recover-workflow').hidden)return;busy=true;const source=currentSession;renderContinuation(window.sessionState);try{await createTask({mode:'resume',source_id:source});}catch(error){toast(error.message);}finally{busy=false;if(window.sessionState)renderContinuation(window.sessionState);}};
$('view-pending').onclick=async()=>{currentStep=window.sessionState.pending_id;currentTab='request';lastDetailKey='';await poll();};
$('cancel').onclick=async()=>{try{await api('/api/sessions/'+currentSession+'/cancel',{});await poll();}catch(error){toast(error.message);}};
$('replay-vision').onclick=async()=>{try{await createTask({mode:'vision',source_id:currentSession});}catch(error){toast(error.message);}};
$('resume').onclick=async()=>{try{await createTask({mode:'resume',source_id:currentSession});}catch(error){toast(error.message);}};
$('open-knowledge').onclick=async()=>{$('knowledge-dialog').showModal();await searchKnowledge();};
$('close-knowledge').onclick=()=>$('knowledge-dialog').close();
$('knowledge-form').onsubmit=async event=>{event.preventDefault();await searchKnowledge();};
(async()=>{await refresh();const selected=new URLSearchParams(location.search).get('session');if(selected)await selectSession(selected);setInterval(poll,1500);setInterval(refresh,15000);})();
