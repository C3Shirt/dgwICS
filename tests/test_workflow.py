"""Repair/routing tests with explicitly mocked models and native runtime adapters."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from factoryio_agents.contracts import ContractError, canonical_hash, read_json, write_json
from factoryio_agents.demo import demo_program, demo_task
from factoryio_agents.scene import build_catalog
from factoryio_agents.workflow import Workflow, verify_artifact_versions
from test_scene import FIXTURE
from test_contracts_runtime import PROTOCOL


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        source = self.root / "source.factoryio"; source.write_text(FIXTURE, encoding="utf-8")
        catalog = build_catalog([source])
        self.task = demo_task(PROTOCOL); self.task["unresolved"] = []
        self.task["objects"] = [{"id": "sensor", "prefab": "DiffusePhotoelectricSensor", "purpose": "detect"},
                                {"id": "conveyor", "prefab": "RollerConveyor6M", "purpose": "move"}]
        self.plan = {"scene_id": self.task["task_id"], "base_source_id": catalog["sources"][0]["id"],
            "objects": [{"id": "sensor", "template_id": catalog["templates"][0]["template_id"]},
                        {"id": "conveyor", "template_id": catalog["templates"][1]["template_id"]}]}
        config = {"scene_sources": [str(source)], "protocol": PROTOCOL, "llm": {},
                  "codesys": {"deploy": True, "transport_confirmed": True},
                  "factoryio": {"runtime_control": True, "load_command": ["mock"], "connect_command": ["mock-connect"]},
                  "max_scene_repairs": 1, "max_plc_repairs": 1}
        self.workflow = Workflow(config, self.root / "run", Mock())
        self.workflow.planner = Mock(); self.workflow.planner.plan.return_value = self.task
        self.workflow.author = Mock(); self.workflow.author.plan.return_value = self.plan
        from test_layout_design import proposal_fixture
        self.workflow.author.propose_layout.return_value = proposal_fixture(self.task)
        self.workflow.planner.review_layout.return_value = {
            'verdict':'approved','summary':'Fixture design supports the requested flow.','findings':[]}
        self.workflow.observer = Mock()
        self.workflow.plc_agent = Mock()
        self.workflow.plc_agent.program.side_effect = lambda task, contract, *args: demo_program(contract)
        self.stack = []
        for name, value in (("factoryio_agents.workflow.run_hook", {"returncode": 0}),
                            ("factoryio_agents.plc.run_codesys", {"compiled": True, "started": True}),
                            ("factoryio_agents.workflow.validate_events", {"passed": False})):
            manager = patch(name, return_value=value); mock = manager.start()
            self.addCleanup(manager.stop); self.stack.append(mock)
        def confirm_load(command, substitutions, timeout):
            request = read_json(Path(substitutions["request"]))
            receipt = ({**request, "connection_confirmed": True, "driver": "ModbusTCPClient", "modbus": {
                "connected": True, **{k: request[k] for k in ("host", "port", "unit_id")}}} if command == ["mock-connect"] else
                {**request, "software_load_confirmed": True, "confirmation_method": "explicitly mocked load confirmation",
                 "native_geometry": {"objects": []}})
            write_json(Path(substitutions["confirmation"]), receipt)
            return {"returncode": 0}
        self.stack[0].side_effect = confirm_load
        self.compiler, self.acceptance = self.stack[1:]
        manager = patch("factoryio_agents.workflow.FactoryIOClient"); self.runtime = manager.start().return_value
        self.runtime.wait_running.side_effect = lambda expected: {"running": expected, "isForced": False}
        self.addCleanup(manager.stop)
        manager = patch("factoryio_agents.workflow.collect_evidence", side_effect=lambda c, out, versions, io, **kw:
            {"versions": versions, "reconciliation": {"passed": True}, "records": [], "mode": "live"})
        manager.start(); self.addCleanup(manager.stop)
        manager = patch('factoryio_agents.geometry_tools.loaded_geometry', return_value={
            'passed':True,'scope':'explicitly_mocked_geometry','spatial_checks_applied':True,'issues':[]})
        self.geometry = manager.start(); self.addCleanup(manager.stop)

    def feedback(self, verdict="pass", target=None):
        def review(task, manifest, contract, evidence, versions, phase, *rest, **kwargs):
            categories = {"scene": "geometry", "plc": "logic", "tools": "communication"}
            return {"phase": phase, "versions": versions, "verdict": verdict,
                "findings": [] if target is None else [{"target": target, "category": categories[target]}]}
        return review

    def test_layout_proposal_is_reviewed_before_scene_and_given_to_vision(self):
        calls=[]
        self.workflow.author.propose_layout.side_effect=lambda *a,**kw:calls.append('proposal') or self.workflow.author.propose_layout.return_value
        self.workflow.planner.review_layout.side_effect=lambda *a,**kw:calls.append('review') or self.workflow.planner.review_layout.return_value
        self.workflow.author.plan.side_effect=lambda *a,**kw:calls.append('generate') or self.plan
        self.workflow.observer.review.side_effect=self.feedback('inconclusive','tools')
        self.workflow.run('test')
        self.assertEqual(calls[:3],['proposal','review','generate'])
        approved=read_json(self.workflow.output/'approved-layout.json')
        self.assertEqual(self.workflow.author.plan.call_args.kwargs['layout_reference'],approved)
        self.assertEqual(self.workflow.observer.review.call_args.kwargs['approved_layout'],approved)
        self.assertEqual(read_json(self.workflow.output/'scene-000/layout-reference.json'),approved)

    def test_uncertain_design_stops_before_realization_or_software_load(self):
        self.workflow.planner.review_layout.return_value={'verdict':'uncertain','summary':'Missing relative position.',
            'findings':[{'object_ids':['sensor'],'reason':'Detection zone is not specified.',
                         'suggestion':'Clarify detection relative to the conveyor.'}]}
        state=self.workflow.run('test')
        self.assertEqual((state['status'],state['phase'],state['target']),
                         ('needs_attention','layout_review','planner'))
        self.workflow.author.plan.assert_not_called()
        self.workflow.observer.review.assert_not_called()
        self.stack[0].assert_not_called()

    def test_rejected_unchanged_proposal_is_bounded_before_realization(self):
        self.workflow.planner.review_layout.return_value={'verdict':'revise','summary':'Fix the detection zone.',
            'findings':[{'object_ids':['sensor'],'reason':'Undefined sensing relation.',
                         'suggestion':'Define the beam through the material path.'}]}
        state=self.workflow.run('test')
        self.assertEqual((state['status'],state['phase']),('repair_limit_reached','layout_review'))
        self.assertEqual(self.workflow.author.propose_layout.call_count,2)
        self.workflow.author.plan.assert_not_called()
        self.assertFalse((self.workflow.output/'approved-layout.json').exists())

    def test_revised_proposal_is_reviewed_again_with_object_feedback(self):
        rejected=self.workflow.author.propose_layout.return_value
        corrected=deepcopy(rejected)
        corrected['objects'][0]['expected_position']['relation']='Detect at the upstream material entry.'
        feedback={'verdict':'revise','summary':'Correct the detector.',
            'findings':[{'object_ids':['sensor'],'reason':'Detection is at the wrong process stage.',
                         'suggestion':'Put the detector at entry before downstream removal.'}]}
        self.workflow.author.propose_layout.side_effect=[rejected,corrected]
        self.workflow.planner.review_layout.side_effect=[feedback,self.workflow.planner.review_layout.return_value]
        self.workflow.observer.review.side_effect=self.feedback('inconclusive','tools')
        self.workflow.run('test')
        args=self.workflow.author.propose_layout.call_args.args
        self.assertEqual(args[2:4],(rejected,feedback))
        self.assertEqual(read_json(self.workflow.output/'approved-layout.json')['objects'],corrected['objects'])
        self.assertEqual(self.workflow.planner.review_layout.call_count,2)

    def test_scene_repairs_are_bounded_and_never_compile_unverified_geometry(self):
        self.workflow.observer.review.side_effect = self.feedback("repair", "scene")
        result = self.workflow.run("test")
        self.assertEqual((result["status"], result["phase"]), ("repair_limit_reached", "scene"))
        self.assertEqual(self.workflow.author.plan.call_count, 2)
        self.compiler.assert_not_called()

    def test_communication_failure_is_not_sent_to_plc_agent(self):
        self.workflow.observer.review.side_effect = self.feedback("repair", "tools")
        self.assertEqual(self.workflow.run("test")["status"], "needs_attention")
        self.workflow.plc_agent.program.assert_not_called()

    def test_unapplied_geometry_check_cannot_advance_to_visual_pass_or_plc(self):
        self.geometry.return_value = {'passed':True,'scope':'unsupported_topology',
            'spatial_checks_applied':False,'issues':[], 'reason':'fixture geometry unsupported'}
        self.workflow.observer.review.side_effect = self.feedback()
        result = self.workflow.run('test')
        self.assertEqual((result['status'],result['phase'],result['target']),('needs_attention','scene_geometry','tools'))
        self.workflow.observer.review.assert_not_called()
        self.workflow.plc_agent.program.assert_not_called()
        self.compiler.assert_not_called()
        self.assertEqual(self.workflow.author.plan.call_count,1)

    def test_failed_applied_geometry_is_still_sent_to_scene_repair(self):
        self.geometry.return_value = {'passed':False,'scope':'single_conveyor_spatial_connections',
            'spatial_checks_applied':True,'issues':[{'object_id':'sensor','error':'beam cannot reach material path'}]}
        result = self.workflow.run('test')
        self.assertEqual((result['status'],result['phase']),('repair_limit_reached','scene'))
        self.workflow.plc_agent.program.assert_not_called()
        self.compiler.assert_not_called()

    def test_visual_handoff_contains_the_applied_geometry_result(self):
        self.workflow.observer.review.side_effect = self.feedback('inconclusive', 'tools')
        self.workflow.run('test')
        evidence = self.workflow.observer.review.call_args.args[3]
        reports = [r for r in evidence['records'] if r['kind'] == 'scene_geometry']
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['result'], self.geometry.return_value)
        self.workflow.plc_agent.program.assert_not_called()

    def test_initial_measurement_uses_two_end_reset_start_and_stop(self):
        self.workflow.config['factoryio']['synchronized_lifecycle'] = True
        self.workflow.observer.review.side_effect = [self.feedback()({}, {}, {}, {}, {}, 'scene'),
            self.feedback('repair','tools')({}, {}, {}, {}, {}, 'runtime')]
        events = []
        startup, cleanup = Mock(), Mock()
        startup.reset_both.side_effect = lambda mode: events.append(('reset',mode)) or {'operation_passed':True}
        startup.start_both.side_effect = lambda: events.append('start') or {'operation_passed':True}
        cleanup.stop_both.side_effect = lambda: events.append('stop')
        capture = {'mode':'live','records':[], 'reconciliation':{'passed':True}}
        with patch('factoryio_agents.synchronized_runtime.SynchronizedRuntime',side_effect=[startup,cleanup]), \
             patch('factoryio_agents.workflow.collect_evidence',side_effect=lambda *a, **k: events.append('capture') or capture):
            self.workflow.run('test')
        self.assertEqual(events,['capture',('reset','warm'),'start','capture','stop'])
        self.runtime.control.assert_not_called()

    def test_stopped_service_blocks_before_models_and_ide(self):
        self.workflow.config['codesys']['ensure_local_services']=True
        self.workflow.observer.review.side_effect=self.feedback()
        with patch('factoryio_agents.codesys_services.service_control') as service:
            service.side_effect=[{'status':'ok','service_running':True},
                {'status':'error','service_running':False,'state':'Stopped','windows_error':5}]
            state=self.workflow.run('test')
        self.compiler.assert_not_called()
        self.assertEqual((state['status'],state['phase']),('needs_configuration','environment_preflight'))
        self.assertEqual(state['target'],'tools')
        self.workflow.planner.plan.assert_not_called()
        self.workflow.observer.review.assert_not_called()
        self.assertEqual(self.workflow.budget.state['calls'],[])
        self.assertEqual(service.call_count,2)
        self.assertFalse(read_json(self.root/'run/environment-preflight.json')['plc_application_running_verified'])

    def test_success_checkpoint_with_no_failure_does_not_break_role_handoffs(self):
        observed = {'kind':'observed_failure','failure':{'active_role':'vision','target':'tools'}}
        self.workflow.memory_context = {'observations':[
            {'kind':'design_checkpoint','failure':None},observed,
            {'kind':'observed_failure','failure':{'active_role':'plc','target':'plc'}}]}
        packet = self.workflow.handoff('vision','scene_review',[],'scene-feedback.json')
        self.assertEqual(packet['memory']['observations'],[observed])
        self.compiler.assert_not_called(); self.workflow.observer.review.assert_not_called()

    def test_model_completion_budget_failure_is_routed_to_configuration(self):
        from factoryio_agents.llm import ModelOutputError
        self.workflow.observer.review.side_effect=self.feedback()
        self.workflow.plc_agent.program.side_effect=ModelOutputError('plc output truncated')
        state=self.workflow.run('test')
        self.assertEqual(state['failure_kind'],'model_output')
        self.assertEqual(state['failed_role'],'plc')
        self.assertEqual(state['target'],'tools')
        self.compiler.assert_not_called()

    def test_visual_provider_failure_does_not_trigger_geometry_repair(self):
        from factoryio_agents.llm import ModelServiceError
        self.workflow.observer.review.side_effect = ModelServiceError('vision model request failed (TimeoutError)')
        result = self.workflow.run('test')
        self.assertEqual(result['failure_kind'], 'model_service')
        self.assertEqual(result['target'], 'tools')
        self.assertEqual(result['failed_role'], 'vision')
        self.assertEqual(self.workflow.author.plan.call_count, 1)
        self.workflow.plc_agent.program.assert_not_called()

    def test_supervisor_investigation_routes_to_plc_and_requires_fresh_runtime_review(self):
        self.workflow.config['supervisor']={'enabled':True}
        self.compiler.return_value.update(bindings_verified=True,task_program_verified=True)
        self.acceptance.side_effect=[{'passed':False},{'passed':True}]
        count=0
        def review(*args, **kwargs):
            nonlocal count
            count+=1
            return self.feedback('inconclusive','plc')(*args,**kwargs) if count==2 else self.feedback()(*args,**kwargs)
        self.workflow.observer.review.side_effect=review
        n=0
        def program(task,contract,*args):
            nonlocal n
            n+=1; result=demo_program(contract)
            result['implementation'] += '\n(* injected test revision '+str(n)+' *)'
            return result
        self.workflow.plc_agent.program.side_effect=program
        with patch('factoryio_agents.supervisor.SupervisorAgent') as factory:
            factory.return_value.investigate.return_value={'verdict':'repair','findings':[{'target':'plc'}]}
            result=self.workflow.run('test')
            factory.return_value.investigate.assert_called_once()
        self.assertEqual(result['status'],'passed')  # Injected fixtures only, no live pass.
        self.assertEqual(self.workflow.observer.review.call_count,3)
        first=self.root/'run/scene-000/plc-000'
        self.assertEqual(read_json(first/'runtime-feedback.json')['verdict'],'inconclusive')
        self.assertEqual(read_json(first/'runtime-investigation.json')['verdict'],'repair')
        self.assertEqual(self.compiler.call_count,2)

    def test_device_capacity_failure_is_not_sent_back_to_scene_agent(self):
        from factoryio_agents.binding_tools import BindingConfigurationError
        self.workflow.config['codesys']['auto_bool_bindings'] = True
        with patch('factoryio_agents.binding_tools.inspected_io_capacity',return_value={}), \
             patch('factoryio_agents.binding_tools.bool_bindings', side_effect=BindingConfigurationError('fixture capacity')):
            result = self.workflow.run('test')
        self.assertEqual(result['failure_kind'], 'tool_configuration')
        self.assertEqual((result['target'], result['phase']), ('tools', 'device_binding'))
        self.assertEqual(self.workflow.author.plan.call_count, 1)
        self.workflow.plc_agent.program.assert_not_called()

    def test_deferred_verification_reaches_each_role_and_closes_only_with_evidence(self):
        from factoryio_agents.verification import VERIFIERS
        self.task['verification_tasks'] = [{'id': key, 'description': 'fixture phase check', 'verifier': key,
            'owner': rule['owner'], 'stage': rule['stage']} for key,rule in VERIFIERS.items()]
        self.compiler.return_value.update(bindings_verified=True, task_program_verified=True)
        self.acceptance.return_value = {'passed': True}
        self.workflow.observer.review.side_effect = self.feedback()
        result = self.workflow.run('test')
        self.assertEqual(result['status'], 'passed')
        ledger = read_json(self.root / 'run/verification.json')
        self.assertTrue(ledger['all_completed'])
        self.assertEqual(len(ledger['checks']), 5)
        for row in ledger['checks']:
            self.assertEqual(row['status'], 'passed')
            self.assertTrue(all(Path(ref['path']).is_file() for ref in row['evidence']))
        self.assertEqual(self.workflow.author.plan.call_count, 1)
        self.assertEqual(self.workflow.observer.review.call_count, 2)

    def test_unmet_requirement_still_blocks_before_scene_authoring(self):
        self.task['unresolved'] = ['required actuator is not available']
        result = self.workflow.run('test')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('unresolved requirements', result['error'])
        self.workflow.author.plan.assert_not_called()
        self.compiler.assert_not_called()

    def test_keep_running_uses_two_end_stop_and_verified_handoff(self):
        self.workflow.config['factoryio'].update(keep_running=True, synchronized_lifecycle=True)
        self.compiler.return_value.update(bindings_verified=True, task_program_verified=True)
        self.acceptance.return_value = {'passed':True}
        self.workflow.observer.review.side_effect = self.feedback()
        with (patch('factoryio_agents.synchronized_runtime.SynchronizedRuntime') as lifecycle,
              patch('factoryio_agents.live_handoff.start_live_handoff',
                    return_value=({'passed':True,'left_running':True},{})) as handoff):
            lifecycle.return_value.reset_both.return_value = {'operation_passed':True}
            lifecycle.return_value.start_both.return_value = {'operation_passed':True}
            result = self.workflow.run('test')
        self.assertEqual(result['status'],'passed')
        self.assertTrue(result['live_handoff']['left_running'])
        lifecycle.return_value.stop_both.assert_called_once()
        self.assertIs(handoff.call_args.kwargs['lifecycle'], lifecycle.return_value)
        self.assertEqual(handoff.call_args.kwargs['reset_mode'],'warm')

    def test_unproven_diagnosis_routes_to_investigation_before_code_changes(self):
        self.workflow.observer.review.side_effect = ContractError("hypothesis requires investigation before automatic repair")
        result = self.workflow.run("test")
        self.assertEqual((result["status"], result["target"]), ("needs_attention", "investigate"))
        self.workflow.plc_agent.program.assert_not_called()

    def test_compiler_failures_are_bounded_and_repair_receives_diagnostics(self):
        self.workflow.observer.review.side_effect = self.feedback()
        self.compiler.return_value = {"compiled": False, "diagnostics": [{"message": "real compiler error fixture"}],
            "bridge_result": {"build_completed": True, "messages_verified": True},
            "error_count": 1, "source_unchanged": True}
        result = self.workflow.run("test")
        self.assertEqual((result["status"], result["phase"]), ("repair_limit_reached", "plc"))
        self.assertEqual(self.compiler.call_count, 1)
        self.assertEqual(result["reason"], "no_progress")
        second = self.workflow.plc_agent.program.call_args_list[1].args
        self.assertEqual(second[2]["phase"], "compile")
        self.assertIsNotNone(second[3])

    def test_configuration_and_tool_failures_do_not_regenerate_plc(self):
        self.workflow.observer.review.side_effect = self.feedback()
        self.compiler.return_value = {"compiled": False, "diagnostics": [{"code": "codesys_runner"}]}
        result = self.workflow.run("test")
        self.assertEqual(result["status"], "needs_configuration")
        self.assertEqual(result["target"], "tools")
        self.assertEqual(self.workflow.plc_agent.program.call_count, 1)

    def test_unconfirmed_build_result_is_not_sent_to_plc_repair(self):
        self.workflow.observer.review.side_effect = self.feedback()
        self.compiler.return_value = {"compiled": False, "command": ["CODESYS"],
            "bridge_result": {"build_completed": True, "messages_verified": False}, "error_count": 1}
        self.assertEqual(self.workflow.run("test")["status"], "needs_attention")
        self.assertEqual(self.workflow.plc_agent.program.call_count, 1)

    def test_changed_config_endpoint_cannot_stamp_evidence_with_old_hash(self):
        import contextlib, io
        from factoryio_agents.__main__ import main
        self.workflow.observer.review.side_effect = self.feedback("repair", "tools")
        result = self.workflow.run("test")
        config = deepcopy(self.workflow.config); config["factoryio"]["web_api_url"] = "http://127.0.0.1:9999"
        path = self.root / "config.json"; write_json(path, config)
        with patch("factoryio_agents.__main__.collect_evidence") as collector, contextlib.redirect_stderr(io.StringIO()):
            code = main(["collect", "--config", str(path), "--scene-dir", result["scene_dir"],
                         "--out", str(self.root / "capture")])
        self.assertEqual(code, 2); collector.assert_not_called()

    def test_visual_pass_cannot_override_failed_deterministic_acceptance(self):
        self.workflow.observer.review.side_effect = self.feedback()
        self.assertEqual(self.workflow.run("test")["status"], "needs_attention")

    def test_runtime_geometry_repair_invalidates_plc_and_creates_new_scene_revision(self):
        fixed_plan = deepcopy(self.plan); fixed_plan["objects"][0]["position"] = [1, 2, 3]
        self.workflow.author.plan.side_effect = [self.plan, fixed_plan]
        callbacks = [self.feedback(), self.feedback("repair", "scene"), self.feedback(), self.feedback()]
        self.workflow.observer.review.side_effect = lambda *args, **kwargs: callbacks.pop(0)(*args, **kwargs)
        self.acceptance.side_effect = [{"passed": False}, {"passed": True}]
        result = self.workflow.run("test")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scene_revision"], 1)
        self.assertEqual(self.compiler.call_count, 2)
        second = self.workflow.plc_agent.program.call_args_list[1].args
        self.assertIsNone(second[2]); self.assertIsNone(second[3])
        versions = [read_json(Path(result["scene_dir"]).parent / f"scene-{n:03d}" / "versions.json") for n in range(2)]
        self.assertEqual(versions[0]["plc"], None)
        self.assertEqual(versions[1]["plc"], None)

    def test_driver_receipt_endpoint_mismatch_stops_before_runtime_or_code_repair(self):
        self.workflow.observer.review.side_effect = self.feedback()
        def mismatch(directory, scene_path, versions):
            raise ContractError("connection receipt does not confirm the configured endpoint")
        self.workflow.connect_runtime = mismatch
        result = self.workflow.run("test")
        self.assertEqual((result["status"], result["phase"], result["target"]), ("needs_attention", "connection", "tools"))
        self.runtime.control.assert_not_called()
        self.assertEqual(self.workflow.plc_agent.program.call_count, 1)

    def test_control_write_without_observed_run_is_a_tool_failure(self):
        self.workflow.observer.review.side_effect = self.feedback()
        self.runtime.wait_running.side_effect = lambda value: (_ for _ in ()).throw(ContractError("Run not observed")) if value else {}
        result = self.workflow.run("test")
        self.assertEqual((result["phase"], result["target"]), ("runtime_control", "tools"))
        self.assertEqual(self.workflow.plc_agent.program.call_count, 1)
        self.assertEqual(self.runtime.control.call_args.args, ("run", False))

    def test_acceptance_diagnostics_and_versioned_inputs_are_handed_to_reviewer(self):
        self.workflow.observer.review.side_effect = self.feedback()
        result = self.workflow.run("test")
        review = self.workflow.observer.review.call_args.args
        self.assertTrue(any(r["kind"] == "acceptance_result" and r["result"]["passed"] is False for r in review[3]["records"]))
        packet = review[-1]
        self.assertEqual(packet["recipient"], "vision")
        self.assertEqual(packet["versions"], result["versions"])
        self.assertTrue(all(Path(i["path"]).is_file() for i in packet["inputs"]))
        self.assertTrue(list((self.root / "run" / "handoffs").glob("*.json")))

    def test_success_exit_and_matching_tags_cannot_replace_scene_load_confirmation(self):
        self.stack[0].side_effect = None
        result = self.workflow.run("test")
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["phase"], "scene_load")
        self.workflow.observer.review.assert_not_called(); self.compiler.assert_not_called()

    def test_missing_required_scene_object_stops_before_loading_or_plc(self):
        self.plan["objects"].pop()
        result = self.workflow.run("test")
        self.assertEqual(result["status"], "repair_limit_reached")
        self.assertIn("required task object", result['repairs'][0]['feedback']['diagnostics']['error'])
        self.assertEqual(self.workflow.author.plan.call_count, 2)
        self.stack[0].assert_not_called(); self.compiler.assert_not_called()

    def test_modified_io_contract_cannot_be_attached_to_original_scene_version(self):
        self.workflow.observer.review.side_effect = self.feedback("repair", "tools")
        result = self.workflow.run("test"); directory = Path(result["scene_dir"])
        contract = read_json(directory / "io-contract.json")
        contract["transport_confirmed"] = False
        write_json(directory / "io-contract.json", contract)
        with self.assertRaises(ContractError): verify_artifact_versions(directory)

    def test_manual_runtime_collection_rejects_plc_from_another_scene(self):
        from factoryio_agents.__main__ import runtime_revision
        directory = self.root / "plc"
        program = {"manual": True}
        versions = {"task": "t", "scene": "s", "io": "i", "config": "c", "plc": canonical_hash(program)}
        write_json(directory / "versions.json", versions)
        write_json(directory / "program.json", program)
        changed = {**versions, "scene": "other"}
        with self.assertRaisesRegex(ContractError, "another scene"):
            runtime_revision(changed, directory)

    def test_manual_runtime_collection_rejects_modified_program(self):
        from factoryio_agents.__main__ import runtime_revision
        directory = self.root / "plc"
        versions = {"task": "t", "scene": "s", "io": "i", "config": "c", "plc": canonical_hash({"manual": True})}
        write_json(directory / "versions.json", versions)
        write_json(directory / "program.json", {"manual": False})
        with self.assertRaisesRegex(ContractError, "modified"):
            runtime_revision(versions, directory)


if __name__ == "__main__": unittest.main()
