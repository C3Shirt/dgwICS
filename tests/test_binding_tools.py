"""Device preparation tests with fixture files and mocked CODESYS; no live deployment."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factoryio_agents.binding_tools import BindingConfigurationError, bool_bindings, compile_bound_program
from factoryio_agents.contracts import file_hash, write_json
from factoryio_agents.plc import transport_fingerprint
from factoryio_agents.scene import build_catalog, build_scene
from test_scene import FIXTURE
from test_contracts_runtime import PROTOCOL


class BindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        source = self.root / 'source.factoryio'; source.write_text(FIXTURE, encoding='utf-8')
        catalog = build_catalog([source])
        plan = {'scene_id': 'fixture', 'base_source_id': catalog['sources'][0]['id'],
                'objects': [{'id': str(i), 'template_id': t['template_id']} for i,t in enumerate(catalog['templates'])]}
        self.contract = build_scene(plan, catalog, self.root / 'scene.factoryio')['io_contract']
        self.contract['protocol'] = PROTOCOL
        self.template = self.root / 'template.project'; self.template.write_bytes(b'fixture project')
        seed = {'template_project': str(self.template), 'verified_template_sha256': file_hash(self.template),
            'verified_transport_sha256': transport_fingerprint(self.contract), 'verified_device_bindings': [
                {'tag_key': m['tag_key'], 'variable': 'Application.OldIO.bit', 'bit_size': '1',
                 'channel_type': m['direction'], 'device_path': ['Device', 'Server'], 'connector_id': 1,
                 'parameter_id': i} for i,m in enumerate(self.contract['mappings'])]}
        write_json(self.root / 'seed.json', {'codesys': seed})
        write_json(self.root / 'previous.json', self.contract)
        self.config = {'binding_seed_config': str(self.root / 'seed.json'), 'binding_seed_io': str(self.root / 'previous.json'),
                       'auto_bool_bindings': True}

    def test_new_scene_guids_keep_actual_verified_channels_without_touching_template(self):
        current = deepcopy(self.contract)
        keys = {t['key']: 'new-guid-' + str(i) for i,t in enumerate(current['tags'])}
        for tag in current['tags']: tag['key'] = keys[tag['key']]
        for mapping in current['mappings']: mapping['tag_key'] = keys[mapping['tag_key']]
        original = self.template.read_bytes()
        result = bool_bindings(self.config, current)
        self.assertEqual(self.template.read_bytes(), original)
        self.assertEqual({b['tag_key'] for b in result['bindings']}, {m['tag_key'] for m in current['mappings']})
        self.assertEqual([b['parameter_id'] for b in result['verified_device_bindings']], list(range(len(current['mappings']))))
        self.assertTrue(result['device_patch']['clear_existing_mappings'])
        self.assertNotIn('program', result)

    def test_numeric_or_unavailable_channels_are_configuration_failures(self):
        current = deepcopy(self.contract); current['mappings'][0]['channel_family'] = 'Numeric'
        with self.assertRaisesRegex(BindingConfigurationError, 'only inspected BOOL'):
            bool_bindings(self.config, current)
        current = deepcopy(self.contract); current['mappings'][0]['channel'] = 'BitInput999'
        with self.assertRaises(BindingConfigurationError): bool_bindings(self.config, current)

    def test_changed_seed_project_is_rejected(self):
        self.template.write_bytes(b'changed')
        with self.assertRaisesRegex(BindingConfigurationError, 'template SHA changed'):
            bool_bindings(self.config, self.contract)

    def test_unverified_preparation_never_deploys(self):
        for field in ('compiled', 'bindings_verified', 'task_program_verified'):
            report = dict.fromkeys(('compiled', 'bindings_verified', 'task_program_verified'), True); report[field] = False
            with patch('factoryio_agents.binding_tools.run_codesys', return_value=report) as runner:
                compile_bound_program(self.config, self.root / field, {}, self.contract, True)
            self.assertEqual(runner.call_count, 1); self.assertFalse(runner.call_args.kwargs['deploy'])

    def test_deployment_uses_prepared_copy_and_removes_device_patch(self):
        prepared = {'compiled': True, 'bindings_verified': True, 'task_program_verified': True,
                    'project_path': str(self.root / 'prepared.project'), 'project_sha256': 'prepared-sha'}
        with patch('factoryio_agents.binding_tools.run_codesys', side_effect=[prepared, {'compiled': True}]) as runner:
            compile_bound_program({**self.config, 'device_patch': {'fixture': True}}, self.root / 'run', {}, self.contract, True)
        deployed_config = runner.call_args.args[0]
        self.assertIsNone(deployed_config['device_patch'])
        self.assertEqual(deployed_config['verified_template_sha256'], 'prepared-sha')
        self.assertEqual(deployed_config['template_project'], prepared['project_path'])
        self.assertTrue(runner.call_args.kwargs['deploy'])


if __name__ == '__main__': unittest.main()
