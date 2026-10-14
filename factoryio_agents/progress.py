"""Public model activity and readable logs; never render program/XML or private reasoning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .training import Redactor, now


ROLES = {'planner':'Agent1 规划', 'scene':'Agent2 场景', 'vision':'Agent3 视觉',
         'plc':'PLC Agent', 'supervisor':'监督 Agent', 'tools':'工具'}
STAGES = {'spec':'需求拆解与部件选择', 'execution':'供料与控制流程',
    'layout_proposal':'提出物品功能与相对布局', 'layout_review':'规划 Agent 审核布局提案',
    'measurement_signals':'定义观测信号', 'measurement_probes':'设计测试操作',
    'measurement_checks':'设计验收规则', 'acceptance':'规划验收',
    'layout_constraints':'选择模板与相对布局', 'layout':'安排对象坐标',
    'configuration_io':'配置部件与 I/O', 'scene':'检查场景画面', 'runtime':'检查运行流程',
    'runtime_investigation':'调查运行失败', 'planning':'规划需求',
    'scene_review':'检查场景画面', 'runtime_review':'检查运行流程', 'plc_generation':'生成 PLC 控制逻辑',
    'scene_generation':'生成场景', 'compile':'编译并部署 PLC', 'connection':'连接 Modbus',
    'environment_preflight':'检查软件环境', 'deployment_preflight':'检查部署环境',
    'live_handoff':'复位、重启并检查交付运行', 'runtime_control':'控制两端运行',
    'initialization':'初始化', 'native_geometry':'读取部件几何', 'decision':'决定工作流下一步',
    'connectivity_probe':'日志与流式通讯验证'}
PUBLIC_FIELDS = {'purpose','description','requirement','control_rules','assumptions','unresolved',
    'scene_goal','function','relation','requirements','relative_to','summary',
    'notes','observed','expected','suggestion','reason','verdict','decision','next_role',
    'hypothesis','discriminating_check','expected_result','retest','status','object_ids','related_object_ids','evidence_images'}
PUBLIC_FIELDS.update({'observation','spatial_observation','repair_intent','suspect_object_ids','inspection_complete'})
BLOCKED_FIELDS = {'declaration','implementation','globals','xml','code','script','scripts',
    'raw_content','response_text','reasoning_content','credentials','password','api_key'}
FIELD_LABELS = {'purpose':'用途', 'description':'工艺步骤', 'requirement':'需求',
    'scene_goal':'场景目标','function':'物品功能','relation':'预期相对位置','requirements':'布局要求',
    'relative_to':'参照物品','summary':'审核说明',
    'control_rules':'控制规则', 'assumptions':'假设', 'unresolved':'未决项', 'notes':'说明',
    'observed':'观察', 'expected':'预期', 'suggestion':'修复建议', 'reason':'判断依据',
    'verdict':'模型结论', 'decision':'建议动作', 'next_role':'下一角色',
    'hypothesis':'原因假设', 'discriminating_check':'区分原因的检查',
    'expected_result':'检查预期', 'retest':'复测要求','status':'审查状态','object_ids':'检查对象',
    'related_object_ids':'关联对象','evidence_images':'图片证据'}
FIELD_LABELS.update(observation='图像观察',spatial_observation='测量辅助说明',repair_intent='修复意图',suspect_object_ids='疑似问题对象',inspection_complete='检查完成')


def safe_text(value, limit=1100):
    """A second display boundary after field allowlisting and credential redaction."""
    if not isinstance(value, (str, int, float, bool)): return ''
    text = str(value)
    text = re.sub(r'```[\s\S]*?```', '[代码片段已省略]', text)
    if re.search(r'(?i)\b(?:PROGRAM|VAR_GLOBAL|FUNCTION_BLOCK)\b.*\b(?:END_VAR|END_PROGRAM|END_FUNCTION_BLOCK)\b', text, re.S):
        return '[代码内容已省略，见对应产物文件]'
    text = re.sub(r'<[^>]+>', '[XML/标记片段已省略]', text)
    text = re.sub(r'`[^`\n]*:=[^`\n]*`', '[代码赋值已省略]', text)
    text = re.sub(r'\b[A-Za-z_]\w*(?:\.\w+)*\s*:=\s*(?:T#[\w.]+|[+-]?\d+(?:\.\d+)?|[A-Za-z_]\w*(?:\.\w+)*)(?:[ \t]*;)?',
                  '[代码赋值已省略]', text)
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', ' ', text).replace('\t',' ')
    return text[:limit] + (' …（完整文字见候选文件）' if len(text)>limit else '')


def substage(payload, role):
    return (payload.get('planning_stage') or payload.get('scene_stage') or
            (payload.get('handoff') or {}).get('phase') or payload.get('phase') or
            ('plc_generation' if role=='plc' else 'decision' if role=='supervisor' else role))


class PublicJSONStream:
    """Incrementally expose only completed, allowlisted JSON string values.

    This is a display lexer, not a contract validator. Complete JSON is still parsed
    and validated by the original model/agent code. Code strings are not retained here.
    """
    def __init__(self, emit, max_fields=24):
        self.emit, self.max_fields = emit, max_fields
        self.stack = []; self.in_string = self.escape = self.primitive = False
        self.token = []; self.string_key = False; self.string_path = (); self.collect = False
        self.fields = 0

    def path(self):
        if not self.stack: return ()
        node = self.stack[-1]
        return node['path'] + ((node.get('key'),) if node['kind']=='object' else (node['index'],))

    def done(self):
        if self.stack:
            node = self.stack[-1]; node['mode'] = 'after'
            if node['kind']=='array': node['index'] += 1

    def public(self, path):
        names = [p for p in path if isinstance(p,str)]
        return bool(names and names[-1] in PUBLIC_FIELDS and not set(names) & BLOCKED_FIELDS)

    def feed(self, fragment):
        for char in fragment:
            if self.in_string:
                if self.collect: self.token.append(char)
                if self.escape: self.escape=False; continue
                if char=='\\': self.escape=True; continue
                if char!='"': continue
                self.in_string=False
                try: value=json.loads(''.join(self.token)) if self.collect else None
                except ValueError: value=None
                if self.string_key and self.stack:
                    self.stack[-1]['key']=value; self.stack[-1]['mode']='colon'
                else:
                    if value is not None and self.public(self.string_path) and self.fields<self.max_fields:
                        self.fields+=1; self.emit(self.string_path,value)
                    self.done()
                self.token=[]; continue
            if self.primitive:
                if not (char.isspace() or char in ',}]'): continue
                self.primitive=False; self.done()
            if char.isspace(): continue
            if char=='"':
                self.string_key=bool(self.stack and self.stack[-1]['kind']=='object' and self.stack[-1]['mode']=='key')
                self.string_path=self.path()
                self.collect=self.string_key or (self.public(self.string_path) and self.fields<self.max_fields)
                self.token=['"'] if self.collect else []; self.in_string=True
            elif char in '{[':
                self.stack.append({'kind':'object' if char=='{' else 'array','path':self.path(),
                    'mode':'key' if char=='{' else 'value','key':None,'index':0})
            elif char in '}]':
                if self.stack: self.stack.pop(); self.done()
            elif char==':' and self.stack: self.stack[-1]['mode']='value'
            elif char==',' and self.stack:
                self.stack[-1]['mode']='key' if self.stack[-1]['kind']=='object' else 'value'
            else: self.primitive=True


def response_summary(role, value, payload):
    """Read business fields from the original reply without changing its artifact."""
    lines=[]
    def add(label, text):
        cleaned=safe_text(text)
        if cleaned: lines.append(label+'：'+cleaned)
    def strings(label, values):
        if isinstance(values,str): add(label,values)
        elif isinstance(values,list):
            for item in values[:12]:
                if isinstance(item,str): add(label,item)
            if len(values)>12: add(label,f'另有 {len(values)-12} 项，见候选文件')
    if not isinstance(value,dict): return ['回复不是 JSON 对象，等待合同检查报告']
    if type(value.get('inspection_complete')) is bool:add('检查完成','是' if value['inspection_complete'] else '否')
    add('场景类型',value.get('scenario_type')); add('需求',value.get('requirement'))
    add('场景目标',value.get('scene_goal')); add('审核说明',value.get('summary'))
    objects=value.get('objects')
    if isinstance(objects,list):
        add('对象数',len(objects))
        lookup={o.get('id'):o.get('prefab') for o in (payload.get('task') or {}).get('objects',[])}
        for obj in objects[:20]:
            if not isinstance(obj,dict): continue
            parts=[safe_text(obj.get('type') or obj.get('prefab') or lookup.get(obj.get('id')) or obj.get('template_id'),130)]
            if obj.get('purpose'): parts.append(safe_text(obj['purpose'],450))
            if obj.get('function'): parts.append(safe_text(obj['function'],450))
            pos=obj.get('position',obj.get('position_grid'))
            if isinstance(pos,list) and all(type(n) in (int,float) for n in pos): parts.append('网格位置 '+str(pos))
            if type(obj.get('yaw_degrees')) in (int,float): parts.append('朝向 '+str(obj['yaw_degrees'])+'°')
            if isinstance(obj.get('configuration'),dict): parts.append('配置项 '+str(len(obj['configuration'])))
            add('对象 '+safe_text(obj.get('id'),100),'；'.join(p for p in parts if p))
            position=obj.get('expected_position')
            if isinstance(position,dict):
                strings('参照物品',position.get('relative_to'))
                add('预期相对位置',position.get('relation')); strings('布局要求',position.get('requirements'))
        if len(objects)>20: add('对象',f'另有 {len(objects)-20} 个，见候选文件')
    for step in value.get('process',[]) if isinstance(value.get('process'),list) else []:
        if isinstance(step,dict): add('流程 '+safe_text(step.get('id'),100),step.get('description') or step.get('action'))
    strings('控制规则',value.get('control_rules')); strings('假设',value.get('assumptions'))
    contract=value.get('execution_contract')
    if isinstance(contract,dict):
        add('物料跟踪',contract.get('tracking_mode')); add('最大在途量',contract.get('max_in_flight'))
    if isinstance(value.get('signals'),dict):
        add('观测信号数',len(value['signals']))
        for name,signal in list(value['signals'].items())[:16]:
            if isinstance(signal,dict): add('信号 '+safe_text(name,100),
                '对象 '+safe_text(signal.get('object_id'),150)+'；语义 '+safe_text(signal.get('meaning'),150))
    for field,label in [('checks','验收规则'),('probes','测试操作'),('verification_tasks','验证待办'),('mappings','I/O 映射')]:
        if isinstance(value.get(field),list):
            if field=='checks' and role=='vision': label='布局检查项'
            add(label+'数',len(value[field]))
            for row in value[field][:12]:
                if isinstance(row,dict):
                    add(label,row.get('description') or row.get('id') or row.get('kind'))
                    if field=='probes' and isinstance(row.get('actions'),list):
                        for action in row['actions'][:12]:
                            if not isinstance(action,dict): continue
                            state='按下' if action.get('pressed') is True else '释放' if action.get('pressed') is False else '状态未指定'
                            add('测试动作 '+safe_text(row.get('id'),100)+'.'+safe_text(action.get('label'),100),
                                safe_text(action.get('object_id'),100)+' '+state+'，保持 '+safe_text(action.get('hold_seconds'),80)+' 秒')
    if role=='plc':
        add('PLC 程序',value.get('program_name') or '控制逻辑片段')
        size=sum(len(value.get(k,'')) for k in ('declaration','implementation') if isinstance(value.get(k),str))
        if size: add('代码产物',f'已返回 {size} 个字符；正文保存在候选与 ST 文件中，待合同检查和编译')
    add('模型结论',value.get('verdict')); add('建议动作',value.get('decision')); add('下一角色',value.get('next_role'))
    add('判断依据',value.get('reason'))
    for check in value.get('checks',[]) if isinstance(value.get('checks'),list) else []:
        if not isinstance(check,dict): continue
        strings('检查对象',check.get('object_ids')); strings('关联对象',check.get('related_object_ids'))
        add('审查状态',check.get('status'))
        for field in ('observed','expected','reason','suggestion'): add(FIELD_LABELS[field],check.get(field))
        strings('图片证据',check.get('evidence_images'))
    if isinstance(value.get('issues'),list):
        add('异常项数',len(value['issues']))
        for issue in value['issues']:
            strings('疑似问题对象',issue.get('suspect_object_ids'));add('审查状态',issue.get('status'))
            for field in ('observation','spatial_observation','reason','repair_intent'):add(FIELD_LABELS[field],issue.get(field))
    for finding in value.get('findings',[]) if isinstance(value.get('findings'),list) else []:
        if not isinstance(finding,dict): continue
        add('问题目标',finding.get('target'))
        strings('问题物品',finding.get('object_ids'))
        for field in ('observed','expected','reason','suggestion'): add(FIELD_LABELS[field],finding.get(field))
        diagnosis=finding.get('diagnosis') or {}
        if isinstance(diagnosis,dict):
            add('因果证据状态',diagnosis.get('causal_status'))
            for field in ('hypothesis','discriminating_check','expected_result'): add(FIELD_LABELS[field],diagnosis.get(field))
        if isinstance(finding.get('evidence_ids'),list): add('证据引用',', '.join(safe_text(x,100) for x in finding['evidence_ids'][:12]))
    strings('模型说明',value.get('notes')); strings('未决项',value.get('unresolved'))
    return lines[:60] + (['摘要达到显示上限；其余业务字段见候选文件'] if len(lines)>60 else [])


class ModelObserver:
    def __init__(self, output, callback=None, secrets=(), heartbeat_seconds=15):
        self.output=Path(output); self.callback=callback; self.redactor=Redactor(secrets)
        self.interval=heartbeat_seconds; self.lock=threading.RLock(); self.count=0; self.last_context={}
        existing=self.output/'model-events.jsonl'
        if existing.is_file():
            with existing.open(encoding='utf-8') as stream:
                self.count=sum(json.loads(line).get('event')=='model_started' for line in stream if line.strip())

    def emit(self, event, context, **details):
        with self.lock:
            item=self.redactor.clean({'timestamp':now(),'event':event,**context,**details,'channel':'model'})
            self.output.mkdir(parents=True,exist_ok=True)
            with (self.output/'model-events.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(item,ensure_ascii=False,allow_nan=False)+'\n')
            if self.callback: self.callback(item)

    def training_event(self, item):
        event=item['event']; details=item['details']
        if event=='component_knowledge_retrieved':
            self.emit(event,{**self.last_context,'active_role':details['role']},
                text='官方知识检索：'+str(len(details['requested_prefabs']))+' 种组件，'+
                    str(len(details['component_ids']))+' 份参考资料；缺失映射 '+str(len(details['missing_prefabs']))+' 种。',
                knowledge_sha256=details['knowledge_sha256'])
        elif event.endswith(('_contract_rejected','_substage_rejected')):
            error=details.get('error') or (details.get('feedback') or {}).get('error') or '合同未通过'
            self.emit('model_validation_rejected',self.last_context,error=safe_text(error),
                      stage=details.get('stage'),candidate_id=item.get('candidate_id'))
        elif event.endswith(('_substage_accepted','_substage_reused')):
            self.emit('model_validation_accepted' if event.endswith('_accepted') else 'model_artifact_reused',
                {**self.last_context,'active_role':event.split('_')[0]},stage=details.get('stage'),candidate_id=item.get('candidate_id'))
        elif event in ('layout_solver_fallback','transport_clearance_resolved','relative_layout_resolved'):
            self.emit('model_tool_adjustment',{**self.last_context,'active_role':'scene'},
                description={'layout_solver_fallback':'当前部件不支持相对求解，改用绝对坐标设计',
                    'transport_clearance_resolved':'工具依据部件边界调整安装间隙，仍需物理验收',
                    'relative_layout_resolved':'工具已解算相对布局，仍需加载和物理验收'}[event])


class ObservedClient:
    def __init__(self, client, observer, recorder=None):
        self.client,self.observer,self.recorder=client,observer,recorder

    def exchange_metadata(self):
        method=getattr(type(self.client),'exchange_metadata',None)
        return method(self.client) if callable(method) else {}

    def complete(self, role, system, payload, images=None):
        observer=self.observer; started=time.monotonic(); stopped=threading.Event()
        with observer.lock:
            observer.count+=1
            context={'active_role':role,'phase':substage(payload,role), 'call_id':uuid.uuid4().hex,'call_number':observer.count}
            if self.recorder and self.recorder.last_candidate_id:
                context['candidate_id']=self.recorder.last_candidate_id
                context['candidate_path']=str(self.recorder.directory/'candidates'/(self.recorder.last_candidate_id+'.json'))
            observer.last_context=context
        activity={'transport_stage':'waiting_for_provider','content_chars':0,'packets':0}
        observer.emit('model_started',context,requirement=safe_text(payload.get('requirement') or
            (payload.get('task') or payload.get('task_spec') or {}).get('requirement') or
            payload.get('layout_review',{}).get('scene_goal')),
            image_count=len(images or []), retry_error=safe_text((payload.get('format_feedback') or
                payload.get('stage_rejection') or payload.get('feedback') or {}).get('error')))
        def transport(event):
            with observer.lock:
                activity.update({k:v for k,v in event.items() if k in ('transport_stage','content_chars','packets')})
            if event['event']=='public_field':
                observer.emit('model_public_field',context,label=event['label'],text=safe_text(event['text']),provisional=True)
            elif event['event'] in ('request_prepared','response_connected'):
                observer.emit('model_transport',context,**{k:v for k,v in event.items() if k!='event'})
        setter=getattr(type(self.client),'set_progress_callback',None)
        old_callback=setter(self.client,transport) if callable(setter) else None
        def heartbeat():
            while not stopped.wait(observer.interval):
                with observer.lock: snapshot=dict(activity)
                observer.emit('model_waiting',context,elapsed_seconds=round(time.monotonic()-started,1),**snapshot)
        worker=threading.Thread(target=heartbeat,name='model-progress-heartbeat',daemon=True); worker.start()
        try:
            value=self.client.complete(role,system,payload,images) if images else self.client.complete(role,system,payload)
        except BaseException as error:
            stopped.set(); worker.join(timeout=1)
            observer.emit('model_failed',context,elapsed_seconds=round(time.monotonic()-started,1),
                error_type=type(error).__name__,error=safe_text(str(error)))
            raise
        else:
            stopped.set(); worker.join(timeout=1)
            metadata=self.exchange_metadata()
            observer.emit('model_returned',context,elapsed_seconds=round(time.monotonic()-started,1),
                model=metadata.get('returned_model') or metadata.get('requested_model'),usage=metadata.get('usage'),
                response_mode=metadata.get('response_mode'),provisional=True,
                summary=response_summary(role,observer.redactor.clean(value),payload))
            return value
        finally:
            stopped.set()
            if callable(setter): setter(self.client,old_callback)


class ConsoleProgress:
    """One readable UTF-8 log for tool events and public model activity, including quiet runs."""
    def __init__(self, output, quiet=False, secrets=()):
        self.output=Path(output) if output is not None else None
        self.quiet=quiet; self.redactor=Redactor(secrets); self.lock=threading.RLock()

    def __call__(self,event):
        event=self.redactor.clean(event)
        stamp=datetime.fromisoformat(event['timestamp']).astimezone(timezone(timedelta(hours=8))).strftime('%H:%M:%S')
        role=ROLES.get(event.get('active_role'),event.get('active_role','框架'))
        stage=event.get('stage') or event.get('phase',''); label=STAGES.get(stage,stage)
        kind=event['event']; lines=[]
        header=f'[{stamp}] {role} / {label}'
        if kind=='model_started':
            lines=[header+f' · 模型请求 #{event["call_number"]} 开始']
            if event.get('requirement'): lines.append('  目标：'+safe_text(event['requirement']))
            if event.get('image_count'): lines.append('  本次使用图片：'+str(event['image_count'])+' 张')
            if event.get('retry_error'): lines.append('  本次修正原因：'+safe_text(event['retry_error']))
        elif kind=='model_transport':
            if event.get('model'): lines=[header+' · 服务 '+safe_text(event.get('provider') or '')+' / '+safe_text(event['model'])+
                ('，流式接收' if event.get('streaming') else '，等待完整回复')]
            elif event.get('transport_stage')=='response_connected': lines=[header+' · 服务已响应，正在接收']
        elif kind=='model_waiting':
            state={'waiting_for_provider':'等待服务响应','response_connected':'等待模型输出',
                'provider_processing':'服务正在处理','receiving_output':'正在接收模型输出'}.get(event.get('transport_stage'),'等待模型回复')
            lines=[header+f' · {state}，已等待 {event["elapsed_seconds"]} 秒，收到正文 {event.get("content_chars",0)} 字符']
        elif kind=='model_public_field':
            lines=[header+' · 输出中（尚待验证）', '  '+safe_text(event['label'],120)+'：'+safe_text(event['text'])]
        elif kind=='model_returned':
            lines=[header+f' · 模型已返回，用时 {event["elapsed_seconds"]} 秒（尚待合同检查/工具验证）']
            lines+=['  '+safe_text(line,1500) for line in event.get('summary',[])]
            usage=event.get('usage')
            if isinstance(usage,dict): lines.append('  实际 tokens：输入 '+str(usage.get('prompt_tokens','未知'))+
                '，输出 '+str(usage.get('completion_tokens','未知'))+'，合计 '+str(usage.get('total_tokens','未知')))
            if event.get('candidate_path'): lines.append('  完整回复文件：'+safe_text(event['candidate_path'],500))
        elif kind in ('model_failed','model_validation_rejected'):
            lines=[header+' · '+('模型调用失败' if kind=='model_failed' else '合同检查未通过'), '  原因：'+safe_text(event.get('error'))]
        elif kind=='model_validation_accepted': lines=[header+' · 当前子任务合同已通过']
        elif kind=='model_artifact_reused': lines=[header+' · 复用已有模型产物，本子任务未重新请求模型']
        elif kind=='model_tool_adjustment': lines=[header+' · '+safe_text(event.get('description'))]
        elif kind=='model_not_finished': lines=[header+' · 此候选尚无完整回复；当前任务状态以 run.json 为准']
        elif kind=='supervisor_rule_decision':
            lines=[header+' · 规则路由：'+safe_text(event.get('decision'))+' → '+safe_text(event.get('next_role')),
                   '  '+safe_text(event.get('reason'))]
        else:
            status=event.get('status',kind)
            lines=[header+' · '+str(status)]
            if event.get('error'): lines.append('  原因：'+safe_text(event['error']))
            if event.get('expected_output'): lines.append('  预期产物：'+safe_text(event['expected_output'],500))
            if event.get('tool'): lines.append('  工具：'+safe_text(event['tool'],200)+'；操作：'+safe_text(event.get('action'),200))
        text='\n'.join(lines)+'\n'
        with self.lock:
            if self.output is not None:
                self.output.mkdir(parents=True,exist_ok=True)
                with (self.output/'progress.log').open('a',encoding='utf-8') as stream: stream.write(text)
            if not self.quiet: print(text,end='',flush=True)


def replay_models(output, console):
    """Read only this attempt's archived candidates, including separately recorded supervisor calls."""
    from .contracts import read_json, require
    output=Path(output).resolve()
    require(output.is_dir(), '运行目录不存在')
    state=read_json(output/'run.json') if (output/'run.json').is_file() else {}
    created=state.get('created_at')
    records=[read_json(p) for p in (output/'training-data/candidates').glob('*.json')]
    if created:
        threshold=datetime.fromisoformat(created)
        records=[r for r in records if r.get('started_at') and datetime.fromisoformat(r['started_at'])>=threshold]
    records.sort(key=lambda r:(r.get('started_at',''),r.get('candidate_id','')))
    print('历史模型记录回放：只读展示已保存的业务输出，不调用模型或操作软件。',flush=True)
    for index,record in enumerate(records,1):
        payload=record.get('payload') or {}; role=record['role']; metadata=record.get('model_metadata') or {}
        context={'active_role':role,'phase':substage(payload,role),'call_number':index,
            'candidate_path':str(output/'training-data/candidates'/(record['candidate_id']+'.json'))}
        console({'timestamp':record['started_at'],'event':'model_started',**context,
            'requirement':safe_text(payload.get('requirement') or (payload.get('task') or {}).get('requirement')),
            'image_count':len(record.get('images',[]))})
        if metadata.get('requested_model'):
            console({'timestamp':record['started_at'],'event':'model_transport',**context,
                'model':metadata['requested_model'],'provider':metadata.get('provider'),'streaming':metadata.get('streaming_requested',False)})
        if record.get('error'):
            console({'timestamp':record.get('finished_at',record['started_at']),'event':'model_failed',**context,
                     'error':safe_text(record['error'].get('message'))})
        elif record.get('structured_output') is not None:
            console({'timestamp':record.get('finished_at',record['started_at']),'event':'model_returned',**context,
                'elapsed_seconds':record.get('latency_seconds','未知'),'usage':metadata.get('usage'),
                'summary':response_summary(role,record['structured_output'],payload)})
        else: console({'timestamp':record['started_at'],'event':'model_not_finished',**context})
    print(f'回放完成：本轮候选 {len(records)} 条；历史复用候选未计入。',flush=True)
    return len(records)
