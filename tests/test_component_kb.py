"""Official document retrieval and actual role-message integration, without model services."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.contracts import ContractError, file_hash
from factoryio_agents.agents import complete_role, VisionAgent


class ComponentKnowledgeTests(unittest.TestCase):
    def kb(self):
        from factoryio_agents.component_kb import KnowledgeBase
        return KnowledgeBase()

    def test_official_text_and_image_observations_keep_separate_provenance(self):
        card=self.kb().lookup('DiffusePhotoelectricSensor')
        self.assertIn('solid',card['function'].lower())
        self.assertIn('blue',card['appearance']['text'].lower())
        self.assertEqual(card['appearance']['basis'],'official_image_observation')
        self.assertTrue(card['chapter_ids'])
        for identifier in card['chapter_ids']:
            self.assertTrue(self.kb().chapter(identifier)['url'].startswith('https://docs.factoryio.com/'))

    def test_remover_definition_is_volume_intersection_and_not_mechanical_grasping(self):
        card=self.kb().lookup('Remover')
        self.assertIn('intersect',card['function'].lower())
        self.assertNotIn('gripper',card['function'].lower())
        self.assertNotIn('push',card['function'].lower())

    def test_two_sensor_objects_retrieve_one_type_reference_and_exclude_unused_types(self):
        from factoryio_agents.component_kb import references_for_payload
        view=references_for_payload({'layout_review':{'objects':[
            {'id':'entry','type':'DiffusePhotoelectricSensor'},
            {'id':'exit','type':'DiffusePhotoelectricSensor'},
            {'id':'sink','type':'Remover'}]}},'vision')
        self.assertEqual(len(view['components']),2)
        self.assertEqual(set(view['requested_prefabs']),{'DiffusePhotoelectricSensor','Remover'})
        self.assertNotIn('PopUpWheelSorter',json.dumps(view))
        self.assertNotIn('body',json.dumps(view))
        self.assertNotIn('documented_range_m',json.dumps(view))

    def test_task_objects_take_priority_over_a_large_catalog(self):
        from factoryio_agents.component_kb import references_for_payload
        payload={'task_spec':{'objects':[{'id':'sink','prefab':'Remover'}]},
            'catalog':{'prefabs':[{'prefab':'Remover'},{'prefab':'ConveyorScale'}]}}
        view=references_for_payload(payload,'planner')
        self.assertEqual(view['requested_prefabs'],['Remover'])
        self.assertNotIn('appearance',view['components'][0])

    def test_unknown_types_remain_explicitly_unknown(self):
        from factoryio_agents.component_kb import references_for_payload
        self.assertIsNone(self.kb().lookup('InventedGripper'))
        view=references_for_payload({'task':{'objects':[{'id':'x','prefab':'InventedGripper'}]}},'scene')
        self.assertEqual(view['missing_prefabs'],['InventedGripper'])
        self.assertEqual(view['components'],[])

    def test_exact_official_component_names_work_without_fuzzy_type_substitution(self):
        self.assertIn('pneumatic',self.kb().lookup('Pusher')['function'].lower())
        self.assertIsNone(self.kb().lookup('PusherWithImaginedGripper'))

    def test_stale_archived_approval_can_be_compared_but_cannot_be_promoted_to_current_approval(self):
        from factoryio_agents.layout_design import compose_approved_layout
        from factoryio_agents.vision_layout import prepare_layout_input
        task={'requirement':'Remove a box','objects':[{'id':'sink','prefab':'Remover'}]}
        proposal={'scene_goal':'Remove a box','objects':[{'id':'sink','name':'Sink','type':'Remover',
            'function':'Remove arriving boxes','expected_position':{'relative_to':[],
            'relation':'At the exit','requirements':['Cover the arrival path']}}]}
        approved=compose_approved_layout(task,proposal,{'verdict':'approved','summary':'Accept','findings':[]},policy_sha256='old')
        manifest={'objects':[{'id':'sink','prefab':'Remover'}]}
        with self.assertRaisesRegex(ContractError,'policy changed'):
            prepare_layout_input(task,manifest,{'records':[]},approved)
        view,_=prepare_layout_input(task,manifest,{'records':[],'review_context':{'archived':True}},approved)
        self.assertFalse(view['reference']['approved'])
        self.assertIn('current planner approval',view['reference']['reason'])

    def test_knowledge_retrieval_progress_reports_selected_types_without_a_model_call(self):
        from factoryio_agents.progress import ModelObserver
        with tempfile.TemporaryDirectory() as directory:
            events=[];observer=ModelObserver(Path(directory),events.append)
            observer.training_event({'event':'component_knowledge_retrieved','details':{'role':'vision',
                'requested_prefabs':['DiffusePhotoelectricSensor'],'component_ids':['diffuse'],
                'missing_prefabs':[],'knowledge_sha256':'fixture'}})
            self.assertEqual(events[0]['active_role'],'vision')
            self.assertIn('1 种组件',events[0]['text'])
            self.assertEqual(observer.count,0)

    def test_keyword_search_resolves_chinese_aliases_and_returns_source_chapters(self):
        rows=self.kb().search('移除器',limit=3)
        self.assertTrue(rows)
        self.assertIn('remover',rows[0]['chapter_id'])
        self.assertIn('intersect',rows[0]['excerpt'].lower())
        self.assertLessEqual(len(rows),3)

    def test_snapshot_and_cards_are_part_of_the_prompt_policy_identity(self):
        from factoryio_agents.prompts import policy_manifest
        self.assertEqual(policy_manifest()['official_knowledge_sha256'],self.kb().identity)
        self.assertGreaterEqual(self.kb().stats()['source_count'],10)
        self.assertGreaterEqual(self.kb().stats()['chapter_count'],60)

    def test_complete_role_injects_tool_owned_scoped_knowledge_and_ignores_supplied_fake_facts(self):
        raw=Mock();raw.complete.return_value={}
        payload={'task':{'objects':[{'id':'sink','prefab':'Remover'}]},
            'component_reference':{'components':[{'function':'A gripper grabs boxes'}]}}
        complete_role(raw,'scene','Scene role',payload)
        sent=raw.complete.call_args.args[2]
        self.assertIn('intersect',sent['component_reference']['components'][0]['function'].lower())
        self.assertNotIn('A gripper grabs boxes',json.dumps(sent))
        self.assertEqual(sent['component_knowledge']['rules'],[])
        self.assertEqual(payload['component_reference']['components'][0]['function'],'A gripper grabs boxes')

    def test_actual_vision_request_keeps_screenshots_as_scene_evidence_and_adds_deduplicated_references(self):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'frame.png';image.write_bytes(b'fixture image')
            task={'requirement':'Detect boxes','objects':[{'id':'sensor','prefab':'DiffusePhotoelectricSensor',
                'purpose':'Confirm arrival'}],'process':[]}
            evidence={'versions':{},'records':[{'id':'top','kind':'image','path':str(image),'sha256':file_hash(image)}]}
            reply={'checks':[{'object_ids':['sensor'],'related_object_ids':[],'status':'uncertain',
                'observed':'A small object is visible','expected':'Sensor beside the line','reason':'No clear view',
                'suggestion':'Capture a close view','evidence_images':['top']}]}
            raw=Mock();raw.complete.return_value=deepcopy(reply)
            VisionAgent(raw).review(task,{'objects':[{'id':'sensor','prefab':'DiffusePhotoelectricSensor',
                'position':[91,92,93]}]},{'tags':[]},evidence,{},'scene')
            _,system,payload,images=raw.complete.call_args.args
            self.assertEqual(payload['evidence']['records'],[{'id':'top','kind':'image'}])
            self.assertEqual(images,[image])
            self.assertIn('blue',payload['component_reference']['components'][0]['appearance']['text'].lower())
            self.assertNotIn('[91, 92, 93]',json.dumps(payload))
            self.assertNotIn('position',payload['layout_review']['objects'][0])
            self.assertNotIn('blue rectangular',system.lower())


if __name__=='__main__': unittest.main()
