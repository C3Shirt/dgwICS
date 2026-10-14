"""Execute task-provided operator steps and collect evidence without process assumptions."""
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
import math
import time

from .contracts import file_hash, read_json, require, write_json
from .runtime import FactoryIOClient, collect_evidence, reconcile_tags, run_hook, utc_now
from .synchronized_runtime import SynchronizedRuntime


def runtime_signals(design, scaffold):
    objects={o['id']:o for o in design['objects']}
    signals={}
    for signal in scaffold['signals']:
        key=signal['tag_key'];require(key not in signals,'duplicate runtime tag identity')
        signals[key]={**deepcopy(signal),'function':objects.get(signal.get('object_id'),{}).get('function')}
    return signals


def validate_runtime_plan(value, buttons, default_seconds):
    require(isinstance(value,dict) and set(value)=={'steps','checks'},'runtime_plan needs steps and checks')
    require(isinstance(value['steps'],list) and value['steps'],'runtime_plan needs executable steps')
    require(isinstance(value['checks'],list) and value['checks'],'runtime_plan needs task-specific checks')
    plan=deepcopy(value);ids=set()
    for check in plan['checks']:
        require(isinstance(check,dict) and set(check)=={'id','description'},'runtime check needs id and description')
        require(isinstance(check['id'],str) and check['id'].strip() and check['id'] not in ids,'invalid or duplicate runtime check ID')
        require(isinstance(check['description'],str) and check['description'].strip(),'runtime check description missing')
        ids.add(check['id'])
    def duration(value):
        require(type(value) in (int,float) and math.isfinite(value) and value>0,'runtime duration must be positive and finite')
        return value
    observed=False
    for step in plan['steps']:
        require(isinstance(step,dict),'runtime step must be an object')
        action=step.get('action');allowed={'action','purpose'}
        if action=='observe':
            allowed.add('seconds');step['seconds']=duration(step.get('seconds',default_seconds));observed=True
        elif action=='press_button':
            allowed.update(('object_id','duration_seconds'))
            require(step.get('object_id') in buttons,'operator action needs a native-confirmed momentary button object ID')
            step['duration_seconds']=duration(step.get('duration_seconds',.35))
        elif action=='reset_both':
            allowed.add('mode');require(step.get('mode','warm') in ('warm','cold'),'unsupported PLC reset mode')
        else:require(action in ('start_both','stop_both'),'unsupported runtime action: '+str(action))
        require(not set(step)-allowed,'unsupported runtime step fields')
    require(observed,'runtime_plan needs an observation window')
    return plan


def summarize_evidence(records):
    """Keep all state changes, including count/connection/force changes, without a prefix cutoff."""
    result=[];previous=None;last_sample=None
    for row in records:
        if row.get('kind')!='runtime_sample':
            result.append(deepcopy(row));continue
        state={k:row[k] for k in ('values','normalized_values','item_count','forced_signals','connection_ok','scene_running')}
        changed=previous is None or state!=previous or row['scope']!=last_sample['scope']
        if changed:
            compact={k:row[k] for k in ('id','kind','timestamp','elapsed_seconds','scope','item_count','forced_signals','connection_ok','scene_running')}
            compact['values']=state['values'] if previous is None else {k:v for k,v in state['values'].items() if previous['values'].get(k)!=v}
            compact['normalized_values']=state['normalized_values'] if previous is None else {k:v for k,v in state['normalized_values'].items() if previous['normalized_values'].get(k)!=v}
            compact['state_encoding']='initial_values_then_changes'
            result.append(compact)
        previous=state;last_sample=row
    if last_sample and (not result or result[-1].get('id')!=last_sample['id']):
        result.append({k:last_sample[k] for k in ('id','kind','timestamp','elapsed_seconds','scope','values','normalized_values',
                                                 'item_count','forced_signals','connection_ok','scene_running')})
    return result


class RuntimeRun:
    def __init__(self, factoryio, codesys, plc_dir, signals, manifest, contract, trace):
        self.factoryio,self.codesys,self.plc_dir=factoryio,codesys,Path(plc_dir)
        self.signals,self.manifest,self.contract,self.trace=signals,manifest,contract,trace
        self.client=FactoryIOClient(factoryio);self.output=self.plc_dir/'runtime'
        self.output.mkdir();self.records=[];self.scope='baseline';self.started=time.monotonic()
        self.observing=False;self.errors=[];self.normalization=[]
        self.lifecycle=SynchronizedRuntime(factoryio,codesys,self.plc_dir,self.output/'lifecycle')

    def button(self, object_id, seconds):
        key=next(o['key'] for o in self.manifest['objects'] if o['id']==object_id)
        def action(pressed):
            path=self.output/('button-'+str(len(self.records))+'-'+str(pressed)+'.json')
            run_hook(self.factoryio.get('operator_command'),{'object_key':key,'pressed':str(pressed).lower(),'output':str(path.resolve())},10)
            receipt=read_json(path);observed=receipt.get('data',{}).get('operator_button',{})
            require(receipt.get('ok') is True and observed.get('object_key')==key and observed.get('pressed') is pressed and observed.get('native_input') is True,
                    'native operator button was not confirmed')
            self.records.append({'id':'operator-'+str(len(self.records)),'kind':'operator_action','timestamp':utc_now(),
                'elapsed_seconds':time.monotonic()-self.started,'scope':self.scope,'object_id':object_id,'pressed':pressed,
                'receipt_path':str(path),'receipt_sha256':file_hash(path)})
        try:action(True);self.wait(seconds)
        finally:action(False)

    def sample(self):
        tags=self.client.tags();require(reconcile_tags(self.contract,tags)['passed'],'runtime tags changed')
        by_id={t['id']:t for t in tags};values={};forced=[];normalized={};running=None
        polarity={r['tag_key']:r['active_value'] for r in self.normalization}
        for key,signal in self.signals.items():
            tag=by_id[key];value=tag.get('value');values[key]=value
            if signal.get('data_type')=='bool':require(type(value) is bool,'Boolean tag returned an incompatible value')
            else:require(type(value) in (int,float) and math.isfinite(value),'numeric tag returned an incompatible value')
            if key in polarity:normalized[key]=value==polarity[key]
            if signal.get('system_field')=='RunInputKey' or signal.get('id')=='system/RunInput':running=value
            if tag.get('isForced') is not False:forced.append(key)
        data=self.lifecycle.native_call('status')['data']
        require(Path(data['scene_path']).resolve()==self.plc_dir.parent.joinpath('scene.factoryio').resolve(),'scene changed during runtime')
        count=data.get('item_count');require(type(count) is int and count>=0,'native item count missing')
        modbus=data['modbus'];connected=modbus.get('connected') is True and modbus.get('socket_connected') is True and modbus.get('has_error') is False
        row={'id':'sample-'+str(len(self.records)),'kind':'runtime_sample','timestamp':utc_now(),
             'elapsed_seconds':time.monotonic()-self.started,'scope':self.scope,'values':values,'normalized_values':normalized,
             'forced_signals':forced,'scene_running':running,'connection_ok':connected,'item_count':count,
             'tags':tags,'native_observation':{k:data[k] for k in ('scene_path','frame','item_count','modbus')}}
        if self.observing and running is True and not connected:
            self.errors.append({'code':'connection_lost','sample_id':row['id']})
        return row

    def wait(self, seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if self.sampler_errors:raise self.sampler_errors[0]
            time.sleep(min(.1,max(0,deadline-time.monotonic())))

    def capture(self, label):
        frames=collect_evidence(self.factoryio,self.output/label,io_contract=self.contract,seconds=0)
        require(frames['reconciliation']['passed'],'runtime image tags changed')
        for row in frames['records']:
            if row['kind']=='image':
                metadata=read_json(Path(row['path']).with_suffix('.bridge.json'))['bridge_response']['data']
                require(Path(metadata['scene_path']).resolve()==self.plc_dir.parent.joinpath('scene.factoryio').resolve(),'runtime screenshot scene changed')
                self.records.append({**row,'id':label+'-'+row['id'],'scope':self.scope})

    def run(self, scaffold, plan, seconds=30, keep_running=False, reset_mode='warm'):
        plan=validate_runtime_plan(plan,scaffold.get('operator_buttons',{}),seconds)
        self.normalization=scaffold.get('normalization',[]);end=Event();ready=Event();self.sampler_errors=[]
        result={'passed':False,'left_running':False,'process_verdict':'pending_visual_review','runtime_plan':plan}
        leave=False;worker=None
        def sample_loop():
            try:
                while not end.is_set():
                    self.records.append(self.sample());ready.set();end.wait(self.factoryio.get('sample_interval_seconds',.05))
            except Exception as error:self.sampler_errors.append(error);ready.set();end.set()
        try:
            self.trace('tool_requested',phase='runtime_control',tool='synchronized_runtime',action='reset_and_start')
            self.lifecycle.reset_both(reset_mode)
            worker=Thread(target=sample_loop,daemon=True);worker.start()
            require(ready.wait(12),'runtime sampler did not become ready')
            if self.sampler_errors:raise self.sampler_errors[0]
            self.lifecycle.start_both()
            self.trace('tool_completed',phase='runtime_control',tool='synchronized_runtime',action='reset_and_start')
            for index,step in enumerate(plan['steps']):
                self.scope='step-'+str(index);action=step['action']
                self.records.append({'id':self.scope,'kind':'runtime_step','timestamp':utc_now(),'step':step})
                tool='runtime_observer' if action=='observe' else 'factoryio_bridge' if action=='press_button' else 'synchronized_runtime'
                self.trace('tool_requested',phase='runtime',tool=tool,action=action,step=step)
                if action=='observe':
                    self.observing=True
                    try:
                        self.capture(self.scope+'-begin');self.wait(step['seconds']);self.capture(self.scope+'-end')
                    finally:self.observing=False
                elif action=='press_button':self.button(step['object_id'],step['duration_seconds'])
                elif action=='reset_both':self.lifecycle.reset_both(step.get('mode',reset_mode))
                elif action=='start_both':self.lifecycle.start_both()
                elif action=='stop_both':self.lifecycle.stop_both()
                self.trace('tool_completed',phase='runtime',tool=tool,action=action,step_index=index)
            self.scope='post_plan';final=self.sample();self.records.append(final)
            infrastructure={'passed':not self.errors,'issues':self.errors,
                'sample_count':sum(r.get('kind')=='runtime_sample' for r in self.records),
                'scope':'scene_identity_tag_types_and_connection','process_acceptance_evaluated':False}
            leave=keep_running and final['scene_running'] is True and infrastructure['passed']
            result.update(infrastructure=infrastructure,observations_complete=True,left_running=leave,
                          scope='task_provided_operator_plan')
            return result
        finally:
            end.set()
            if worker is not None:
                worker.join(12)
                if worker.is_alive():result.update(cleanup_error='runtime sampler did not finish',left_running=False);leave=False
            if not leave:
                try:self.lifecycle.stop_both()
                except Exception as error:result.update(cleanup_error=str(error),left_running=False)
            for index,record in enumerate(self.lifecycle.records):
                self.records.append({'id':'lifecycle-'+str(index),'kind':'lifecycle_receipt',**record})
            write_json(self.output/'evidence.json',{'schema_version':1,'mode':'live','records':self.records,'finished_at':utc_now()})
            write_json(self.plc_dir/'acceptance.json',result)

    def stop(self):return self.lifecycle.stop_both()