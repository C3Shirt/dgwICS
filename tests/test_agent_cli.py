"""CLI tests use mocked workflows/services; they never run software or create training gold."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factoryio_agents import cli
from factoryio_agents.contracts import file_hash, read_json, write_json
from factoryio_agents.diagnostics import summarize_run


class CLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        providers = self.root / 'providers.yaml'
        providers.write_text('Text:\n Base_URL: https://text.example\n Model_Name: fake-text\n API_Key: fake-key\n'
                             'Vision:\n Base_URL: https://vision.example/v1\n Model_Name: fake-vision\n API_Key: fake-image-key\n', encoding='utf-8')
        self.config = self.root / 'config.json'
        write_json(self.config, {'schema_version': 1, 'scene_sources': [],
            'llm': {'provider_config': str(providers), 'role_providers': {'planner': 'Text', 'scene': 'Text', 'plc': 'Text', 'vision': 'Vision'}},
            'codesys': {}, 'factoryio': {}, 'protocol': {}})
        self.output = self.root / 'result'

    def invoke(self, state, *options):
        with (patch.object(cli, 'ROOT', self.root), patch.object(cli, 'source_snapshot', return_value={}),
              patch.object(cli, 'Workflow') as workflow, contextlib.redirect_stdout(io.StringIO())):
            workflow.return_value.run.return_value = state
            workflow.return_value.prepare.return_value = state
            code = cli.main(['my scene prompt', '--config', str(self.config), '--out', str(self.output), *options])
        return code, workflow

    def test_positional_prompt_goes_to_framework_without_artifact_fallback(self):
        code, workflow = self.invoke({'status': 'failed', 'phase': 'planning', 'active_role': 'planner',
                                     'error': 'task has unresolved requirements'})
        self.assertEqual(code, 2)
        workflow.return_value.run.assert_called_once_with('my scene prompt')
        workflow.return_value.prepare.assert_not_called()
        self.assertFalse(read_json(self.output / 'execution.json')['manual_artifact_fallback_allowed'])
        self.assertEqual(read_json(self.output / 'failure-analysis.json')['failure']['category'], 'requirements_unresolved')

    def test_mocked_pass_without_provider_replies_is_not_a_live_pass(self):
        code, _ = self.invoke({'status': 'passed', 'acceptance': {'passed': True}})
        self.assertEqual(code, 2)
        self.assertFalse(read_json(self.output / 'result.json')['live_pipeline_passed'])

    def test_prepare_has_distinct_completion_semantics(self):
        code, workflow = self.invoke({'status': 'awaiting_scene_review'}, '--mode', 'prepare')
        self.assertEqual(code, 0)
        workflow.return_value.prepare.assert_called_once()
        workflow.return_value.run.assert_not_called()
        self.assertFalse(read_json(self.output / 'result.json')['live_pipeline_passed'])

    def test_keep_running_is_explicit_and_recorded_for_the_framework(self):
        code, workflow = self.invoke({'status':'failed', 'phase':'planning'}, '--keep-running')
        self.assertEqual(code, 2)
        self.assertTrue(workflow.call_args.args[0]['factoryio']['keep_running'])
        self.assertTrue(read_json(self.output/'execution.json')['keep_running_requested'])

    def test_check_only_never_calls_a_model_or_workflow(self):
        with (patch.object(cli, 'ROOT', self.root), patch.object(cli, 'source_snapshot', return_value={}),
              patch.object(cli, 'preflight', return_value={'issues': [], 'model_calls_issued': False}) as doctor,
              patch.object(cli, 'Workflow') as workflow, contextlib.redirect_stdout(io.StringIO())):
            code = cli.main(['--check', '--config', str(self.config), '--out', str(self.output)])
        self.assertEqual(code, 0); doctor.assert_called_once(); workflow.assert_not_called()

    def test_show_models_is_read_only_and_does_not_load_credentials_or_models(self):
        source=self.root/'history'
        write_json(source/'run.json',{'created_at':'2026-10-11T10:00:00+00:00'})
        write_json(source/'training-data/candidates/fixture.json',{'candidate_id':'fixture','role':'planner',
            'started_at':'2026-10-11T10:01:00+00:00','payload':{'planning_stage':'spec'},
            'structured_output':{'control_rules':['一次供料一件']}})
        before={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
        with (patch.object(cli,'ModelClient') as model,patch.object(cli,'protected_credentials') as credentials,
              contextlib.redirect_stdout(io.StringIO()) as stdout):
            code=cli.main(['--show-models',str(source)])
        self.assertEqual(code,0);model.assert_not_called();credentials.assert_not_called()
        self.assertIn('一次供料一件',stdout.getvalue())
        self.assertEqual(before,{str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})

    def test_cli_passes_readable_progress_logger_to_workflow(self):
        from factoryio_agents.progress import ConsoleProgress
        _,workflow=self.invoke({'status':'failed','phase':'planning'})
        self.assertIsInstance(workflow.call_args.kwargs['progress'],ConsoleProgress)

    def test_existing_output_cannot_be_reused(self):
        self.output.mkdir(); (self.output / 'previous.txt').write_text('keep')
        with patch.object(cli, 'Workflow') as workflow, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['scene', '--config', str(self.config), '--out', str(self.output)])
        workflow.assert_not_called(); self.assertEqual((self.output / 'previous.txt').read_text(), 'keep')

    def test_artifact_change_is_exposed_in_authorship_report(self):
        self.output.mkdir()
        value = {'task_id': 'model-task'}
        write_json(self.output / 'task.json', {'task_id': 'manually-changed'})
        write_json(self.output / 'training-data/candidates/fixture.json', {'candidate_id': 'fixture', 'role': 'planner',
            'source_kind': 'injected_client', 'status': 'response_received', 'output_origin': 'serialized_client_result',
            'structured_output': value})
        report = summarize_run(self.output, {'status': 'passed'})
        self.assertTrue(report['manual_override_detected'])
        self.assertFalse(report['live_pipeline_passed'])

    def test_interrupted_provider_call_is_kept_and_not_passed(self):
        self.output.mkdir()
        write_json(self.output / 'training-data/candidates/fixture.json', {'candidate_id': 'fixture', 'role': 'vision',
            'source_kind': 'injected_client', 'status': 'started', 'structured_output': None})
        report = summarize_run(self.output, {'status': 'cancelled'})
        self.assertEqual(report['failure']['category'], 'cancelled')
        self.assertFalse(report['all_four_roles_have_provider_replies'])

    def test_cli_credentials_are_cleaned_up_and_failure_logs_are_redacted(self):
        config = read_json(self.config)
        names = {'username_env': 'TEST_SCENE_PLC_USER', 'password_env': 'TEST_SCENE_PLC_PASSWORD'}
        config['codesys']['online_credentials'] = names; write_json(self.config, config)
        def credentials(_):
            os.environ[names['username_env']] = 'fixture-user'
            os.environ[names['password_env']] = 'fixture-secret'
        with (patch.dict(os.environ, {}, clear=True), patch.object(cli, 'ROOT', self.root),
              patch.object(cli, 'protected_credentials', side_effect=credentials),
              patch.object(cli, 'source_snapshot', return_value={}), patch.object(cli, 'Workflow') as workflow,
              contextlib.redirect_stdout(io.StringIO()) as stdout):
            workflow.return_value.state = {'phase': 'connection', 'active_role': 'tools'}
            workflow.return_value.run.side_effect = RuntimeError('failure fixture-secret')
            code = cli.main(['scene', '--config', str(self.config), '--out', str(self.output)])
            self.assertFalse(any(name in os.environ for name in names.values()))
        self.assertEqual(code, 2)
        self.assertNotIn('fixture-secret', stdout.getvalue())
        for filename in ('result.json', 'failure-analysis.json', 'run.json', 'config.snapshot.json'):
            self.assertNotIn('fixture-secret', (self.output / filename).read_text(encoding='utf-8'))

    def test_keyboard_interrupt_still_writes_failure_report(self):
        with (patch.object(cli, 'ROOT', self.root), patch.object(cli, 'source_snapshot', return_value={}),
              patch.object(cli, 'Workflow') as workflow, contextlib.redirect_stdout(io.StringIO())):
            workflow.return_value.state = {'phase': 'planning', 'active_role': 'planner'}
            workflow.return_value.run.side_effect = KeyboardInterrupt()
            code = cli.main(['scene', '--config', str(self.config), '--out', str(self.output)])
        self.assertEqual(code, 2)
        self.assertEqual(read_json(self.output / 'failure-analysis.json')['failure']['category'], 'cancelled')

    def test_acceptance_requires_both_visual_reviews_and_real_role_candidates(self):
        self.output.mkdir()
        scene = self.output / 'scene-000'; plc = scene / 'plc-000'
        versions = {'scene': 'fixture-version', 'plc': 'fixture-version'}
        acceptance = {'passed': True, 'scope': 'test fixture, no live software'}
        artifacts = [(self.output / 'task.json', 'planner', {'task_id': 'fixture'}),
            (scene / 'scene-plan.json', 'scene', {'scene_id': 'fixture'}),
            (plc / 'program.json', 'plc', {'implementation': 'fixture'}),
            (scene / 'scene-feedback.json', 'vision', {'verdict': 'pass', 'versions': versions}),
            (plc / 'runtime-feedback.json', 'vision', {'verdict': 'pass', 'versions': versions})]
        for i,(path,role,value) in enumerate(artifacts):
            write_json(path, value)
            write_json(self.output / f'training-data/candidates/fixture-{i}.json', {'candidate_id': f'fixture-{i}',
                'role': role, 'source_kind': 'live_model', 'output_origin': 'provider_content',
                'status': 'response_received', 'model_metadata': {'http_status': 200}, 'structured_output': value})
        (scene / 'scene.factoryio').write_text('fixture', encoding='utf-8')
        write_json(scene / 'manifest.json', {'scene_sha256': file_hash(scene / 'scene.factoryio')})
        write_json(plc / 'acceptance.json', acceptance)
        state = {'status': 'passed', 'scene_dir': str(scene), 'plc_dir': str(plc),
                 'versions': versions, 'acceptance': acceptance}
        self.assertTrue(summarize_run(self.output, state)['live_pipeline_passed'])
        (plc / 'runtime-feedback.json').unlink()
        report = summarize_run(self.output, state)
        self.assertFalse(report['live_pipeline_passed'])
        self.assertEqual(report['failure']['category'], 'attribution_or_acceptance_missing')


if __name__ == '__main__': unittest.main()
