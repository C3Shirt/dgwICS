"""Recovery tests use recorded injected fixtures; no live software success is asserted."""
from copy import deepcopy
import contextlib
import io
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from factoryio_agents import cli
from factoryio_agents.contracts import ContractError, canonical_hash, file_hash, read_json, write_json
from factoryio_agents.diagnostics import summarize_run
from factoryio_agents.memory import ExperienceMemory, inspect_resume, planner_stage_cache, plc_io_design_hash
from factoryio_agents.supervisor import SupervisorAgent, progress_view
from factoryio_agents.training import Redactor, RecordingClient, TrainingRecorder
from factoryio_agents.workflow import Workflow
from factoryio_agents.agents import PlannerAgent, SceneAgent
from factoryio_agents.scene import build_catalog
from factoryio_agents.coordination import make_handoff
from test_coordination import source_fixture, task_fixture
from test_scene import FIXTURE
import test_workflow as workflow_fixtures


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workflow_fixtures.WorkflowTests(); self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.workflow = self.fixture.workflow
        self.root = self.fixture.root; self.source = self.workflow.output
        self.fixture.task['requirement'] = 'test'
        self.fixture.compiler.return_value.update(bindings_verified=True,task_program_verified=True)
        self.workflow.observer.review.side_effect = self.fixture.feedback()
        self.workflow.connect_runtime = Mock(side_effect=ContractError('fixture offline connection'))
        self.state = self.workflow.run('test')
        write_json(self.source / 'config.snapshot.json', self.workflow.config)
        self.record_authorship()

    def record_authorship(self):
        paths = [(self.source / 'task.json','planner')]
        paths += [(self.source/'layout-proposal.json','scene'),(self.source/'layout-review.json','planner')]
        paths += [(p,'scene') for p in self.source.glob('scene-*/scene-plan.json')]
        paths += [(p,'plc') for p in self.source.glob('scene-*/plc-*/program.json')]
        paths += [(p,'vision') for p in self.source.glob('scene-*/scene-feedback.json')]
        from factoryio_agents.layout_design import layout_policy_hash, record_layout_approval
        from factoryio_agents.vision_layout import layout_task_hash
        for n,(path,role) in enumerate(paths):
            payload={}
            if path.name=='layout-proposal.json':
                payload={'task':{k:v for k,v in read_json(self.source/'task.json').items() if k!='handoff_id'},
                         'layout_policy_sha256':layout_policy_hash()}
            if path.name=='layout-review.json':
                payload={'task':read_json(self.source/'task.json'),
                    'task_sha256':layout_task_hash(read_json(self.source/'task.json')),
                    'layout_proposal':read_json(self.source/'layout-proposal.json'),
                    'layout_policy_sha256':layout_policy_hash()}
            write_json(self.source / f'training-data/candidates/fixture-{n}.json',{
                'candidate_id':f'fixture-{n}', 'role':role, 'source_kind':'injected_client',
                'status':'response_received', 'output_origin':'serialized_client_result',
                'structured_output':read_json(path),'payload':payload})
        recorder=TrainingRecorder(self.source/'training-data',source_kind='injected_client')
        record_layout_approval(RecordingClient(Mock(),recorder),read_json(self.source/'task.json'),
            read_json(self.source/'layout-proposal.json'),read_json(self.source/'layout-review.json'),
            read_json(self.source/'approved-layout.json'))
        report = summarize_run(self.source, self.state)
        write_json(self.source / 'result.json', report)

    def test_connection_failure_reuses_design_but_reexecutes_live_operations(self):
        plan = inspect_resume(self.source,self.workflow.config)
        self.assertEqual(plan['reusable'], ['task','layout','scene','plc'])
        original = {p: file_hash(p) for p in self.source.rglob('*') if p.is_file()}
        restored = Workflow(deepcopy(self.workflow.config),self.root/'resumed',Mock())
        restored.planner,restored.author,restored.plc_agent = Mock(),Mock(),Mock()
        restored.observer = Mock()
        restored.observer.review.side_effect = self.fixture.feedback()
        restored.connect_runtime = Mock(side_effect=ContractError('fixture still offline'))
        before = self.fixture.compiler.call_count
        state = restored.resume(plan)
        self.assertEqual(state['phase'],'connection')
        restored.planner.plan.assert_not_called(); restored.author.plan.assert_not_called()
        restored.author.propose_layout.assert_not_called(); restored.planner.review_layout.assert_not_called()
        restored.plc_agent.program.assert_not_called()
        self.assertEqual(self.fixture.compiler.call_count,before+1)
        self.assertEqual(restored.observer.review.call_count,1)
        restored.connect_runtime.assert_called_once()
        self.assertEqual(original, {p:file_hash(p) for p in original})
        self.assertFalse(summarize_run(restored.output,state)['live_pipeline_passed'])
        self.assertFalse((restored.output/'scene-000/plc-000/acceptance.json').exists())

    def test_additional_supervisor_role_keeps_required_roles_and_authorship_audit(self):
        investigation = {'verdict':'repair','fixture':'recorded supervisor investigation'}
        path = Path(self.state['plc_dir'])/'runtime-investigation.json'
        write_json(path,investigation)
        write_json(self.source/'training-data/candidates/fixture-supervisor.json',{
            'candidate_id':'fixture-supervisor','role':'supervisor','source_kind':'injected_client',
            'status':'response_received','output_origin':'serialized_client_result',
            'structured_output':investigation})
        report = summarize_run(self.source,self.state)
        self.assertTrue(report['completion_checks']['all_required_artifact_roles_present'])
        self.assertFalse(report['manual_override_detected'])
        row = next(r for r in report['artifact_authorship'] if r['producer_role']=='supervisor')
        self.assertTrue(row['matches_recorded_candidate'])
        self.assertFalse(row['matches_live_provider_candidate'])
        self.assertFalse(report['completion_checks']['artifacts_match_live_provider_replies'])
        self.assertFalse(report['live_pipeline_passed'])

    def test_unattributed_supervisor_artifact_still_blocks_provenance(self):
        write_json(Path(self.state['plc_dir'])/'runtime-investigation.json',{
            'verdict':'repair','fixture':'unattributed supervisor investigation'})
        report = summarize_run(self.source,self.state)
        self.assertTrue(report['completion_checks']['all_required_artifact_roles_present'])
        self.assertTrue(report['manual_override_detected'])
        row = next(r for r in report['artifact_authorship'] if r['producer_role']=='supervisor')
        self.assertFalse(row['matches_recorded_candidate'])
        self.assertFalse(report['completion_checks']['artifacts_match_live_provider_replies'])
        self.assertFalse(report['live_pipeline_passed'])

    def test_old_live_acceptance_and_connection_receipts_are_not_restored(self):
        plc = Path(self.state['plc_dir'])
        write_json(plc/'acceptance.json', {'passed':True,'scope':'stale fixture'})
        write_json(plc/'connection-receipt.json', {'connection_confirmed':True})
        from factoryio_agents.memory import restore_resume
        output = self.root/'child'
        restore_resume(inspect_resume(self.source,self.workflow.config), output)
        self.assertFalse((output/'scene-000/plc-000/acceptance.json').exists())
        self.assertFalse((output/'scene-000/plc-000/connection-receipt.json').exists())

    def test_relocated_manifest_satisfies_loader_path_gate_without_scene_or_io_changes(self):
        from factoryio_agents.memory import restore_resume
        output = self.root/'child'
        before = {name:file_hash(self.source/'scene-000'/name) for name in
                  ('scene.factoryio','scene-plan.json','io-contract.json')}
        restore_resume(inspect_resume(self.source,self.workflow.config),output)
        scene = (output/'scene-000/scene.factoryio').resolve()
        for name in ('manifest.json','structural-validation.json'):
            self.assertEqual(Path(read_json(output/'scene-000'/name)['scene_path']).resolve(),scene)
            self.assertEqual(Path(read_json(self.source/'scene-000'/name)['scene_path']).resolve(),
                             (self.source/'scene-000/scene.factoryio').resolve())
        self.assertEqual(before,{name:file_hash(output/'scene-000'/name) for name in before})
        self.assertFalse(read_json(output/'memory/relocation.json')['scene_bytes_changed'])

    def test_modified_task_or_scene_is_rejected(self):
        path = self.source/'scene-000/scene.factoryio'; path.write_text('tampered fixture')
        with self.assertRaisesRegex(ContractError,'modified|changed'):
            inspect_resume(self.source,self.workflow.config)

    def test_checkpoint_blocks_io_edits_even_with_new_versions(self):
        path = self.source/'scene-000/io-contract.json'
        value = read_json(path); value['unknown_manual_change']=True; write_json(path,value)
        with self.assertRaisesRegex(ContractError,'checkpoint artifact changed'):
            inspect_resume(self.source,self.workflow.config)

    def test_plc_prompt_change_invalidates_only_plc(self):
        state = read_json(self.source/'run.json')
        state['policy']['prompt_sha256']['plc']='newer prompt needed'
        write_json(self.source/'run.json',state)
        self.assertEqual(inspect_resume(self.source,self.workflow.config)['reusable'],['task','layout','scene'])

    def test_scene_change_invalidates_scene_and_plc_but_keeps_task(self):
        state = read_json(self.source/'run.json'); state['policy']['prompt_sha256']['scene']='old policy'
        write_json(self.source/'run.json',state)
        self.assertEqual(inspect_resume(self.source,self.workflow.config)['reusable'],['task','layout'])

    def test_transport_change_invalidates_design(self):
        config=deepcopy(self.workflow.config); config['protocol']['port']+=1
        self.assertEqual(inspect_resume(self.source,config)['reusable'],[])

    def test_layout_policy_change_keeps_task_but_invalidates_layout_scene_and_plc(self):
        state=read_json(self.source/'run.json')
        state['policy']['prompt_sha256']['planner_layout_review']='old review policy'
        write_json(self.source/'run.json',state)
        plan=inspect_resume(self.source,self.workflow.config)
        self.assertEqual(plan['reusable'],['task'])
        self.assertIn('layout proposal or review policy changed',plan['invalidation_reasons'])

    def test_modified_approved_layout_is_rejected_by_checkpoint(self):
        path=self.source/'approved-layout.json'
        value=read_json(path); value['objects'][0]['function']='manual substitution'; write_json(path,value)
        with self.assertRaisesRegex(ContractError,'modified|changed'):
            inspect_resume(self.source,self.workflow.config)

    def test_explicit_plc_restart_preserves_task_and_scene(self):
        self.assertEqual(inspect_resume(self.source,self.workflow.config,'plc')['reusable'],['task','layout','scene'])

    def test_latest_restart_repair_invalidates_plc_despite_old_runtime_pass(self):
        plc=Path(self.state['plc_dir'])
        for name,value in [('runtime-feedback.json',{'verdict':'pass','findings':[]}),
                           ('restart-feedback.json',{'verdict':'repair','findings':[{'target':'plc'}]})]:
            write_json(plc/name,value)
            write_json(self.source/('training-data/candidates/'+name),{'candidate_id':name,'role':'vision',
                'source_kind':'injected_client','status':'response_received','structured_output':value})
        write_json(plc/'live-handoff/receipt.json',{'passed':False})
        plan=inspect_resume(self.source,self.workflow.config)
        self.assertEqual(plan['reusable'],['task','layout','scene'])
        self.assertEqual(plan['latest_runtime_review']['stage'],'live_handoff')

    def test_inconclusive_plc_hypothesis_preserves_code_for_measurement(self):
        plc=Path(self.state['plc_dir'])
        feedback={'verdict':'inconclusive','findings':[{'target':'plc'}]}
        write_json(plc/'runtime-feedback.json',feedback)
        write_json(self.source/'training-data/candidates/runtime-review.json',{'candidate_id':'review','role':'vision',
            'source_kind':'injected_client','status':'response_received','structured_output':feedback})
        self.assertEqual(inspect_resume(self.source,self.workflow.config)['reusable'],['task','layout','scene','plc'])

    def test_restored_design_survives_new_environment_gate_failure(self):
        config=deepcopy(self.workflow.config);config['codesys']['ensure_local_services']=True
        plan=inspect_resume(self.source,config)
        output=self.root/'restored-environment-failure'
        restored=Workflow(config,output,Mock())
        with patch('factoryio_agents.codesys_services.service_control',return_value={
                'status':'error','service_running':False,'windows_error':5}):
            state=restored.resume(plan)
        write_json(output/'config.snapshot.json',config)
        second=inspect_resume(output,config)
        self.assertEqual(second['reusable'],['task','layout','scene','plc'])
        self.assertEqual(restored.budget.state['calls'],[])
        self.assertEqual(state['status'],'needs_configuration')

    def test_passed_source_requires_explicit_revalidation_and_preserves_live_gates(self):
        report = summarize_run(self.source,self.state)
        report.update(live_pipeline_passed=True,failure=None)
        before = {p:file_hash(p) for p in self.source.rglob('*') if p.is_file()}
        # Injected status exercises the entry gate; it does not claim real software passed.
        with patch('factoryio_agents.diagnostics.summarize_run',return_value=report):
            with self.assertRaisesRegex(ContractError,'revalidate'):
                inspect_resume(self.source,self.workflow.config)
            plan = inspect_resume(self.source,self.workflow.config,revalidate=True)
        self.assertEqual(plan['purpose'],'revalidation')
        self.assertTrue(plan['source_live_pipeline_passed_at_inspection'])
        self.assertEqual(plan['reusable'],['task','layout','scene','plc'])
        for phase in ('scene_load','compile_and_deploy','connection','runtime_capture',
                      'runtime_acceptance','runtime_visual_review'):
            self.assertIn(phase,plan['live_steps_reexecuted'])
        self.assertEqual(before,{p:file_hash(p) for p in before})

    def test_revalidation_cli_inspection_is_read_only(self):
        config = deepcopy(self.workflow.config); config['schema_version']=1
        config_path = self.root/'config-revalidation.json'; write_json(config_path,config)
        with (patch.object(cli,'protected_credentials') as credentials,patch.object(cli,'Workflow') as workflow,
              contextlib.redirect_stdout(io.StringIO())):
            code=cli.main(['--resume',str(self.source),'--revalidate','--resume-inspect',
                           '--config',str(config_path),'--out',str(self.root/'revalidation-inspection')])
        self.assertEqual(code,0); credentials.assert_not_called(); workflow.assert_not_called()
        self.assertEqual(read_json(self.root/'revalidation-inspection/resume-plan.json')['purpose'],'revalidation')

    def test_source_template_change_blocks_resume(self):
        Path(self.workflow.config['scene_sources'][0]).write_text('different template')
        with self.assertRaisesRegex(ContractError,'template changed'):
            inspect_resume(self.source,self.workflow.config)

    def test_live_writer_cannot_be_resumed(self):
        (self.source/'.writer.lock').write_text('999999')
        with self.assertRaisesRegex(ContractError,'writer'): inspect_resume(self.source,self.workflow.config)

    def test_read_only_cli_inspection_does_not_decrypt_credentials_or_create_workflow(self):
        config = deepcopy(self.workflow.config); config['schema_version']=1
        config_path = self.root/'config.json'; write_json(config_path,config)
        with (patch.object(cli,'protected_credentials') as credentials,patch.object(cli,'Workflow') as workflow,
              contextlib.redirect_stdout(io.StringIO())):
            code=cli.main(['--resume',str(self.source),'--resume-inspect','--config',str(config_path),
                           '--out',str(self.root/'inspection')])
        self.assertEqual(code,0); credentials.assert_not_called(); workflow.assert_not_called()
        self.assertEqual(read_json(self.root/'inspection/resume-plan.json')['reusable'],['task','layout','scene','plc'])


class MemoryAndSupervisorTests(unittest.TestCase):
    def test_plc_design_equivalence_ignores_only_renewed_native_receipts(self):
        original = {'tags':[{'key':'guid-1','index':0,'kind':'input'}],
            'component_semantics':[{'tag_key':'guid-1','app_version_confirmed':True,
                'semantics':{'pressed_value':False},'evidence':['old/receipt.json'],
                'bridge_response_sha256':'old-hash'}]}
        fresh = deepcopy(original)
        fresh['component_semantics'][0].update(evidence=['new/receipt.json'],bridge_response_sha256='new-hash')
        self.assertEqual(plc_io_design_hash(original),plc_io_design_hash(fresh))
        self.assertNotEqual(canonical_hash(original),canonical_hash(fresh))
        for field,value in [('semantics',{'pressed_value':True}),('tag_key','different-guid'),
                            ('app_version_confirmed',False)]:
            changed = deepcopy(fresh); changed['component_semantics'][0][field] = value
            self.assertNotEqual(plc_io_design_hash(original),plc_io_design_hash(changed))
        changed = deepcopy(fresh); changed['tags'][0]['index']=1
        self.assertNotEqual(plc_io_design_hash(original),plc_io_design_hash(changed))
        self.assertEqual(original['component_semantics'][0]['evidence'],['old/receipt.json'])

    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.root=Path(tmp.name)

    def test_experience_retrieval_is_requirement_scoped_and_redacted(self):
        store=ExperienceMemory(self.root/'experiences',Redactor(['fixture-secret']))
        result={'failure':{'category':'software_or_adapter','phase':'connection','active_role':'tools',
                          'observed_error':'connection failed fixture-secret','root_cause_confirmed':False}}
        for n,requirement in enumerate(('scenario A','scenario B')):
            output=self.root/f'run-{n}'; write_json(output/'result.json',result)
            store.remember(output,requirement,result)
        rows=store.retrieve('scenario A'); self.assertEqual(len(rows),1)
        self.assertNotIn('fixture-secret',str(rows)); self.assertFalse(rows[0]['failure']['root_cause_confirmed'])
        write_json(self.root/'run-0/result.json',{'changed':True})
        self.assertEqual(store.retrieve('scenario A'),[])

    def test_planner_fragment_cache_revalidates_and_retains_original_candidate(self):
        recorder=TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        raw=Mock(); raw.complete.return_value={'handoff_id':'old-id','signals':{'sensor':{'index':0}}}
        client=RecordingClient(raw,recorder)
        value=client.complete('planner','fixture prompt',{})
        original=(recorder.directory/'candidates'/f'{recorder.last_candidate_id}.json').read_bytes()
        inputs={'requirement':'same','planning_stage':'measurement_signals'}
        check=Mock()
        planner_stage_cache(recorder,'measurement_signals',inputs,check,value)
        cached,ref=planner_stage_cache(recorder,'measurement_signals',inputs,check)
        self.assertEqual(cached,value); check.assert_called_once_with(value)
        self.assertEqual(raw.complete.call_count,1)
        self.assertEqual(original,(recorder.directory/'candidates'/f'{ref["candidate_id"]}.json').read_bytes())
        self.assertIsNone(planner_stage_cache(recorder,'measurement_signals',{'requirement':'changed'},check))

    def test_cached_fragment_tampering_is_rejected(self):
        recorder=TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        raw=Mock(); raw.complete.return_value={'handoff_id':'old-id','execution_contract':{}}
        value=RecordingClient(raw,recorder).complete('planner','fixture',{})
        planner_stage_cache(recorder,'execution',{},Mock(),value)
        path=recorder.directory/'candidates'/f'{recorder.last_candidate_id}.json'
        candidate=read_json(path); candidate['structured_output']['changed']=True; write_json(path,candidate)
        with self.assertRaisesRegex(ContractError,'candidate changed'):
            planner_stage_cache(recorder,'execution',{},Mock())

    def test_failed_fourth_planning_stage_resumes_without_repeating_first_three(self):
        source=self.root/'source.factoryio'; source.write_text(source_fixture(),encoding='utf-8')
        catalog=build_catalog([source]); task=task_fixture(); task['handoff_id']='old-task-handoff'
        spec={k:v for k,v in task.items() if k not in ('acceptance','execution_contract')}
        signals={'motor':{'object_id':'conveyor','kind':'output','index':0,'meaning':'raw'}}
        client=Mock(); client.complete.side_effect=[spec,{'handoff_id':'old','execution_contract':task['execution_contract']},
            {'handoff_id':'old','signals':signals},*([{'handoff_id':'old','probes':'invalid'}]*3)]
        recorder=TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        with self.assertRaises(ContractError):
            PlannerAgent(RecordingClient(client,recorder),True,True).plan(task['requirement'],catalog,task['protocol'])
        self.assertEqual(client.complete.call_count,6)
        child=self.root/'child'; shutil.copytree(self.root/'training-data',child/'training-data')
        shutil.copytree(self.root/'memory',child/'memory')
        new_client=Mock(); new_client.complete.side_effect=[{'handoff_id':'new','probes':[]},
            {'handoff_id':'new','verification_tasks':[],'checks':[{'id':'cycle','process_ids':['feed','exit'],
             'kind':'sequence','steps':[{'signal':'motor','edge':'rising'},{'signal':'motor','edge':'falling'}],'min_matches':1}]}]
        new_recorder=TrainingRecorder(child/'training-data',source_kind='injected_client')
        result=PlannerAgent(RecordingClient(new_client,new_recorder),True,True).plan(task['requirement'],catalog,task['protocol'])
        self.assertEqual(new_client.complete.call_count,2)
        write_json(child/'task.json',result)
        audit=summarize_run(child,{'status':'planning'})['artifact_authorship'][0]
        self.assertTrue(audit['matches_recorded_candidate']); self.assertEqual(audit['assembly_mode'],'five_model_fragments')
        self.assertFalse(audit['matches_live_provider_candidate'])

    def test_scene_configuration_failure_keeps_accepted_layout(self):
        source=self.root/'source.factoryio'; source.write_text(FIXTURE,encoding='utf-8')
        catalog=build_catalog([source]); template=catalog['templates'][0]
        task={'task_id':'mock','objects':[{'id':'sensor','prefab':template['prefab']}],'process':[]}
        layout={'scene_id':'mock','base_source_id':template['source_id'],'objects':[
            {'id':'sensor','template_id':template['template_id'],'position':[1,2,3],'yaw_deg':90}]}
        bad=deepcopy(layout); bad['objects'][0]['position'][0]+=1
        client=Mock(); client.complete.side_effect=[layout,bad,bad,bad]
        recorder=TrainingRecorder(self.root/'training-data',source_kind='injected_client')
        with self.assertRaises(ContractError): SceneAgent(RecordingClient(client,recorder),staged=True).plan(task,catalog)
        self.assertEqual(client.complete.call_count,4)
        client.complete.side_effect=None; client.complete.return_value=layout
        self.assertEqual(SceneAgent(RecordingClient(client,recorder),staged=True).plan(task,catalog),layout)
        self.assertEqual(client.complete.call_count,5)
        self.assertEqual(client.complete.call_args.args[2]['scene_stage'],'configuration_io')

    def test_failure_memory_is_delivered_only_to_its_responsible_role(self):
        workflow=Workflow({'llm':{},'codesys':{},'factoryio':{},'scene_sources':[]},self.root,Mock())
        workflow.memory_context={'observations':[{'kind':'observed_failure','failure':{'active_role':'tools','target':'tools'}},
            {'kind':'observed_failure','failure':{'active_role':'planner','target':None,'observed_error':'capacity exceeded'}}]}
        packet=workflow.handoff('planner','planning',[],'task.json')
        self.assertEqual(len(packet['memory']['observations']),1)
        self.assertEqual(packet['memory']['observations'][0]['failure']['active_role'],'planner')
        self.assertEqual(packet['id'],canonical_hash({k:v for k,v in packet.items() if k!='id'}))

    def test_supervisor_routes_connection_to_tools_and_cannot_claim_done(self):
        state={'status':'needs_attention','phase':'connection','active_role':'tools'}
        report={'live_pipeline_passed':False,'failure':{'category':'software_or_adapter','target':'tools'}}
        view=progress_view(state,report,{'reusable':['task','scene','plc']})
        self.assertEqual({r['role'] for r in view['allowed_actions']},{'tools'})
        raw=Mock(); raw.complete.return_value={'schema_version':1,'decision':'done','next_role':'none',
                                             'reason':'unsupported success','evidence_ids':['result']}
        supervisor=SupervisorAgent({'llm':{}},self.root,raw)
        with self.assertRaisesRegex(ContractError,'allowed'): supervisor.decide(view)
        self.assertTrue(list((self.root/'training-data/candidates').glob('*.json')))

    def test_supervisor_decision_needs_real_input_evidence(self):
        view=progress_view({'status':'failed','phase':'planning','active_role':'planner'},
                           {'live_pipeline_passed':False,'failure':{}},{'reusable':[]})
        # Ambiguous actions still require a model decision with real evidence IDs.
        view['allowed_actions'].append({'action':'investigate_tools','role':'tools'})
        raw=Mock(); raw.complete.return_value={'schema_version':1,'decision':'replan','next_role':'planner',
                                             'reason':'spec failed','evidence_ids':['invented']}
        supervisor=SupervisorAgent({'llm':{}},self.root,raw)
        with self.assertRaisesRegex(ContractError,'unknown evidence'): supervisor.decide(view)
        raw.complete.return_value['evidence_ids']=['result']
        self.assertEqual(supervisor.decide(view)['decision'],'replan')

    def test_runtime_investigation_requires_source_and_tags_and_cannot_pass(self):
        task={'process':[{'id':'exit'}]}; manifest={'objects':[{'id':'belt'}]}
        contract={'tags':[{'key':'motor'}]}; program={'implementation':'fixture source'}
        versions={'task':canonical_hash(task),'io':canonical_hash(contract),'plc':canonical_hash(program)}
        handoff=make_handoff('orchestrator','supervisor','runtime_investigation',versions,[],'runtime-investigation.json',[])
        evidence={'versions':versions,'records':[{'id':'measured','kind':'tag_sample','tags':[]}]}
        finding={'category':'logic','target':'plc','object_ids':['belt'],'observed':'mock measured stop',
            'expected':'mock continue','evidence_ids':['measured','current-plc-source'],'confidence':.9,
            'suggestion':'repair the supported control path','diagnosis':{'process_step':'exit','signal_keys':['motor'],
                'causal_status':'supported','hypothesis':'mock flag persists','alternatives':['mock timer'],
                'discriminating_check':'mock source plus trace','expected_result':'mock motor continues',
                'repair_scope':['mock flag lifecycle'],'retest':['mock next cycle']}}
        value={'handoff_id':handoff['id'],'versions':versions,'phase':'runtime','verdict':'repair','findings':[finding]}
        raw=Mock(); raw.complete.return_value=deepcopy(value)
        agent=SupervisorAgent({'llm':{}},self.root,raw)
        self.assertEqual(agent.investigate(task,manifest,contract,evidence,versions,program,{},handoff)['verdict'],'repair')
        self.assertEqual(raw.complete.call_args.args[0],'supervisor')
        self.assertEqual(raw.complete.call_args.args[2]['plc']['implementation'],'fixture source')
        raw.complete.return_value['findings'][0]['evidence_ids']=['measured']
        with self.assertRaisesRegex(ContractError,'current source'):
            agent.investigate(task,manifest,contract,evidence,versions,program,{},handoff)
        raw.complete.return_value=deepcopy(value)
        raw.complete.return_value['findings'][0]['diagnosis']['causal_status']='hypothesis'
        with self.assertRaisesRegex(ContractError,'investigation'):
            agent.investigate(task,manifest,contract,evidence,versions,program,{},handoff)
        raw.complete.return_value={**value,'verdict':'pass','findings':[]}
        with self.assertRaisesRegex(ContractError,'cannot declare a pass'):
            agent.investigate(task,manifest,contract,evidence,versions,program,{},handoff)


if __name__ == '__main__': unittest.main()
