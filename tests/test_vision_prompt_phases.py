"""Verify actual VisionAgent request routing with a mocked external model only."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.agents import VisionAgent
from factoryio_agents.contracts import ContractError, file_hash
from factoryio_agents.prompts import policy_manifest


class VisionPromptPhaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        frame = Path(temporary.name)/'frame.png'
        frame.write_bytes(b'mocked image, never sent to an external model')
        self.task = {'requirement':'Transfer one box at a time from supply to removal.',
            'objects':[], 'process':[{'id':'transfer','description':'Move one box'}],
            'acceptance':[{'kind':'event_sequence','events':['emitted','removed']}]}
        self.evidence = {'versions':{}, 'records':[{'id':'image','kind':'image',
            'path':str(frame), 'sha256':file_hash(frame)}]}
        self.calls = []
        def complete(role, prompt, payload, images=None):
            self.calls.append((prompt, payload))
            if payload['phase']=='scene': return {'checks':[]}
            return {'versions':payload['versions'], 'phase':payload['phase'], 'verdict':'pass', 'findings':[]}
        self.client = Mock()
        self.client.complete.side_effect = complete
        self.agent = VisionAgent(self.client)

    def review(self, phase):
        return self.agent.review(self.task, {'objects':[]}, {'tags':[]}, self.evidence, {}, phase)

    def test_scene_and_runtime_send_different_scope_prompts(self):
        self.review('scene')
        self.review('runtime')
        layout, runtime = [call[0] for call in self.calls]
        self.assertFalse(layout == runtime, 'Layout and runtime must not share one business prompt')
        self.assertTrue(layout.isascii())
        self.assertTrue(runtime.isascii())
        self.assertIn('layout reviewer', layout)
        self.assertIn('runtime reviewer', runtime)
        self.assertNotIn('signal_contract', layout)
        self.assertNotIn('tag_rows', layout)
        self.assertIn('signal_contract', runtime)
        for _, payload in self.calls:
            goal=payload['layout_review']['scene_goal'] if payload['phase']=='scene' else payload['task']['requirement']
            self.assertEqual(goal,self.task['requirement'])

    def test_request_exposes_current_phase_and_prompt_identity(self):
        for phase, prompt_id in (('scene','vision_layout'),('runtime','vision_runtime')):
            with self.subTest(phase=phase):
                self.review(phase)
                payload = self.calls[-1][1]
                current = payload.get('review_task',{})
                self.assertEqual(current.get('phase'),phase)
                self.assertEqual(current.get('prompt_id'),prompt_id)
                self.assertTrue(current.get('objective'))

    def test_invalid_review_phase_is_rejected_before_model_call(self):
        with self.assertRaises(ContractError):
            self.review('planning')
        self.assertEqual(self.client.complete.call_count,0,'Invalid phase must not consume a model call')

    def test_visual_policies_track_each_phase_independently(self):
        hashes = policy_manifest()['prompt_sha256']
        self.assertIn('vision_layout', hashes)
        self.assertIn('vision_runtime', hashes)
        self.assertNotEqual(hashes['vision_layout'], hashes['vision_runtime'])
        self.assertIn('vision', hashes, 'Legacy role-level cache identity must remain available')


if __name__=='__main__': unittest.main()
