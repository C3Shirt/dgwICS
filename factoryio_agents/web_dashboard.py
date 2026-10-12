"""Local, manually stepped UI over Workflow; standard-library HTTP, no alternate agents."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
from pathlib import Path
import secrets
from threading import RLock, Thread
from urllib.parse import unquote, urlsplit, parse_qs
import uuid

from .contracts import ContractError, canonical_hash, file_hash, read_json, require, write_json
from .cli import read_configuration, protected_credentials
from .interactive import ManualControl
from .llm import ModelClient
from .prompts import policy_manifest
from .training import Redactor, configured_secret_values, now
from .workflow import Workflow

ROOT=Path(__file__).resolve().parents[1]
STATIC=Path(__file__).parent/'webui'


class Dashboard:
    def __init__(self, project=ROOT, config=None):
        self.project=Path(project).resolve(); self.config_path=Path(config or self.project/'config.agent.json').resolve()
        self.config=read_configuration(self.config_path)
        raw=ModelClient(self.config['llm'])
        self.secrets=configured_secret_values(self.config)+raw.secret_values()
        self.redactor=Redactor(self.secrets); self.lock=RLock()
        self.sessions={}; self.history={}
        self.models={role:raw.providers.get(self.config['llm'].get('role_providers',{}).get(role),{}).get('model_name')
            or self.config['llm'].get('models',{}).get(role) for role in ('planner','scene','vision','plc')}

    def scan_history(self):
        paths=[]
        for area in ('runs','artifacts'):
            base=self.project/area
            if not base.is_dir(): continue
            for root in base.iterdir():
                if not root.is_dir(): continue
                if (root/'training-data/candidates').is_dir(): paths.append(root)
                if area=='artifacts':
                    for child in root.iterdir():
                        if child.is_dir() and child.name not in ('framework-source','build','memory','training-data') and (
                                child/'training-data/candidates').is_dir(): paths.append(child)
        active={s['output'] for s in self.sessions.values() if not s.get('historical')}
        for path in sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True)[:70]:
            path=path.resolve()
            if str(path) in active: continue
            identifier='history-'+canonical_hash(str(path))[:16]
            self.history[identifier]=path
        return [{'id':key,'name':str(path.relative_to(self.project)),'output':str(path),'historical':True,
            'status':'history','created_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()}
            for key,path in self.history.items()]

    def bootstrap(self):
        with self.lock:
            history=self.scan_history()
            from .component_kb import knowledge_base
            return {'project':str(self.project),'models':self.models,'knowledge':knowledge_base().stats(),
                'sessions':[self.summary(s) for s in self.sessions.values() if not s.get('historical')],
                'history':history,'manual_control':True}

    def knowledge(self,query='',prefab=None):
        from .component_kb import knowledge_base
        kb=knowledge_base();kb.unchanged()
        return {'stats':kb.stats(),'results':kb.search(query,8) if query else [],
                'reference':kb.references([prefab],'vision') if prefab else None}

    def historical(self, identifier):
        require(identifier in self.history,'unknown historical run')
        root=self.history[identifier]; control=ManualControl(root,self.secrets)
        records=[]
        for path in (root/'training-data/candidates').glob('*.json'):
            try: records.append(read_json(path))
            except (ValueError,OSError): continue
        for record in sorted(records,key=lambda r:r.get('started_at','')):
            sid=record['candidate_id']; images=[]
            image_ids=[r['id'] for r in record.get('payload',{}).get('evidence',{}).get('records',[]) if r.get('kind')=='image']
            for n,asset in enumerate(record.get('images',[])):
                path=(root/'training-data'/asset['path']).resolve()
                if not path.is_relative_to(root/'training-data') or not path.is_file(): continue
                aid=sid+'-'+str(n); control.assets[aid]=path
                images.append({'asset_id':aid,'image_id':image_ids[n] if n<len(image_ids) else 'image-'+str(n+1),'name':path.name,
                    'sha256':asset['sha256'],'size_bytes':asset.get('size_bytes')})
            system=next((m['content'] for m in record.get('messages',[]) if m['role']=='system'),'')
            payload=record.get('payload',{})
            from .progress import substage
            phase=substage(payload,record['role'])
            control.steps.append(self.redactor.clean({'id':sid,'number':len(control.steps)+1,'kind':'model',
                'role':record['role'],'phase':phase,'status':'completed' if record.get('status')=='response_received' else 'failed',
                'created_at':record.get('started_at'),'finished_at':record.get('finished_at'),
                'request':{'system':system,'payload':payload,'messages':record.get('messages',[]),'images':images,
                    'provider':record.get('model_metadata',{})},
                'response':record.get('structured_output'),'raw_response':record.get('response_text'),
                'metadata':record.get('model_metadata',{}),'error':record.get('error')}))
        session={'id':identifier,'name':str(root.relative_to(self.project)),'output':str(root),'mode':'history',
            'historical':True,'status':'history','created_at':None,'control':control,'result':None}
        self.sessions[identifier]=session
        return session

    def get(self, identifier):
        with self.lock:
            if identifier not in self.sessions:
                if re.fullmatch(r'[0-9a-f]{32}',identifier):
                    return self.restore_finished_session(identifier)
                return self.historical(identifier)
            return self.sessions[identifier]

    def restore_finished_session(self, identifier):
        """Reopen an ended web session after a dashboard upgrade, without executing it."""
        matches=list((self.project/'runs').glob('web-*-'+identifier[:8]))
        require(len(matches)==1,'saved web session not found')
        root=matches[0].resolve();require(root.is_relative_to(self.project/'runs'),'invalid saved session path')
        saved=read_json(root/'web-session.json')
        require(saved.get('id')==identifier and Path(saved['output']).resolve()==root,'saved session identity differs')
        require(saved.get('status') in ('finished','failed','cancelled') and not (root/'.writer.lock').exists(),
                'this session is still active in another dashboard; continue there')
        control=ManualControl(root,self.secrets)
        record=read_json(root/'manual/steps.json') if (root/'manual/steps.json').is_file() else {}
        control.steps=self.redactor.clean(record.get('steps',[]));control.revision=record.get('revision',0)
        for step in control.steps:
            for asset in step.get('request',{}).get('images',[]):
                path=(root/'manual/assets'/asset['asset_id']).resolve()
                require(path.is_relative_to(root/'manual/assets') and path.is_file() and file_hash(path)==asset['sha256'],
                        'saved manual image changed or unavailable')
                control.assets[asset['asset_id']]=path
        if (root/'trace.jsonl').is_file():
            control.events=self.redactor.clean([json.loads(line) for line in (root/'trace.jsonl').read_text(encoding='utf-8').splitlines()[-70:] if line.strip()])
        session={**self.redactor.clean(saved),'historical':False,'control':control,'restored':True}
        self.sessions[identifier]=session
        return session

    def summary(self, session):
        snapshot=session['control'].snapshot()
        return {k:session.get(k) for k in ('id','name','output','mode','historical','created_at','result')} | {
            'status':'waiting' if snapshot['pending_id'] else session['status'],
            'pending_id':snapshot['pending_id'],'revision':snapshot['revision'],
            'steps':snapshot['steps'],'events':snapshot['events']}

    def resolve_source(self, identifier):
        session=self.get(identifier)
        return Path(session['output']).resolve()

    def create(self, body):
        mode=body.get('mode','prepare')
        require(mode in ('prepare','run','vision','resume'),'unknown workflow mode')
        prompt=body.get('prompt','').strip()
        require(mode in ('vision','resume') or bool(prompt),'enter a scene requirement')
        with self.lock:
            require(not any(s['status'] in ('running','cancelling') for s in self.sessions.values() if not s.get('historical')),
                    '已有手动任务正在运行，请先结束当前任务。')
            source=self.resolve_source(body['source_id']) if mode in ('vision','resume') else None
            if mode=='vision':
                require((source/'task.json').is_file(),'source has no task.json')
                prompt=read_json(source/'task.json')['requirement']
            identifier=uuid.uuid4().hex
            output=self.project/'runs'/('web-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+identifier[:8])
            control=ManualControl(output,self.secrets)
            session={'id':identifier,'name':prompt[:80] or '恢复已有任务','output':str(output),'mode':mode,
                'historical':False,'status':'running','created_at':now(),'control':control,'result':None}
            self.sessions[identifier]=session
            output.mkdir(parents=True)
            write_json(output/'web-session.json',{k:v for k,v in session.items() if k!='control'})
            thread=Thread(target=self.work,args=(session,prompt,source),daemon=True)
            session['thread']=thread; thread.start()
            return self.summary(session)

    def work(self, session, prompt, source):
        output=Path(session['output']); config=deepcopy(self.config); control=session['control']
        try:
            if session['mode'] in ('run','resume'): protected_credentials(config)
            workflow=Workflow(config,output,progress=control.observe,interaction=control)
            control.redactor=workflow.redactor
            write_json(output/'config.snapshot.json',workflow.redactor.clean(config))
            # Hash identity avoids copying the entire source tree into every web run.
            source_hashes={str(p.relative_to(self.project)):file_hash(p) for p in
                (self.project/'factoryio_agents').rglob('*.py')}
            write_json(output/'execution.json',{'interface':'manual_web','source_hashes':source_hashes,
                'framework_source_storage':'hashes_only','started_at':now()})
            if session['mode']=='vision': result=self.replay_vision(workflow,source)
            elif session['mode']=='resume':
                from .memory import inspect_resume
                result=workflow.resume(inspect_resume(source,config))
            else: result=getattr(workflow,session['mode'])(prompt)
            session['result']=workflow.redactor.clean(result)
            session['status']='finished' if not control.cancelled else 'cancelled'
        except KeyboardInterrupt:
            session.update(status='cancelled',result={'status':'cancelled','reason':'manual cancellation'})
        except Exception as error:
            session.update(status='failed',result={'status':'failed','error':self.redactor.clean(str(error))})
        finally:
            control.observe({'event':'web_session_finished','timestamp':now(),'status':session['status'],
                             'result':session['result']})
            write_json(output/'web-session.json',{k:v for k,v in session.items() if k not in ('control','thread')})

    def replay_vision(self, workflow, source):
        if (source/'manifest.json').is_file():
            scene=source; evidence_path=source/'evidence.json'
        else:
            scenes=sorted(p for p in source.glob('scene-*') if (p/'visual/evidence.json').is_file())
            require(bool(scenes),'source has no scene images'); scene=scenes[-1]; evidence_path=scene/'visual/evidence.json'
        task=read_json(source/'task.json'); manifest=read_json(scene/'manifest.json'); io=read_json(scene/'io-contract.json')
        versions=read_json(scene/'versions.json'); evidence=read_json(evidence_path)
        versions.update(agent_policy=canonical_hash(policy_manifest()),config=canonical_hash(workflow.config))
        evidence['versions']=versions
        evidence['review_context']={'archived':True,'source_run':str(source),'fresh_capture':False}
        reference=source/'approved-layout.json'
        if not reference.is_file(): reference=scene/'layout-reference.json'
        approved=read_json(reference) if reference.is_file() else None
        output=workflow.output
        for name,value in [('task',task),('manifest',manifest),('io-contract',io),('versions',versions),('evidence',evidence)]:
            write_json(output/(name+'.json'),value)
        if approved: write_json(output/'approved-layout.json',approved)
        workflow.state['versions']=versions
        inputs=[output/(name+'.json') for name in ('task','manifest','io-contract','versions','evidence')]
        if approved: inputs.append(output/'approved-layout.json')
        feedback=workflow.observer.review(task,manifest,io,evidence,versions,'scene',
            handoff=workflow.handoff('vision','scene_review',inputs,'scene-feedback.json'),approved_layout=approved)
        write_json(output/'scene-feedback.json',feedback)
        return {'status':'archived_review_completed','feedback':feedback,'fresh_capture':False,
                'software_modified':False,'live_pipeline_passed':False}

    def cancel(self, identifier):
        session=self.get(identifier); require(not session.get('historical'),'historical runs are read-only')
        session['status']='cancelling'; session['control'].cancel()

    def close(self):
        for session in list(self.sessions.values()):
            if not session.get('historical') and session['status']=='running': session['control'].cancel()


def create_server(dashboard, port=8766):
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def send(self, value, status=200, content_type='application/json; charset=utf-8'):
            data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.send_response(status); self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            try:
                path=urlsplit(self.path).path
                if path in ('/','/app.js','/style.css'):
                    name='index.html' if path=='/' else path[1:]
                    data=(STATIC/name).read_bytes()
                    if name=='index.html': data=data.replace(b'__TOKEN__',token.encode('ascii'))
                    return self.send(data,content_type=mimetypes.guess_type(name)[0]+'; charset=utf-8')
                parts=[unquote(p) for p in path.strip('/').split('/')]
                if parts==['api','bootstrap']: return self.send(dashboard.bootstrap())
                if parts==['api','knowledge']:
                    query=parse_qs(urlsplit(self.path).query)
                    return self.send(dashboard.knowledge(query.get('q',[''])[0],query.get('prefab',[None])[0]))
                if len(parts)>=3 and parts[:2]==['api','sessions']:
                    session=dashboard.get(parts[2])
                    if len(parts)==3: return self.send(dashboard.summary(session))
                    if len(parts)==5 and parts[3]=='steps': return self.send(session['control'].step(parts[4]))
                    if len(parts)==5 and parts[3]=='images':
                        asset=session['control'].asset(parts[4])
                        return self.send(asset.read_bytes(),content_type=mimetypes.guess_type(asset.name)[0])
                self.send({'error':'Not found'},404)
            except (ValueError,KeyError,OSError) as error: self.send({'error':dashboard.redactor.clean(str(error))},400)

        def do_POST(self):
            try:
                require(self.headers.get('X-Dashboard-Token')==token,'invalid local dashboard token')
                origin=self.headers.get('Origin')
                require(origin in (None,'http://'+self.headers.get('Host','')),'request origin mismatch')
                length=int(self.headers.get('Content-Length','0')); require(0<length<=1024*1024,'invalid request size')
                body=json.loads(self.rfile.read(length)); require(isinstance(body,dict),'request must be an object')
                parts=urlsplit(self.path).path.strip('/').split('/')
                if parts==['api','sessions']: return self.send(dashboard.create(body),201)
                if len(parts)==4 and parts[:2]==['api','sessions']:
                    session=dashboard.get(parts[2]); require(not session.get('historical'),'historical runs are read-only')
                    if parts[3]=='approve': session['control'].approve(body.get('step_id'))
                    elif parts[3]=='cancel': dashboard.cancel(parts[2])
                    else: raise ContractError('unknown action')
                    return self.send(dashboard.summary(session))
                self.send({'error':'Not found'},404)
            except (ValueError,KeyError,OSError) as error: self.send({'error':dashboard.redactor.clean(str(error))},400)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.dashboard_token=token
    return server


def main(argv=None):
    parser=argparse.ArgumentParser(description='本地手动 Agent 调试台，每一步由网页按钮放行。')
    parser.add_argument('--config',type=Path,default=ROOT/'config.agent.json')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--server-info',type=Path,default=ROOT/'artifacts/web-dashboard-server.json')
    args=parser.parse_args(argv)
    dashboard=Dashboard(config=args.config)
    server=create_server(dashboard,args.port)
    url='http://127.0.0.1:'+str(server.server_port)
    write_json(args.server_info,{'url':url,'pid':os.getpid(),'config':str(args.config.resolve()),'started_at':now()})
    print('Factory I/O Agent 调试台：'+url,flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: dashboard.close(); server.server_close()


if __name__=='__main__': main()
