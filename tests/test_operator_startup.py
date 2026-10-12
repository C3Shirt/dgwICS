from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from factoryio_agents.contracts import ContractError,write_json
from factoryio_agents.runtime import operator_startup


class OperatorStartupTests(unittest.TestCase):
    def setUp(self):
        self.manifest={'objects':[{'id':'reset','key':'r','prefab':'ResetPushButton'},
                                  {'id':'start','key':'s','prefab':'StartPushButton'}]}
        self.client=Mock();self.client.tags.return_value=[]
        self.evidence={'records':[]}

    def test_only_native_buttons_are_requested_and_release_always_follows_press(self):
        calls=[]
        def hook(command,values,timeout):
            calls.append((values['object_key'],values['pressed']))
            write_json(Path(values['output']),{'ok':True,'data':{'operator_button':{'object_key':values['object_key'],
                'pressed':values['pressed']=='true','native_input':True}}})
        with tempfile.TemporaryDirectory() as tmp,patch('factoryio_agents.runtime.run_hook',side_effect=hook),patch('factoryio_agents.runtime.time.sleep'):
            operator_startup({'operator_command':['native']},self.manifest,Path(tmp),self.evidence,self.client,0,{'tags':[]})
        self.assertEqual(calls,[('r','true'),('r','false'),('s','true'),('s','false')])
        self.assertEqual(len([r for r in self.evidence['records'] if r['kind']=='tag_sample']),4)
        self.assertTrue(all(r['receipt_sha256'] for r in self.evidence['records'] if r['kind']=='operator_action'))

    def test_ambiguous_buttons_are_rejected_before_any_native_action(self):
        self.manifest['objects'].append(self.manifest['objects'][0])
        with tempfile.TemporaryDirectory() as tmp,patch('factoryio_agents.runtime.run_hook') as hook:
            with self.assertRaisesRegex(ContractError,'exactly one ResetPushButton'):
                operator_startup({},self.manifest,Path(tmp),self.evidence,self.client,0,{'tags':[]})
            hook.assert_not_called()

    def test_button_is_released_even_when_pressed_receipt_is_invalid(self):
        calls=[]
        def hook(command,values,timeout):
            calls.append(values['pressed'])
            write_json(Path(values['output']),{'ok':True,'data':{'operator_button':{'object_key':values['object_key'],
                'pressed':values['pressed']=='true','native_input':values['pressed']=='false'}}})
        with tempfile.TemporaryDirectory() as tmp,patch('factoryio_agents.runtime.run_hook',side_effect=hook):
            with self.assertRaisesRegex(ContractError,'did not confirm'):
                operator_startup({'operator_command':['native']},self.manifest,Path(tmp),self.evidence,self.client,0,{'tags':[]})
        self.assertEqual(calls,['true','false'])


if __name__=='__main__':unittest.main()
