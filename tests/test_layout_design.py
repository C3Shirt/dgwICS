"""Upstream layout contracts and real role wrappers; injected models, no live acceptance."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.agents import PlannerAgent, SceneAgent
from factoryio_agents.contracts import ContractError, canonical_hash, read_json, write_json
from factoryio_agents.training import RecordingClient, TrainingRecorder
from factoryio_agents.vision_layout import prepare_layout_input


def proposal_fixture(task):
    ids=[o['id'] for o in task['objects']]
    return {'scene_goal':task['requirement'], 'objects':[
        {'id':o['id'],'name':o['id'],'type':o['prefab'],'function':o['purpose'],
         'expected_position':{'relative_to':[] if n==0 else [ids[0]],
             'relation':'anchor beside the work area' if n==0 else 'aligned with the first object',
             'requirements':['Support the required material path and keep service access clear.']}}
        for n,o in enumerate(task['objects'])]}


class LayoutDesignTests(unittest.TestCase):
    def setUp(self):
        self.task={'requirement':'Feed boxes onto a conveyor and remove them after exit detection.',
            'objects':[{'id':'belt','prefab':'BeltConveyor4M','purpose':'transport boxes'},
                       {'id':'feed','prefab':'Emitter','purpose':'supply one box'}],
            'process':[{'id':'feed','description':'Place a box on the belt.'}]}
        self.proposal=proposal_fixture(self.task)
        self.review={'verdict':'approved','summary':'The arrangement supports the required flow.','findings':[]}
        self.catalog={'sources':[],'templates':[{'prefab':o['prefab'],'tags':[],
            'configuration':{}} for o in self.task['objects']]}
        self.manifest={'objects':[{'id':o['id'],'prefab':o['prefab'],'position':[0,0,0]}
                                  for o in self.task['objects']]}

    def design_module(self):
        import factoryio_agents
        self.assertTrue((Path(factoryio_agents.__file__).parent/'layout_design.py').is_file(),
                        'missing upstream layout proposal and approval contract')
        from factoryio_agents import layout_design
        return layout_design

    def test_scene_role_proposes_semantic_layout_before_coordinates(self):
        self.assertTrue(callable(getattr(SceneAgent,'propose_layout',None)),
                        'SceneAgent needs a separate layout proposal subtask')
        client=Mock(); client.complete.return_value=deepcopy(self.proposal)
        value=SceneAgent(client).propose_layout(self.task,self.catalog)
        self.assertEqual(value,self.proposal)
        role,prompt,payload=client.complete.call_args.args
        self.assertEqual(role,'scene')
        self.assertEqual(payload['scene_stage'],'layout_proposal')
        self.assertIn('expected_position',prompt)
        self.assertNotIn('PLC implementation',prompt)

    def test_planner_role_reviews_the_exact_proposal(self):
        self.assertTrue(callable(getattr(PlannerAgent,'review_layout',None)),
                        'PlannerAgent needs a layout review subtask')
        client=Mock(); client.complete.return_value=deepcopy(self.review)
        result=PlannerAgent(client).review_layout(self.task,self.proposal,self.catalog)
        self.assertEqual(result,self.review)
        self.assertEqual(client.complete.call_args.args[2]['layout_proposal'],self.proposal)

    def test_approval_is_composed_from_both_roles_and_reaches_vision(self):
        design=self.design_module()
        approved=design.compose_approved_layout(self.task,self.proposal,self.review)
        layout,_=prepare_layout_input(self.task,self.manifest,{'records':[]},approved)
        self.assertTrue(layout['reference']['approved'])
        self.assertEqual(layout['objects'],self.proposal['objects'])
        self.assertEqual(approved['approval']['proposal_sha256'],design.proposal_hash(self.proposal))
        self.assertNotIn('position',approved['objects'][0])

    def test_unknown_relative_object_and_substituted_type_are_rejected(self):
        design=self.design_module()
        for field,value in [('relative_to',['invented']),('relation','')]:
            proposal=deepcopy(self.proposal); proposal['objects'][1]['expected_position'][field]=value
            with self.subTest(field=field),self.assertRaises(ContractError):
                design.validate_layout_proposal(proposal,self.task)
        proposal=deepcopy(self.proposal); proposal['objects'][1]['type']='Remover'
        with self.assertRaises(ContractError): design.validate_layout_proposal(proposal,self.task)

    def test_issue_and_uncertainty_need_object_reason_and_suggestion(self):
        design=self.design_module()
        for verdict in ('revise','uncertain'):
            review={'verdict':verdict,'summary':'Supply is disconnected.','findings':[
                {'object_ids':['feed','belt'],'reason':'The emission point misses the belt.',
                 'suggestion':'Place the emission volume above the upstream belt surface.'}]}
            design.validate_layout_review(review,self.proposal)
            for field in ('object_ids','reason','suggestion'):
                broken=deepcopy(review); broken['findings'][0].pop(field)
                with self.subTest(verdict=verdict,field=field),self.assertRaises(ContractError):
                    design.validate_layout_review(broken,self.proposal)
            with self.assertRaises(ContractError):
                design.compose_approved_layout(self.task,self.proposal,review)

    def test_modified_design_task_or_policy_cannot_reuse_approval(self):
        design=self.design_module()
        approved=design.compose_approved_layout(self.task,self.proposal,self.review)
        design.validate_approved_layout(self.task,approved,self.proposal,self.review)
        changed=deepcopy(self.task); changed['requirement']='Route to two destinations.'
        with self.assertRaises(ContractError): design.validate_approved_layout(changed,approved)
        changed=deepcopy(self.proposal); changed['objects'][1]['function']='unrelated function'
        with self.assertRaises(ContractError):
            design.validate_approved_layout(self.task,approved,changed,self.review)
        changed=deepcopy(approved); changed['approval']['layout_policy_sha256']='obsolete'
        with self.assertRaises(ContractError): design.validate_approved_layout(self.task,changed)

    def test_scene_realization_receives_approved_reference(self):
        design=self.design_module()
        approved=design.compose_approved_layout(self.task,self.proposal,self.review)
        import inspect
        self.assertIn('layout_reference',inspect.signature(SceneAgent.plan).parameters)

    def test_proposal_is_cached_when_planner_review_fails(self):
        self.assertTrue(callable(getattr(SceneAgent,'propose_layout',None)))
        from factoryio_agents.llm import ModelServiceError
        with tempfile.TemporaryDirectory() as temp:
            recorder=TrainingRecorder(Path(temp)/'training-data',source_kind='injected_client')
            client=Mock(); client.complete.side_effect=[deepcopy(self.proposal),ModelServiceError('fixture outage')]
            wrapped=RecordingClient(client,recorder)
            scene,planner=SceneAgent(wrapped),PlannerAgent(wrapped)
            scene.propose_layout(self.task,self.catalog)
            with self.assertRaises(ModelServiceError): planner.review_layout(self.task,self.proposal,self.catalog)
            before=client.complete.call_count
            self.assertEqual(scene.propose_layout(self.task,self.catalog),self.proposal)
            self.assertEqual(client.complete.call_count,before)

    def test_approval_audit_retains_raw_sources_and_rejects_tampering(self):
        design=self.design_module()
        from factoryio_agents.diagnostics import summarize_run
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); recorder=TrainingRecorder(root/'training-data',source_kind='injected_client')
            client=Mock(); client.complete.side_effect=[deepcopy(self.proposal),deepcopy(self.review)]
            wrapped=RecordingClient(client,recorder)
            proposal=SceneAgent(wrapped).propose_layout(self.task,self.catalog)
            review=PlannerAgent(wrapped).review_layout(self.task,proposal,self.catalog)
            approved=design.compose_approved_layout(self.task,proposal,review)
            for name,value in [('task',self.task),('layout-proposal',proposal),('layout-review',review),('approved-layout',approved)]:
                write_json(root/(name+'.json'),value)
            design.record_layout_approval(wrapped,self.task,proposal,review,approved)
            report=summarize_run(root,{'status':'layout_approved'})
            row=next(r for r in report['artifact_authorship'] if Path(r['path']).name=='approved-layout.json')
            self.assertTrue(row['matches_recorded_candidate'])
            self.assertEqual(row['assembly_mode'],'planner_approved_layout')
            approved['objects'][1]['expected_position']['relation']='manual replacement'
            write_json(root/'approved-layout.json',approved)
            self.assertTrue(summarize_run(root,{'status':'layout_approved'})['manual_override_detected'])

    def test_layout_progress_displays_goal_relations_and_review_findings(self):
        from factoryio_agents.progress import response_summary, STAGES, PUBLIC_FIELDS
        self.assertIn('layout_proposal',STAGES)
        self.assertIn('relation',PUBLIC_FIELDS)
        summary='\n'.join(response_summary('scene',self.proposal,{'task':self.task}))
        self.assertIn(self.proposal['scene_goal'],summary)
        self.assertIn(self.proposal['objects'][1]['expected_position']['relation'],summary)
        review={'verdict':'revise','summary':'Correct the feeder.', 'findings':[
            {'object_ids':['feed'],'reason':'Supply misses the belt.','suggestion':'Align the emission point.'}]}
        summary='\n'.join(response_summary('planner',review,{}))
        self.assertIn('feed',summary); self.assertIn(review['summary'],summary)

    def test_design_handoff_metadata_is_optional_but_conflicts_and_changed_inputs_are_rejected(self):
        self.assertTrue(callable(getattr(SceneAgent,'propose_layout',None)))
        from factoryio_agents.coordination import make_handoff
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'task.json'; write_json(path,self.task)
            packet=make_handoff('orchestrator','scene','layout_proposal',{'task':canonical_hash(self.task)},[path],
                                'layout-proposal.json',[])
            client=Mock(); client.complete.return_value=deepcopy(self.proposal)
            value=SceneAgent(client).propose_layout(self.task,self.catalog,handoff=packet)
            self.assertEqual(value,self.proposal)
            client.complete.return_value={**self.proposal,'handoff_id':'different request'}
            with self.assertRaisesRegex(ContractError,'handoff'):
                SceneAgent(client).propose_layout(self.task,self.catalog,handoff=packet)
            def mutate(*args):
                write_json(path,{'changed':True}); return deepcopy(self.proposal)
            client.complete.side_effect=mutate
            with self.assertRaisesRegex(ContractError,'input changed'):
                SceneAgent(client).propose_layout(self.task,self.catalog,handoff=packet)

    def test_scene_reference_rejects_a_different_approved_design(self):
        design=self.design_module()
        approved=design.compose_approved_layout(self.task,self.proposal,self.review)
        self.manifest['layout_reference_sha256']=canonical_hash(approved)
        changed=deepcopy(self.proposal)
        changed['objects'][1]['expected_position']['relation']='Use the opposite belt end.'
        other=design.compose_approved_layout(self.task,changed,self.review)
        with self.assertRaisesRegex(ContractError,'another approved layout'):
            prepare_layout_input(self.task,self.manifest,{'records':[]},other)

    def test_identical_review_text_cannot_be_sourced_from_another_task(self):
        design=self.design_module()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); recorder=TrainingRecorder(root/'training-data',source_kind='injected_client')
            client=Mock(); client.complete.side_effect=[deepcopy(self.proposal),deepcopy(self.review)]
            wrapped=RecordingClient(client,recorder)
            proposal=SceneAgent(wrapped).propose_layout(self.task,self.catalog)
            review=PlannerAgent(wrapped).review_layout(self.task,proposal,self.catalog)
            review_record=read_json(recorder.directory/'candidates'/(recorder.last_candidate_id+'.json'))
            unrelated=deepcopy(review_record); unrelated['candidate_id']='zz-unrelated-review'
            unrelated['payload']['task_sha256']='another task'
            write_json(recorder.directory/'candidates/zz-unrelated-review.json',unrelated)
            approved=design.compose_approved_layout(self.task,proposal,review)
            design.record_layout_approval(wrapped,self.task,proposal,review,approved)
            receipt=read_json(recorder.directory/'derived'/('layout-approval-'+canonical_hash(approved)+'.json'))
            source=next(s for s in receipt['sources'] if s['role']=='planner')
            self.assertEqual(source['candidate_id'],review_record['candidate_id'])


if __name__=='__main__': unittest.main()
