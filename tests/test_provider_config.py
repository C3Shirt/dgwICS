"""Provider routing tests use fake credentials and HTTP responses only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factoryio_agents.llm import ModelClient
from factoryio_agents.provider_config import load_providers
from factoryio_agents.contracts import ContractError


class ProviderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'models.yaml'
        self.path.write_text('Text:\n  Base_URL: https://text.example/v1\n  Model_Name: text-model\n  API_Key: fake-text-key\n'
                             'Vision:\n  Base_URL: https://vision.example/v1\n  Model_Name: image-model\n  API_Key: fake-image-key\n', encoding='utf-8')

    def test_role_routes_model_endpoint_and_key_together(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps({'choices': [{'message': {'content': '{}'}}]}).encode()
        client = ModelClient({'provider_config': str(self.path), 'role_providers': {'planner': 'Text', 'vision': 'Vision'}})
        with patch.object(client.opener, 'open', return_value=Response()) as opened:
            for role, host, model, key in [('planner', 'text', 'text-model', 'fake-text-key'), ('vision', 'vision', 'image-model', 'fake-image-key')]:
                client.complete(role, 'system', {})
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, f'https://{host}.example/v1/chat/completions')
                self.assertEqual(request.headers['Authorization'], 'Bearer ' + key)
                self.assertEqual(json.loads(request.data)['model'], model)
                self.assertNotIn(key, json.dumps(client.exchange_metadata()))

    def test_missing_provider_never_sends_request(self):
        client = ModelClient({'provider_config': str(self.path), 'role_providers': {'plc': 'Missing'}})
        with patch.object(client.opener, 'open') as opened, self.assertRaises(ContractError):
            client.complete('plc', 'system', {})
        opened.assert_not_called()

    def test_non_thinking_option_is_forwarded_and_rejects_non_boolean(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"choices":[{"message":{"content":"{}"}}]}'
        cfg = {'provider_config':str(self.path), 'role_providers':{'vision':'Vision'},
               'provider_options':{'Vision':{'enable_thinking':False}}}
        client = ModelClient(cfg)
        with patch.object(client.opener, 'open', return_value=Response()) as opened:
            client.complete('vision', 'JSON', {})
            self.assertIs(json.loads(opened.call_args.args[0].data)['enable_thinking'], False)
        cfg['provider_options']['Vision']['enable_thinking']='false'
        with patch.object(client.opener, 'open') as opened, self.assertRaises(ContractError):
            client.complete('vision', 'JSON', {})
        opened.assert_not_called()

    def test_role_request_options_override_provider_without_affecting_other_roles(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"choices":[{"message":{"content":"{}"}}]}'
        cfg={'provider_config':str(self.path),'role_providers':{'planner':'Text','plc':'Text'},
             'provider_options':{'Text':{'thinking':{'type':'disabled'},'max_tokens':12000}},
             'request_options':{'plc':{'thinking':{'type':'enabled'}}}}
        client=ModelClient(cfg)
        with patch.object(client.opener,'open',return_value=Response()) as opened:
            client.complete('plc','JSON',{})
            body=json.loads(opened.call_args.args[0].data)
            self.assertEqual(body['thinking'],{'type':'enabled'})
            self.assertEqual(body['max_tokens'],12000)
            client.complete('planner','JSON',{})
            self.assertEqual(json.loads(opened.call_args.args[0].data)['thinking'],{'type':'disabled'})
        self.assertEqual(cfg['provider_options']['Text']['thinking'],{'type':'disabled'})
        cfg['request_options']['plc']['shell_command']='invalid'
        with patch.object(client.opener,'open') as opened,self.assertRaises(ContractError):
            client.complete('plc','JSON',{})
        opened.assert_not_called()

    def test_length_finish_is_a_budget_failure_even_if_partial_json_parses(self):
        from factoryio_agents.llm import ModelOutputError
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}'
        client=ModelClient({'provider_config':str(self.path),'role_providers':{'plc':'Text'}})
        with patch.object(client.opener,'open',return_value=Response()),self.assertRaisesRegex(ModelOutputError,'finish_reason=length'):
            client.complete('plc','JSON',{})
        self.assertEqual(client.exchange_metadata()['http_status'],200)
        self.assertEqual(client.exchange_metadata()['finish_reason'],'length')

    def test_embedded_url_credentials_rejected_without_echo(self):
        self.path.write_text('Text:\n Base_URL: https://user:secret@example.com/v1\n Model_Name: model\n API_Key: fake\n', encoding='utf-8')
        with self.assertRaises(ContractError) as caught: load_providers(self.path)
        self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__': unittest.main()
