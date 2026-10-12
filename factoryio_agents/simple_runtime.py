"""One reusable single-item transfer check, driven by bound roles and native observations."""
from pathlib import Path
from threading import Event, Thread
import time

from .contracts import file_hash, read_json, require, write_json
from .runtime import FactoryIOClient, collect_evidence, reconcile_tags, run_hook, utc_now
from .synchronized_runtime import SynchronizedRuntime


def evaluate_transfer(samples, required_cycles=2, scope='cycle'):
    rows=[r for r in samples if r['scope']==scope]
    issues=[];cycles=[];active=None;previous_entry=False;previous_exit=False;maximum=0
    def issue(code,row):
        if not any(i['code']==code for i in issues):issues.append({'code':code,'sample_id':row.get('id'),'elapsed_seconds':row['elapsed_seconds']})
    for row in rows:
        values=row['values'];t=row['elapsed_seconds'];count=row['item_count'];maximum=max(maximum,count)
        if row.get('forced_signals'):issue('forced_signals',row)
        if not row.get('connection_ok'):issue('connection_lost',row)
        if count>1:issue('multiple_items',row)
        if not values['running']:issue('simulation_not_running',row)
        if not values['conveyor']:issue('conveyor_stopped',row)
        entry=values['entry'];exit_=values['exit']
        if entry and not previous_entry:
            if active is not None:issue('new_entry_before_removal',row)
            else:active={'entry_at':t,'entry_sample':row.get('id'),'exit_at':None}
        if exit_ and not previous_exit:
            if active is None:issue('exit_before_entry',row)
            else:active['exit_at']=t;active['exit_sample']=row.get('id')
        if active is not None:
            if t-active['entry_at']>.2 and values['feed']:issue('feed_not_closed',row)
            if active['exit_at'] is not None and count==0:
                cycles.append({**active,'removed_at':t,'removal_sample':row.get('id')});active=None
        previous_entry,previous_exit=entry,exit_
    if len(cycles)<required_cycles:
        issues.append({'code':'insufficient_ordered_cycles','expected':required_cycles,'observed':len(cycles)})
    return {'passed':not issues,'scope':scope,'completed_cycles':len(cycles),'cycles':cycles,
        'observed_max_items':maximum,'sample_count':len(rows),'issues':issues,
        'limitation':'Finite sampled observations; no claim of continuous or future correctness.'}


def transfer_signals(design, scaffold):
    objects={o['id']:o for o in design['objects']};signals=scaffold['signals'];selected={}
    def choose(role,predicate):
        matches=[s for s in signals if predicate(s,objects.get(s.get('object_id'),{}))]
        require(len(matches)==1,'single-transfer observation needs exactly one '+role+' signal')
        selected[role]=matches[0]
    choose('running',lambda s,o:s['id']=='system/RunInput')
    for role,prefab,kind in [('feed','Emitter','output'),('remove','Remover','output'),
        ('start','StartPushButton','input'),('stop','StopPushButton','input'),('reset','ResetPushButton','input')]:
        choose(role,lambda s,o,p=prefab,k=kind:o.get('prefab')==p and s['kind']==k)
    choose('conveyor',lambda s,o:'conveyor' in o.get('prefab','').lower() and s['kind']=='output')
    for role,words in [('entry',('entry','entrance','入口')),('exit',('exit','出口'))]:
        choose(role,lambda s,o,words=words:s['kind']=='input' and 'sensor' in o.get('prefab','').lower()
               and any(w in (o.get('id','')+' '+o.get('function','')).lower() for w in words))
    require(len({s['tag_key'] for s in selected.values()})==len(selected),'runtime roles overlap')
    return selected


class TransferRun:
    def __init__(self, factoryio, codesys, plc_dir, signals, manifest, contract, trace):
        self.factoryio,self.codesys,self.plc_dir=factoryio,codesys,Path(plc_dir)
        self.signals,self.manifest,self.contract,self.trace=signals,manifest,contract,trace
        self.client=FactoryIOClient(factoryio);self.output=self.plc_dir/'runtime'
        self.output.mkdir();self.records=[];self.scope='startup';self.started=time.monotonic()
        self.lifecycle=SynchronizedRuntime(factoryio,codesys,self.plc_dir,self.output/'lifecycle')

    def button(self,role):
        key=next(o['key'] for o in self.manifest['objects'] if o['id']==self.signals[role]['object_id'])
        def action(pressed):
            path=self.output/(role+'-'+str(len(self.records))+'-'+str(pressed)+'.json')
            run_hook(self.factoryio.get('operator_command'),{'object_key':key,'pressed':str(pressed).lower(),'output':str(path.resolve())},10)
            receipt=read_json(path);observed=receipt.get('data',{}).get('operator_button',{})
            require(receipt.get('ok') is True and observed.get('object_key')==key and observed.get('pressed') is pressed
                    and observed.get('native_input') is True,'native operator button was not confirmed')
            self.records.append({'id':'operator-'+str(len(self.records)),'kind':'operator_action','timestamp':utc_now(),
                'role':role,'pressed':pressed,'receipt_path':str(path),'receipt_sha256':file_hash(path)})
        try:action(True);time.sleep(.35)
        finally:action(False)

    def sample(self):
        scope=self.scope;tags=self.client.tags();require(reconcile_tags(self.contract,tags)['passed'],'runtime tags changed')
        by_id={t['id']:t for t in tags};values={};forced=[]
        for role,signal in self.signals.items():
            tag=by_id[signal['tag_key']];require(type(tag.get('value')) is bool,'runtime signal is not Boolean')
            active=signal.get('normalized_active_value',True)
            # The scaffold exposes normalized TRUE; the actual input uses its confirmed raw polarity.
            if signal['kind']=='input':
                active=next(r['active_value'] for r in self.normalization if r['tag_key']==signal['tag_key'])
            values[role]=tag['value']==active
            if tag.get('isForced') is not False:forced.append(role)
        native=self.lifecycle.native_call('status');data=native['data']
        require(Path(data['scene_path']).resolve()==self.plc_dir.parent.joinpath('scene.factoryio').resolve(),'scene changed during runtime')
        modbus=data['modbus'];count=data['item_count'];require(type(count) is int and count>=0,'native item count missing')
        return {'id':'sample-'+str(len(self.records)),'kind':'transfer_sample','timestamp':utc_now(),
            'elapsed_seconds':time.monotonic()-self.started,'scope':scope,'values':values,'forced_signals':forced,
            'connection_ok':modbus.get('connected') is True and modbus.get('socket_connected') is True and modbus.get('has_error') is False,
            'item_count':count,'tags':tags,'native_observation':{k:data[k] for k in ('scene_path','frame','item_count','modbus')},
            'sources':['factoryio_web_api','native_bridge.Scene.CurrentItemCount']}

    def segment(self,scope,seconds,scaffold,capture=False):
        self.scope=scope;self.normalization=scaffold['normalization'];end=Event();errors=[]
        def sample_loop():
            try:
                while not end.is_set():
                    self.records.append(self.sample());end.wait(self.factoryio.get('sample_interval_seconds',.05))
            except Exception as error:errors.append(error);end.set()
        worker=Thread(target=sample_loop,daemon=True);worker.start()
        try:
            if capture:
                frames=collect_evidence(self.factoryio,self.output/(scope+'-images'),io_contract=self.contract,seconds=0)
                require(frames['reconciliation']['passed'],'runtime image tags changed')
                for row in frames['records']:
                    if row['kind']=='image':
                        metadata=read_json(Path(row['path']).with_suffix('.bridge.json'))['bridge_response']['data']
                        require(Path(metadata['scene_path']).resolve()==self.plc_dir.parent.joinpath('scene.factoryio').resolve(),'runtime screenshot scene changed')
                        self.records.append({**row,'id':scope+'-'+row['id']})
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline and not errors:time.sleep(min(.1,max(0,deadline-time.monotonic())))
        finally:end.set();worker.join(12);require(not worker.is_alive(),'runtime sampler did not finish')
        if errors:raise errors[0]

    def run(self,scaffold,seconds=30,keep_running=False,reset_mode='warm'):
        require(10<=seconds<=120,'runtime observation must be 10..120 seconds')
        result={'passed':False,'left_running':False};leave=False
        try:
            self.trace('tool_requested',phase='runtime_control',tool='synchronized_runtime',action='reset_and_start')
            reset=self.lifecycle.reset_both(reset_mode);self.lifecycle.start_both();self.button('start');time.sleep(.3)
            self.trace('tool_completed',phase='runtime_control',tool='synchronized_runtime',action='reset_and_start')
            self.trace('tool_requested',phase='runtime',tool='transfer_observer',action='sample_cycles_and_items')
            self.segment('cycle',seconds,scaffold,capture=True)
            primary=evaluate_transfer(self.records)
            self.button('stop');time.sleep(.35);self.segment('operator_stop',1,scaffold)
            stop_rows=[r for r in self.records if r.get('scope')=='operator_stop']
            operator_stop=bool(stop_rows) and all(not r['values']['feed'] and not r['values']['conveyor'] for r in stop_rows)
            stopped=self.lifecycle.stop_both();reset=self.lifecycle.reset_both(reset_mode)
            self.lifecycle.start_both();self.button('start');time.sleep(.3)
            self.segment('after_reset',min(20,seconds),scaffold,capture=True)
            restarted=evaluate_transfer(self.records,required_cycles=1,scope='after_reset')
            passed=primary['passed'] and operator_stop and restarted['passed'] and stopped['operation_passed'] and reset['operation_passed']
            result={'passed':passed,'scope':'single_item_transfer','cycles':primary,'operator_stop_passed':operator_stop,
                'synchronized_stop_passed':stopped['operation_passed'],'synchronized_reset_passed':reset['operation_passed'],
                'reset_mode':reset_mode,'restart':restarted,'left_running':passed and keep_running}
            leave=passed and keep_running
            self.trace('tool_completed',phase='runtime',tool='transfer_observer',action='sample_cycles_and_items',
                passed=passed,completed_cycles=primary['completed_cycles'],max_items=primary['observed_max_items'])
            return result
        finally:
            self.scope='cleanup'
            if not leave:
                try:self.lifecycle.stop_both()
                except Exception as error:result.update(passed=False,left_running=False,cleanup_error=str(error))
            evidence={'schema_version':1,'mode':'live','records':self.records,'finished_at':utc_now()}
            write_json(self.output/'evidence.json',evidence);write_json(self.plc_dir/'acceptance.json',result)

    def stop(self):return self.lifecycle.stop_both()
