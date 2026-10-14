"""Manual step boundaries and local dashboard use injected services only."""
from pathlib import Path
from threading import Thread
import json
import tempfile
import unittest
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
import urllib.request
import urllib.error

from factoryio_agents.contracts import ContractError, file_hash


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def module(self):
        import factoryio_agents
        self.assertTrue((Path(factoryio_agents.__file__).parent/'interactive.py').is_file(),
                        'missing manual interaction boundary for the existing framework')
        from factoryio_agents import interactive
        return interactive

    def test_model_request_and_images_are_previewed_before_any_call(self):
        module=self.module(); control=module.ManualControl(self.root)
        raw=Mock(); raw.complete.return_value={'checks':[]}
        image=self.root/'original.png'; image.write_bytes(b'original image bytes')
        client=module.ManualClient(raw,control)
        result=[]
        thread=Thread(target=lambda:result.append(client.complete('vision','Look at images',
            {'phase':'scene','layout_review':{'scene_goal':'Inspect a conveyor'}},[image])))
        thread.start(); self.addCleanup(lambda:control.cancel())
        pending=control.wait_pending(timeout=2)
        self.assertIsNotNone(pending)
        raw.complete.assert_not_called()
        step=control.step(pending)
        self.assertEqual(step['request']['system'],'Look at images')
        self.assertEqual(len(step['request']['images']),1)
        asset=control.asset(step['request']['images'][0]['asset_id'])
        self.assertEqual(asset.read_bytes(),b'original image bytes')
        image.write_bytes(b'changed source after preview')
        control.approve(pending); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result,[{'checks':[]}])
        sent=raw.complete.call_args.args[3][0]
        self.assertEqual(sent.read_bytes(),asset.read_bytes())
        self.assertEqual(control.step(pending)['status'],'completed')

    def test_one_approval_cannot_release_the_next_agent(self):
        module=self.module(); control=module.ManualControl(self.root)
        raw=Mock(); raw.complete.return_value={'answer':'done'}
        client=module.ManualClient(raw,control)
        thread=Thread(target=lambda:[client.complete(role,'prompt',{}) for role in ('planner','scene')])
        thread.start(); self.addCleanup(lambda:control.cancel())
        first=control.wait_pending(timeout=2); control.approve(first)
        second=control.wait_pending(after=first,timeout=2)
        self.assertIsNotNone(second); self.assertNotEqual(first,second)
        self.assertEqual(raw.complete.call_count,1)
        with self.assertRaises(ContractError): control.approve(first)
        self.assertEqual(raw.complete.call_count,1)
        control.approve(second); thread.join(2)
        self.assertEqual(raw.complete.call_count,2)

    def test_cancel_waiting_step_makes_no_model_call(self):
        module=self.module(); control=module.ManualControl(self.root)
        raw=Mock(); client=module.ManualClient(raw,control); errors=[]
        def work():
            try: client.complete('planner','prompt',{})
            except BaseException as error: errors.append(error)
        thread=Thread(target=work); thread.start()
        self.assertIsNotNone(control.wait_pending(timeout=2))
        control.cancel(); thread.join(2)
        raw.complete.assert_not_called()
        self.assertTrue(errors and isinstance(errors[0],KeyboardInterrupt))

    def test_tool_boundary_also_waits_and_secret_values_are_redacted(self):
        module=self.module(); control=module.ManualControl(self.root,secrets=['fixture-key'])
        thread=Thread(target=lambda:control.before_tool({'event':'tool_requested','tool':'codesys',
            'action':'compile_and_deploy','password':'fixture-key'}))
        thread.start(); self.addCleanup(lambda:control.cancel())
        pending=control.wait_pending(timeout=2)
        self.assertEqual(control.step(pending)['kind'],'tool')
        self.assertNotIn('fixture-key',json.dumps(control.step(pending)))
        control.approve(pending); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_invalid_asset_id_cannot_read_arbitrary_files(self):
        module=self.module(); control=module.ManualControl(self.root)
        with self.assertRaises(ContractError): control.asset('../../config.agent.json')

    def test_waiting_for_human_does_not_exhaust_execution_budget(self):
        module=self.module()
        from factoryio_agents.execution import ExecutionBudget
        budget=ExecutionBudget(self.root,{'execution_limits':{'max_elapsed_seconds':1}})
        budget.state['started_at']-=10
        budget.exclude_human_wait(10)
        budget.check()
        self.assertEqual(budget.state['human_wait_seconds'],10)


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        import factoryio_agents
        self.assertTrue((Path(factoryio_agents.__file__).parent/'web_dashboard.py').is_file(),
                        'missing local dashboard HTTP API')
        from factoryio_agents.web_dashboard import Dashboard, create_server
        from factoryio_agents.contracts import write_json
        config={'schema_version':1,'scene_sources':[],'protocol':{'host':'127.0.0.1','port':502},'codesys':{},'factoryio':{},
                'llm':{'base_url':'http://fixture.invalid/v1','models':{'vision':'fixture-model'}}}
        write_json(self.root/'config.agent.json',config)
        source=self.root/'runs/history'
        image=source/'training-data/assets/frame.png'; image.parent.mkdir(parents=True); image.write_bytes(b'frozen fixture')
        write_json(source/'training-data/candidates/reply.json',{'candidate_id':'reply','role':'vision','status':'response_received',
            'started_at':'2026-10-12T00:00:00Z','payload':{'phase':'scene','review_task':{'prompt_id':'vision_layout'},
                'evidence':{'records':[{'id':'top','kind':'image'}]}},
            'messages':[{'role':'system','content':'Inspect the images.'}],
            'images':[{'path':'assets/frame.png','sha256':file_hash(image)}],
            'response_text':'{"checks":[]}','structured_output':{'checks':[]}})
        self.dashboard=Dashboard(self.root)
        self.server=create_server(self.dashboard,0)
        self.thread=Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(self,path):
        try:
            with self.opener.open(self.url+path) as response: return response.read()
        except urllib.error.HTTPError as error:
            error.close(); raise

    def test_history_exposes_the_saved_prompt_reply_and_exact_image_bytes(self):
        bootstrap=json.loads(self.get('/api/bootstrap')); identifier=bootstrap['history'][0]['id']
        session=json.loads(self.get('/api/sessions/'+identifier))
        self.assertTrue(session['historical'])
        step=json.loads(self.get('/api/sessions/'+identifier+'/steps/reply'))
        self.assertEqual(step['request']['system'],'Inspect the images.')
        self.assertEqual(step['raw_response'],'{"checks":[]}')
        self.assertEqual(step['request']['images'][0]['image_id'],'top')
        self.assertEqual(self.get('/api/sessions/'+identifier+'/images/reply-0'),b'frozen fixture')

    def test_history_cannot_be_released_and_missing_token_blocks_mutations(self):
        identifier=json.loads(self.get('/api/bootstrap'))['history'][0]['id']
        for token in (None,self.server.dashboard_token):
            headers={'Content-Type':'application/json'}
            if token: headers['X-Dashboard-Token']=token
            request=urllib.request.Request(self.url+'/api/sessions/'+identifier+'/approve',
                data=b'{"step_id":"reply"}',headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as caught: self.opener.open(request)
            self.assertEqual(caught.exception.code,400)
            caught.exception.close()

    def test_image_route_and_static_route_do_not_allow_path_traversal(self):
        identifier=json.loads(self.get('/api/bootstrap'))['history'][0]['id']
        with self.assertRaises(urllib.error.HTTPError):
            self.get('/api/sessions/'+identifier+'/images/..%2F..%2Fconfig.agent.json')
        with self.assertRaises(urllib.error.HTTPError): self.get('/..%2Fconfig.agent.json')

    def test_page_boots_with_step_controls_and_image_viewer(self):
        page=self.get('/').decode('utf-8')
        self.assertIn('发送图片',page); self.assertIn('执行当前步骤',page)
        self.assertIn(self.server.dashboard_token,page)
        self.assertNotIn('__TOKEN__',page)
        self.assertIn('async function loadStep',self.get('/app.js').decode('utf-8'))

    def test_official_knowledge_search_and_prefab_retrieval_are_read_only(self):
        data=json.loads(self.get('/api/knowledge?q=Remover'))
        self.assertGreaterEqual(data['stats']['chapter_count'],60)
        self.assertIn('remover',data['results'][0]['chapter_id'])
        view=json.loads(self.get('/api/knowledge?prefab=DiffusePhotoelectricSensor'))['reference']
        self.assertIn('blue',view['components'][0]['appearance']['text'].lower())
        self.assertEqual(self.dashboard.sessions,{})

    def test_ended_web_session_is_reopened_after_upgrade_without_execution_or_copy(self):
        from factoryio_agents.contracts import write_json
        identifier='abc12345'+'0'*24;source=self.root/'runs'/('web-20261012-120000-'+identifier[:8])
        write_json(source/'web-session.json',{'id':identifier,'name':'Original requirement','output':str(source),
            'mode':'run','status':'finished','result':{'status':'needs_attention','phase':'scene_geometry','error':'missing geometry'}})
        write_json(source/'manual/steps.json',{'revision':2,'steps':[{'id':'tool','kind':'tool','role':'tools',
            'number':1,'phase':'scene_load','status':'released','request':{'tool':'factoryio_bridge','images':[]}}]})
        session=json.loads(self.get('/api/sessions/'+identifier))
        self.assertEqual(session['result']['phase'],'scene_geometry')
        self.assertIsNone(session['pending_id'])
        self.assertEqual(session['steps'][0]['id'],'tool')
        self.assertFalse((source/'framework-source').exists())
        self.assertNotIn('thread',self.dashboard.get(identifier))

    def test_another_dashboards_active_session_is_not_reopened_or_released(self):
        from factoryio_agents.contracts import write_json
        identifier='def12345'+'0'*24;source=self.root/'runs'/('web-20261012-120000-'+identifier[:8])
        write_json(source/'web-session.json',{'id':identifier,'name':'Active','output':str(source),
            'mode':'run','status':'running','result':None})
        with self.assertRaises(ContractError):self.dashboard.get(identifier)
        self.assertNotIn(identifier,self.dashboard.sessions)

    def test_simple_web_task_does_not_construct_the_legacy_workflow(self):
        from factoryio_agents.training import Redactor
        fake=Mock();fake.redactor=Redactor();fake.run.return_value={'status':'scene_verified','plc_executed':False}
        with patch('factoryio_agents.web_dashboard.Workflow',side_effect=AssertionError('legacy workflow constructed')),\
             patch('factoryio_agents.simple.SimpleWorkflow',return_value=fake):
            summary=self.dashboard.create({'mode':'simple_scene','prompt':'A single conveyor'})
            self.dashboard.get(summary['id'])['thread'].join(3)
        fake.run.assert_called_once_with('A single conveyor')
        self.assertEqual(self.dashboard.get(summary['id'])['result']['status'],'scene_verified')

    def test_simple_cli_history_keeps_its_framework_and_result_without_execution(self):
        from factoryio_agents.contracts import write_json
        source=self.root/'runs/simple-history'
        (source/'training-data/candidates').mkdir(parents=True)
        write_json(source/'run.json',{'framework':'simple','status':'scene_verified','plc_executed':False})
        history=self.dashboard.scan_history()
        identifier=next(s['id'] for s in history if Path(s['output'])==source.resolve())
        with patch('factoryio_agents.simple.SimpleWorkflow',side_effect=AssertionError('must not execute')):
            summary=self.dashboard.summary(self.dashboard.get(identifier))
        self.assertEqual(summary['framework'],'simple')
        self.assertEqual(summary['result']['status'],'scene_verified')

    def test_pre_model_tool_failure_is_visible_in_simple_history(self):
        from factoryio_agents.contracts import write_json
        source=self.root/'runs/simple-tool-failure';source.mkdir(parents=True)
        write_json(source/'run.json',{'framework':'simple','status':'needs_attention','error':'Runtime Stopped'})
        event={'event':'tool_requested','timestamp':'2026-10-12T00:00:00Z','phase':'deployment_preflight',
            'tool':'codesys_services','action':'preflight'}
        (source/'trace.jsonl').write_text(json.dumps(event)+'\n',encoding='utf-8')
        identifier=next(s['id'] for s in self.dashboard.scan_history() if Path(s['output'])==source.resolve())
        session=self.dashboard.summary(self.dashboard.get(identifier))
        self.assertEqual(session['steps'][0]['kind'],'tool');self.assertEqual(session['steps'][0]['status'],'failed')
        self.assertEqual(session['result']['error'],'Runtime Stopped')


if __name__=='__main__': unittest.main()
