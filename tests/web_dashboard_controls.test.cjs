const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

async function dashboard(session){
  const elements=new Map(), requests=[];
  function element(){return {hidden:false,disabled:false,textContent:'',value:'',dataset:{},
    classList:{toggle(){}},addEventListener(){},append(){},replaceChildren(){},
    showModal(){this.open=true;},close(){this.open=false;}};}
  const document={getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);},
    querySelector(){return {content:'fixture-token'};},querySelectorAll(){return [];},createElement:element,
    createTextNode(text){return text;}};
  const context=vm.createContext({document,window:{},Intl,URLSearchParams,
    location:{search:'?session=fixture'},history:{replaceState(){}},
    navigator:{clipboard:{writeText:async()=>{}}},setInterval(){},setTimeout(){},clearTimeout(){},
    fetch:async(url,options={})=>{
      requests.push({url,...options});
      let data=url==='/api/bootstrap'?{sessions:[],history:[],models:{}}:session;
      if(url.includes('/steps/'))data={...session.steps.find(s=>url.endsWith(s.id)),
        request:{system:'Read request',payload:{},images:[],tool:'codesys_services'},response:{summary:'Done'}};
      return {ok:true,json:async()=>data};
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../factoryio_agents/webui/app.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {context,requests,get:id=>document.getElementById(id)};
}

function state(values={}){return {id:'fixture',name:'Fixture',output:'fixture',mode:'run',historical:false,
  status:'waiting',revision:1,events:[],result:null,pending_id:'next',
  steps:[{id:'old',number:1,kind:'model',role:'planner',phase:'spec',status:'completed'},
    {id:'next',number:2,kind:'model',role:'scene',phase:'layout_proposal',status:'waiting'}],...values};}

test('next-step control stays visible while viewing the previous reply and releases only the pending step',async()=>{
  const ui=await dashboard(state());
  vm.runInContext("currentStep='old'; renderContinuation(window.sessionState);",ui.context);
  assert.equal(ui.get('pending-banner').hidden,false);
  assert.equal(ui.get('continue-next').disabled,false);
  assert.match(ui.get('pending-label').textContent,/第 2 步/);
  await ui.get('continue-next').onclick();
  const approvals=ui.requests.filter(r=>r.url.endsWith('/approve'));
  assert.equal(approvals.length,1);
  assert.equal(JSON.parse(approvals[0].body).step_id,'next');
});

test('finished vision replay explains there is no next step and offers a full workflow without sending requests',async()=>{
  const ui=await dashboard(state({mode:'vision',status:'finished',pending_id:null,
    steps:[{id:'old',number:1,kind:'model',role:'vision',phase:'scene',status:'completed'}],
    result:{status:'archived_review_completed'}}));
  assert.equal(ui.get('pending-banner').hidden,false);
  assert.equal(ui.get('continue-next').disabled,true);
  assert.match(ui.get('continuation-hint').textContent,/只进行视觉检查/);
  assert.equal(ui.get('new-full-workflow').hidden,false);
  await ui.get('new-full-workflow').onclick();
  assert.equal(ui.get('mode').value,'run');
  assert.equal(ui.get('new-dialog').open,true);
  assert.equal(ui.requests.filter(r=>r.method==='POST').length,0);
});

test('running or cancelled work cannot be released by the persistent control',async()=>{
  for(const status of ['running','cancelled']){
    const ui=await dashboard(state({status,pending_id:null,steps:[]}));
    assert.equal(ui.get('pending-banner').hidden,false);
    assert.equal(ui.get('continue-next').disabled,true);
    await ui.get('continue-next').onclick();
    assert.equal(ui.requests.filter(r=>r.method==='POST').length,0);
  }
});

function serviceFailure(){return state({status:'finished',pending_id:null,
  steps:[{id:'preflight',number:1,kind:'tool',role:'tools',phase:'environment_preflight',status:'released',
    created_at:'2026-10-12T11:40:10Z'}],
  events:[{event:'tool_requested',tool:'codesys_services',phase:'environment_preflight',timestamp:'2026-10-12T11:40:10Z'},
    {event:'tool_completed',tool:'codesys_services',phase:'environment_preflight',ready:false,
      receipt:'environment-preflight.json',timestamp:'2026-10-12T11:40:15Z'},
    {event:'workflow_transition',phase:'environment_preflight',error:'CODESYS local services are unavailable',
      timestamp:'2026-10-12T11:40:16Z',diagnostics:{services:[{service:'CODESYS Control Win V3 - x64',
        state:'Stopped',status:'error',service_running:false,windows_error:5,error:'[WinError 5] 拒绝访问。'}]}}],
  result:{status:'needs_configuration',phase:'environment_preflight',error:'CODESYS local services are unavailable',
    diagnostics:{services:[{service:'CODESYS Control Win V3 - x64',state:'Stopped',status:'error',
      service_running:false,windows_error:5,error:'[WinError 5] 拒绝访问。'}]}}});}

test('failed environment check explains the blocker and resumes the same requirement instead of creating a fresh task',async()=>{
  const ui=await dashboard(serviceFailure());
  assert.match(ui.get('continuation-title').textContent,/环境检查失败/);
  assert.match(ui.get('continuation-hint').textContent,/CODESYS Control Win/);
  assert.match(ui.get('continuation-hint').textContent,/拒绝|权限/);
  assert.equal(ui.get('recover-workflow').hidden,false);
  assert.equal(ui.get('recover-workflow').disabled,false);
  await ui.get('recover-workflow').onclick();
  const creates=ui.requests.filter(r=>r.url==='/api/sessions'&&r.method==='POST');
  assert.equal(creates.length,1);
  assert.deepEqual(JSON.parse(creates[0].body),{mode:'resume',source_id:'fixture'});
  assert.equal(ui.requests.filter(r=>r.url.endsWith('/approve')).length,0);
});

test('the tool reply shows its actual failed outcome rather than just permission to execute',async()=>{
  const ui=await dashboard(serviceFailure());
  assert.equal(ui.get('step-status').textContent,'失败');
  assert.match(ui.get('readable-response').textContent,/ready/);
  assert.match(ui.get('readable-response').textContent,/false/);
  assert.match(ui.get('readable-response').textContent,/拒绝访问/);
  assert.equal(ui.get('step-error').hidden,false);
});

test('an executing tool must not borrow the result of another call to the same tool',async()=>{
  const session=serviceFailure();session.status='running';session.result=null;
  session.steps.push({id:'retry',number:2,kind:'tool',role:'tools',phase:'environment_preflight',status:'released',
    created_at:'2026-10-12T11:41:10Z'});
  session.events.push({event:'tool_requested',tool:'codesys_services',phase:'environment_preflight',timestamp:'2026-10-12T11:41:10Z'});
  const ui=await dashboard(session);
  const outcome=vm.runInContext("toolStep({id:'retry',kind:'tool',phase:'environment_preflight',status:'released',created_at:'2026-10-12T11:41:10Z',request:{tool:'codesys_services'}},window.sessionState)",ui.context);
  assert.equal(outcome.status,'released');
  assert.equal(outcome.error,undefined);
  assert.equal(ui.get('recover-workflow').hidden,true);
});

test('loaded scene with missing geometry names the affected objects without claiming a visual pass',async()=>{
  const session=serviceFailure();session.result={status:'needs_attention',phase:'scene_geometry',error:'Native geometry missing',
    diagnostics:{scope:'missing_conveyor_geometry',spatial_checks_applied:false,issues:[
      {object_id:'conveyor',prefab:'BeltConveyor4M',error:'native conveyor component geometry missing'},
      {object_id:'emitter',error:'native voxel registration failed: overlap or out-of-world placement'}]}};
  const ui=await dashboard(session);
  assert.match(ui.get('continuation-title').textContent,/场景已加载，几何检查未通过/);
  assert.match(ui.get('continuation-hint').textContent,/BeltConveyor4M/);
  assert.match(ui.get('continuation-hint').textContent,/emitter/);
  assert.match(ui.get('continuation-hint').textContent,/后续步骤尚未执行/);
  assert.equal(ui.get('continue-next').disabled,true);
  assert.equal(ui.get('recover-workflow').hidden,false);
});

test('resume logs accept text and structured summaries without interrupting the next-step preview',async()=>{
  for(const summary of ['Recovered task',{reusable:['task','layout']}]){
    const ui=await dashboard(state({events:[{event:'resume_restored',summary,timestamp:'2026-10-12T12:41:10Z'}]}));
    assert.equal(vm.runInContext('currentStep',ui.context),'next');
    assert.match(ui.get('step-title').textContent,/第 2 步/);
    assert.equal(ui.get('toast').textContent,'');
  }
});
