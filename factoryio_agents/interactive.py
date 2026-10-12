"""Human-controlled boundaries around the existing model client and tool trace hooks."""
from copy import deepcopy
from pathlib import Path
from threading import Condition
import time
import uuid

from .contracts import ContractError, file_hash, read_json, require, write_json
from .training import Redactor, TrainingRecorder, json_text, now
from .progress import substage


class ManualControl:
    def __init__(self, output, secrets=()):
        self.output=Path(output).resolve()
        self.redactor=Redactor(secrets)
        self.condition=Condition()
        self.steps=[]; self.events=[]; self.assets={}
        self.pending=None; self.cancelled=False; self.revision=0

    def save(self):
        self.revision+=1
        write_json(self.output/'manual/steps.json',{'schema_version':1,'revision':self.revision,
            'pending_id':self.pending,'cancelled':self.cancelled,'steps':self.steps})

    def image(self, path, image_id):
        path=Path(path).resolve()
        require(path.suffix.lower() in ('.png','.jpg','.jpeg','.webp'),'unsupported preview image')
        digest=file_hash(path)
        target=self.output/'manual/assets'/(digest+path.suffix.lower())
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists(): target.write_bytes(path.read_bytes())
        require(file_hash(target)==digest,'preview image changed during snapshot')
        asset_id=digest+path.suffix.lower()
        self.assets[asset_id]=target
        return {'asset_id':asset_id,'image_id':image_id,'name':path.name,'sha256':digest,
                'size_bytes':target.stat().st_size}

    def asset(self, identifier):
        with self.condition:
            require(identifier in self.assets,'unknown image asset')
            path=self.assets[identifier]
            require(path.is_file() and path.is_relative_to(self.output),'image asset is unavailable')
            return path

    def stage(self, kind, role, phase, request, budget=None):
        with self.condition:
            if self.cancelled: raise KeyboardInterrupt('manual workflow cancelled')
            require(self.pending is None,'another step is awaiting approval')
            identifier=uuid.uuid4().hex
            row=self.redactor.clean({'id':identifier,'number':len(self.steps)+1,'kind':kind,'role':role,
                'phase':phase,'status':'waiting','created_at':now(),'request':request,
                'response':None,'raw_response':None,'error':None})
            self.steps.append(row); self.pending=identifier; self.save(); self.condition.notify_all()
            started=time.monotonic()
            try:
                self.condition.wait_for(lambda:self.cancelled or row['status']!='waiting')
                if self.cancelled: raise KeyboardInterrupt('manual workflow cancelled')
            finally:
                if budget is not None: budget.exclude_human_wait(time.monotonic()-started)
            return identifier

    def approve(self, identifier):
        with self.condition:
            require(not self.cancelled and self.pending==identifier,'this step is no longer awaiting approval')
            row=next(r for r in self.steps if r['id']==identifier)
            require(row['status']=='waiting','step was already released')
            row.update(status='running',approved_at=now())
            self.pending=None; self.save(); self.condition.notify_all()

    def finish(self, identifier, response=None, raw=None, error=None, status=None, metadata=None):
        with self.condition:
            row=next(r for r in self.steps if r['id']==identifier)
            row.update(self.redactor.clean({'status':status or ('failed' if error else 'completed'),
                'response':response,'raw_response':raw,'error':str(error) if error else None,
                'metadata':metadata or {},'finished_at':now()}))
            self.save(); self.condition.notify_all()

    def before_tool(self, record, budget=None):
        identifier=self.stage('tool','tools',record.get('phase','tools'),{'tool':record.get('tool'),
            'action':record.get('action'),'details':record},budget)
        self.finish(identifier,response={'note':'此工具操作已获放行，执行结果见后续日志和产物。'},status='released')

    def observe(self, record):
        with self.condition:
            self.events.append(self.redactor.clean(record))
            if len(self.events)>4000: self.events=self.events[-4000:]
            self.revision+=1

    def cancel(self):
        with self.condition:
            self.cancelled=True
            if self.pending:
                row=next(r for r in self.steps if r['id']==self.pending)
                row.update(status='cancelled',finished_at=now())
            self.pending=None; self.save(); self.condition.notify_all()

    def wait_pending(self, after=None, timeout=None):
        with self.condition:
            ready=self.condition.wait_for(lambda:self.cancelled or (self.pending and self.pending!=after),timeout)
            return self.pending if ready and not self.cancelled else None

    def step(self, identifier):
        with self.condition:
            row=next((r for r in self.steps if r['id']==identifier),None)
            require(row is not None,'unknown step')
            return deepcopy(row)

    def snapshot(self):
        with self.condition:
            keys=('id','number','kind','role','phase','status','created_at','finished_at','error')
            return {'revision':self.revision,'pending_id':self.pending,'cancelled':self.cancelled,
                'steps':[{**{k:r[k] for k in keys if k in r},'image_count':len(r['request'].get('images',[]))}
                         for r in self.steps], 'events':deepcopy(self.events[-70:])}


def model_info(raw, role, payload):
    from .llm import ModelClient
    if not isinstance(raw,ModelClient): return {}
    config=raw.config; name=config.get('role_providers',{}).get(role)
    provider=raw.providers.get(name,{})
    options={**config.get('provider_options',{}).get(name,{}),**config.get('request_options',{}).get(role,{})}
    fmt={'type':'json_object'} if config.get('json_mode',True) else None
    if role in config.get('schema_roles',[]):
        from .response_schema import review_schema
        fmt={'type':'json_schema','json_schema':{'name':'FactoryIOReview','strict':True,'schema':review_schema(payload)}}
    return {'provider':name,'model':provider.get('model_name') or config.get('models',{}).get(role),
            'request_options':options,'response_format':fmt}


class ManualClient:
    def __init__(self, client, control, budget=None, info=None):
        self.client,self.control,self.budget,self.info=client,control,budget,info
        recorder=getattr(client,'recorder',None)
        if isinstance(recorder,TrainingRecorder): self.recorder=recorder

    def complete(self, role, system, payload, images=None):
        image_ids=[r['id'] for r in (payload.get('evidence') or {}).get('records',[]) if r.get('kind')=='image']
        assets=[self.control.image(path,image_ids[n] if n<len(image_ids) else 'image-'+str(n+1))
                for n,path in enumerate(images or [])]
        content=[{'type':'text','text':json_text(payload)}]+[{'type':'image','asset_id':a['asset_id'],'image_id':a['image_id']} for a in assets]
        request={'system':system,'payload':payload,'images':assets,
                 'messages':[{'role':'system','content':system},{'role':'user','content':content if images else content[0]['text']}],
                 'provider':self.info(role,payload) if self.info else {}}
        identifier=self.control.stage('model',role,substage(payload,role),request,self.budget)
        frozen=[self.control.asset(a['asset_id']) for a in assets]
        recorder=getattr(self,'recorder',None)
        prior_candidate=recorder.last_candidate_id if recorder else None
        try:
            value=self.client.complete(role,system,payload,frozen) if images else self.client.complete(role,system,payload)
            record=None; recorder=getattr(self,'recorder',None)
            if recorder and recorder.last_candidate_id:
                record=read_json(recorder.directory/'candidates'/(recorder.last_candidate_id+'.json'))
            self.control.finish(identifier,value,record.get('response_text') if record else json_text(value),
                metadata=record.get('model_metadata') if record else None)
            return value
        except BaseException as error:
            record=None
            if recorder and recorder.last_candidate_id and recorder.last_candidate_id!=prior_candidate:
                record=read_json(recorder.directory/'candidates'/(recorder.last_candidate_id+'.json'))
            self.control.finish(identifier,error=error,raw=record.get('response_text') if record else None,
                metadata=record.get('model_metadata') if record else None)
            raise
