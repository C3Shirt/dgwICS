"""Layout review contracts, with only the remote model mocked."""
from copy import deepcopy
import json
import importlib.util
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.agents import VisionAgent
from factoryio_agents.contracts import ContractError, canonical_hash, file_hash, read_json, write_json
from factoryio_agents.coordination import make_handoff
from factoryio_agents.response_schema import review_schema
from factoryio_agents.progress import response_summary, ModelObserver, ObservedClient
from factoryio_agents.diagnostics import summarize_run
from factoryio_agents.training import RecordingClient, TrainingRecorder


class LayoutChecksTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.image=self.root/'frame.png'; self.image.write_bytes(b'mocked image')
        self.task={'task_id':'layout-test','requirement':'Place a conveyor and a feed unit.',
            'objects':[{'id':'belt','prefab':'BeltConveyor4M','purpose':'carry'},
                       {'id':'feed','prefab':'Emitter','purpose':'supply'}], 'process':[]}
        self.manifest={'objects':[{'id':o['id'],'key':o['id'],'prefab':o['prefab'],
            'position':[1,2,3]} for o in self.task['objects']]}
        self.evidence={'versions':{},'records':[{'id':'top','kind':'image',
            'path':str(self.image),'sha256':file_hash(self.image)}]}
        self.approved={'scene_goal':self.task['requirement'],'objects':[
            {'id':'belt','name':'Conveyor','type':'BeltConveyor4M','function':'Carry material',
             'expected_position':{'relation':'transport anchor'}},
            {'id':'feed','name':'Feed unit','type':'Emitter','function':'Release material',
             'expected_position':{'relative_to':'belt','relation':'above infeed'}}],
            'approval':{'reviewer':'planner','verdict':'approved','task_sha256':canonical_hash(self.task)}}
        self.reply={'checks':[self.check('belt'),self.check('feed')]}
        self.client=Mock(); self.client.complete.side_effect=lambda *args:deepcopy(self.reply)

    def check(self, object_id, status='ok'):
        return {'object_ids':[object_id],'related_object_ids':[], 'status':status,
            'observed':'Visible placement','expected':'Approved placement', 'reason':'Compared with reference',
            'evidence_images':['top'],'suggestion':'Keep placement' if status=='ok' else 'Inspect position'}

    def review(self, reference=True, handoff=None):
        self.assertIn('approved_layout',inspect.signature(VisionAgent.review).parameters,
                      'VisionAgent needs a separate approved-layout input')
        return VisionAgent(self.client).review(self.task,self.manifest,{'tags':[]},self.evidence,{},'scene',
            handoff=handoff,approved_layout=self.approved if reference else None)

    def test_vision_instructions_are_defined_in_python(self):
        self.assertIsNotNone(importlib.util.find_spec('factoryio_agents.vision_prompts'))

    def test_dynamic_goal_functions_and_relative_positions_are_in_prompt(self):
        result=self.review()
        prompt=self.client.complete.call_args.args[1]
        self.assertIn(self.task['requirement'],prompt)
        self.assertIn('Release material',prompt)
        self.assertIn('above infeed',prompt)
        self.assertNotIn('signal_contract',prompt)
        payload=self.client.complete.call_args.args[2]
        self.assertIn('layout_review',payload)
        self.assertNotIn('io_contract',payload)
        self.assertEqual(result['verdict'],'pass')

    def test_issue_has_objects_reason_and_routes_to_scene(self):
        self.reply['checks'][1]=self.check('feed','issue')
        result=self.review()
        self.assertEqual(result['verdict'],'repair')
        self.assertEqual(result['checks'][1]['reason'],'Compared with reference')
        self.assertEqual(result['findings'][0]['target'],'scene')

    def test_uncertain_blocks_pass_and_preserves_reason(self):
        self.reply['checks'][1]=self.check('feed','uncertain')
        result=self.review()
        self.assertEqual(result['verdict'],'inconclusive')
        self.assertEqual(result['findings'][0]['object_ids'],['feed'])
        self.assertTrue(result['findings'][0]['reason'])

    def test_missing_approval_never_uses_actual_positions_as_expectations(self):
        result=self.review(reference=False)
        payload=self.client.complete.call_args.args[2]
        self.assertFalse(result['layout_reference']['approved'])
        self.assertTrue(all(o['expected_position'] is None for o in payload['layout_review']['objects']))
        self.assertEqual(result['verdict'],'inconclusive')
        self.assertTrue(any(f['target']=='planner' for f in result['findings']))

    def test_unapproved_model_ok_is_uncertain_but_raw_checks_are_preserved(self):
        result=self.review(reference=False)
        self.assertTrue(all(c['status']=='uncertain' for c in result['checks']))
        self.assertEqual(result['model_checks'],self.reply['checks'])
        self.assertTrue(all(c['source']=='framework' and c['reason'] for c in result['checks']))

    def test_issue_and_uncertainty_do_not_trigger_blind_repair(self):
        self.reply['checks']=[self.check('belt','issue'),self.check('feed','uncertain')]
        self.assertEqual(self.review()['verdict'],'inconclusive')

    def test_missing_reason_unknown_object_or_missing_evidence_is_rejected(self):
        for field,bad in (('reason',''),('object_ids',['invented']),('evidence_images',['invented']),('suggestion','')):
            with self.subTest(field=field):
                self.reply={'checks':[self.check('belt'),self.check('feed','uncertain')]}
                self.reply['checks'][1][field]=bad
                with self.assertRaises(ContractError): self.review()

    def test_missing_object_check_cannot_pass(self):
        self.reply['checks']=[self.check('belt')]
        with self.assertRaises(ContractError): self.review()

    def test_stale_approval_is_rejected_before_model_call(self):
        self.approved['approval']['task_sha256']='stale'
        with self.assertRaises(ContractError): self.review()
        self.assertEqual(self.client.complete.call_count,0)

    def test_handoff_metadata_is_added_by_program(self):
        handoff=make_handoff('orchestrator','vision','scene_review',{},[],'scene-feedback.json')
        result=self.review(handoff=handoff)
        self.assertEqual(result['handoff_id'],handoff['id'])
        self.assertEqual(result['versions'],{})

    def test_layout_schema_only_asks_for_checks(self):
        payload={'phase':'scene','review_task':{'prompt_id':'vision_layout'},
            'layout_review':{'objects':[{'id':'belt'}]},
            'evidence':{'records':[{'id':'top','kind':'image'}]}}
        try: schema=review_schema(payload)
        except (KeyError,ContractError): self.fail('Layout schema must accept its compact input')
        self.assertEqual(schema['required'],['checks'])
        properties=schema['properties']['checks']['items']['properties']
        self.assertIn('reason',properties)
        self.assertNotIn('diagnosis',properties)

    def test_public_output_exposes_checks_objects_and_reasons(self):
        lines='\n'.join(response_summary('vision',{'checks':[self.check('feed','uncertain')]},{}))
        self.assertIn('feed',lines)
        self.assertIn('uncertain',lines)
        self.assertIn('Compared with reference',lines)

    def test_program_derived_feedback_is_auditable_and_tampering_is_detected(self):
        self.assertIn('approved_layout',inspect.signature(VisionAgent.review).parameters)
        recorder=TrainingRecorder(self.root/'training-data')
        feedback=VisionAgent(RecordingClient(self.client,recorder)).review(self.task,self.manifest,{'tags':[]},
            self.evidence,{},'scene',approved_layout=self.approved)
        path=self.root/'scene-000/scene-feedback.json'; write_json(path,feedback)
        report=summarize_run(self.root,{'status':'needs_attention'})
        self.assertTrue(report['artifact_authorship'][0]['matches_recorded_candidate'])
        self.assertEqual(report['artifact_authorship'][0]['assembly_mode'],'layout_checks_with_tool_metadata')
        feedback['checks'][0]['reason']='unrecorded alteration'; write_json(path,feedback)
        self.assertFalse(summarize_run(self.root,{'status':'needs_attention'})['artifact_authorship'][0]['matches_recorded_candidate'])

    def test_progress_displays_dynamic_layout_goal(self):
        events=[]
        observer=ModelObserver(self.root,events.append)
        ObservedClient(self.client,observer).complete('vision','prompt',
            {'phase':'scene','layout_review':{'scene_goal':self.task['requirement']}})
        started=next(e for e in events if e['event']=='model_started')
        self.assertEqual(started['requirement'],self.task['requirement'])

    def geometry_evidence(self):
        self.evidence['records'] += [
            {'id':'native','kind':'scene_load_receipt','receipt':{'native_geometry':{'objects':[
                {'key':'belt','bounds_world':{'center':[91,92,93]},
                 'component_geometry':{'beam_direction':[0,0,1]},'native_registered':True}]}}},
            {'id':'geometry','kind':'scene_geometry','result':{'passed':True,
                'spatial_checks_applied':False,'reason':'Native connection was not checked.',
                'actual_position_grid':[91,92,93]}}]

    def test_actual_coordinates_and_geometry_never_enter_layout_wire_request(self):
        self.geometry_evidence()
        handoff=make_handoff('orchestrator','vision','scene_review',{},[],'scene-feedback.json',
            [{'actual_position_grid':[91,92,93],'beam_direction':[0,0,1]}])
        self.review(handoff=handoff)
        _,prompt,payload,*_=self.client.complete.call_args.args
        wire=prompt+json.dumps(payload)
        for field in ('actual_objects','actual_position_grid','actual_rotation','native_geometry',
                      'bounds_world','beam_direction','scene_geometry','[91, 92, 93]'):
            self.assertNotIn(field,wire)
        self.assertNotIn('World coordinates',prompt)
        for key in ('handoff','policy','versions'): self.assertNotIn(key,payload)
        self.assertEqual(set(payload['layout_review']),{'scene_goal','objects'})
        self.assertEqual(payload['evidence']['records'],[{'id':'top','kind':'image'}])

    def test_tool_geometry_gate_stays_independent_and_auditable(self):
        self.geometry_evidence()
        recorder=TrainingRecorder(self.root/'training-data')
        feedback=VisionAgent(RecordingClient(self.client,recorder)).review(self.task,self.manifest,{'tags':[]},
            self.evidence,{},'scene',approved_layout=self.approved)
        self.assertEqual(feedback['verdict'],'inconclusive')
        self.assertTrue(any(f['target']=='tools' for f in feedback['framework_checks']))
        self.assertTrue(all(c['status']=='ok' for c in feedback['model_checks']))
        write_json(self.root/'scene-000/scene-feedback.json',feedback)
        self.assertTrue(summarize_run(self.root,{'status':'needs_attention'})['artifact_authorship'][0]['matches_recorded_candidate'])
        candidate=read_json(recorder.directory/'candidates'/(recorder.last_candidate_id+'.json'))
        self.assertTrue(all(r['kind']=='image' for r in candidate['payload']['evidence']['records']))

    def test_audit_context_is_saved_outside_model_messages(self):
        path=self.root/'task.json'; write_json(path,self.task)
        handoff=make_handoff('orchestrator','vision','scene_review',{'task':canonical_hash(self.task)},[path],
                            'scene-feedback.json',[{'actual_position_grid':[91,92,93]}])
        self.evidence['versions']={'task':canonical_hash(self.task)}
        recorder=TrainingRecorder(self.root/'training-data')
        VisionAgent(RecordingClient(self.client,recorder)).review(self.task,self.manifest,{'tags':[]},
            self.evidence,self.evidence['versions'],'scene',handoff=handoff,approved_layout=self.approved)
        candidate=read_json(recorder.directory/'candidates'/(recorder.last_candidate_id+'.json'))
        self.assertEqual(candidate['handoff_id'],handoff['id'])
        self.assertEqual(candidate['input_artifacts'],handoff['inputs'])
        self.assertEqual(candidate['recording_context']['handoff'],handoff)
        self.assertNotIn('actual_position_grid',json.dumps(candidate['messages']))
        self.assertNotIn('recording_context',candidate['payload'])

    def test_format_retry_does_not_echo_model_invented_coordinates(self):
        bad=deepcopy(self.reply); bad['checks'][0]['status']='invalid'
        bad['checks'][0]['observed']='actual_position_grid=[91,92,93]'
        self.client.complete.side_effect=[bad,deepcopy(self.reply)]
        self.review()
        retry=self.client.complete.call_args.args[2]
        self.assertNotIn('previous_response',retry['format_feedback'])
        self.assertNotIn('actual_position_grid',json.dumps(retry))


if __name__=='__main__': unittest.main()
