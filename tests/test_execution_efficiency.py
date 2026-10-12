"""Admission and evidence scope regressions; fake providers do not prove live success."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from factoryio_agents.contracts import canonical_hash, read_json, write_json
from factoryio_agents.coordination import model_review_evidence
from factoryio_agents.execution import BudgetedClient, ExecutionBudget, ExecutionLimitError
from factoryio_agents.memory import latest_runtime_review
from factoryio_agents.review_context import project_review_payload
from factoryio_agents.supervisor import SupervisorAgent, progress_view
from factoryio_agents.geometry_tools import relative_layout_capability, validate_layout_geometry, resolve_transport_clearance


class Provider:
    def __init__(self): self.calls = 0
    def complete(self, *args): self.calls += 1; return {'fixture':True}
    def exchange_metadata(self): return {'usage':{'total_tokens':7}}


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_budget_shared_and_reopened_does_not_reset_counts(self):
        config = {'execution_limits':{'max_calls':2}}
        budget = ExecutionBudget(self.root,config); raw=Provider()
        a=BudgetedClient(raw,budget);b=BudgetedClient(raw,budget)
        a.complete('vision','s',{});b.complete('supervisor','s',{})
        self.assertEqual(budget.state['observed_total_tokens'],14)
        reopened=BudgetedClient(raw,ExecutionBudget(self.root,config))
        with self.assertRaises(ExecutionLimitError):reopened.complete('plc','s',{})
        self.assertEqual(raw.calls,2)

    def test_repeated_rejected_reply_blocks_before_provider_call(self):
        budget=ExecutionBudget(self.root,{})
        raw=Provider(); client=BudgetedClient(raw,budget)
        payload={'format_feedback':{'error':'bad target','previous_response':{'handoff_id':'one','target':'belt'}}}
        client.complete('vision','s',payload)
        payload['format_feedback']['previous_response']['handoff_id']='two'
        with self.assertRaisesRegex(ExecutionLimitError,'identical'):client.complete('vision','s',payload)
        self.assertEqual(raw.calls,1)

    def test_token_and_time_limits_are_admission_checks(self):
        raw=Provider();budget=ExecutionBudget(self.root,{'execution_limits':{'max_total_tokens':5}})
        client=BudgetedClient(raw,budget);client.complete('vision','s',{})
        with self.assertRaisesRegex(ExecutionLimitError,'reported-token'):client.complete('vision','s',{})
        self.assertEqual(raw.calls,1)
        budget2=ExecutionBudget(self.root/'another',{});budget2.state['started_at']=0
        with self.assertRaisesRegex(ExecutionLimitError,'elapsed'):budget2.check()

    def test_repeated_failure_names_the_rejected_subtask_and_preserves_the_underlying_error(self):
        budget = ExecutionBudget(self.root,{})
        raw = Provider(); client = BudgetedClient(raw,budget)
        payload = {'planning_stage':'measurement_probes', 'stage_rejection':{
            'error':'probe stop.release.hold_seconds=0.2; allowed [0.4, 3]',
            'previous_response':{'implementation':'HIDDEN REJECTED CODE'}}}
        client.complete('planner','s',payload)
        with self.assertRaises(ExecutionLimitError) as caught:
            client.complete('planner','s',payload)
        message = str(caught.exception)
        self.assertIn('subtask=measurement_probes',message)
        self.assertIn('hold_seconds=0.2',message)
        self.assertIn('[0.4, 3]',message)
        self.assertNotIn('HIDDEN REJECTED CODE',message)
        self.assertEqual(raw.calls,1)
        self.assertEqual(read_json(self.root/'execution-budget.json')['stopped_reason'],message)

    def test_rule_supervisor_needs_no_model_or_training_candidate(self):
        raw=Mock()
        view=progress_view({'status':'passed'},{'live_pipeline_passed':True})
        result=SupervisorAgent({'llm':{}},self.root,raw).decide(view)
        self.assertEqual(result['decision'],'done');raw.complete.assert_not_called()
        self.assertFalse((self.root/'training-data/candidates').exists())
        self.assertFalse(read_json(self.root/'supervisor-routing.json')['model_call_issued'])
        view['live_pipeline_passed']=False
        with self.assertRaisesRegex(ValueError,'unaudited'):SupervisorAgent({'llm':{}},self.root,raw).decide(view)

    def test_progress_view_has_bounded_state_without_candidate_history(self):
        state={'status':'failed','history':[{'status':'plc_compiled','diagnostics':'large'*20000}]}
        view=progress_view(state,{'live_pipeline_passed':False,'artifact_authorship':['large']*5000})
        value=view['evidence'][0]['value']
        self.assertNotIn('history',value);self.assertEqual(value['history_count'],1)
        self.assertEqual(value['original_sha256'],canonical_hash(state))

    def test_review_scope_preserves_runtime_acceptance_and_semantics(self):
        payload={'phase':'scene','task':{'process':[{'id':'exit'}],'acceptance':[{'kind':'fixture'}],
            'verification_tasks':['huge']},'plc':{'implementation':'same source','globals':['duplicate']},
            'manifest':{'objects':[{'id':'belt','tags':['duplicate']}]},
            'io_contract':{'tags':[{'key':'stop','forced_value':True}],
                'component_semantics':[{'tag_key':'stop','semantics':{'raw_unpressed':True},'evidence':['path']}]}}
        before=deepcopy(payload); scene=project_review_payload(payload,'vision')
        self.assertNotIn('acceptance',scene['task']);self.assertEqual(payload,before)
        payload['phase']='runtime';runtime=project_review_payload(payload,'vision')
        self.assertEqual(runtime['task']['acceptance'],payload['task']['acceptance'])
        self.assertEqual(runtime['io_contract']['tags'],payload['io_contract']['tags'])
        self.assertEqual(runtime['io_contract']['component_semantics'][0]['semantics'],{'raw_unpressed':True})
        self.assertEqual(runtime['plc']['implementation'],'same source')

    def test_online_diagnostics_and_named_running_signal_survive_projection(self):
        snapshot={'verified':False,'source_program_sha256':'source','observed_at':'time',
            'online_probe':{'connected':True,'application_state':'Stop','values':{'x':'TRUE'},'monitor_error':'error'}}
        ev={'records':[{'id':'s','kind':'tag_sample','tags':[{'id':'running','name':'Running',
            'kind':'Input','value':True,'isForced':False,'openCircuit':None,'shortCircuit':False}]},
            {'id':'diag','kind':'plc_diagnostics','result':snapshot}]}
        view=model_review_evidence(ev)
        diag=next(r for r in view['records'] if r['kind']=='plc_diagnostics')['result']
        self.assertEqual(diag['online_probe'],snapshot['online_probe']);self.assertFalse(diag['verified'])
        signal=view['signal_index'][0]
        self.assertTrue(signal['first_raw']);self.assertEqual(signal['key'],'running')
        self.assertEqual(signal['unknown_status_samples'],1)

    def test_status_dictionary_roundtrips_faults_and_unknowns(self):
        rows=[{'id':str(i),'kind':'tag_sample','tags':[{'id':'s','value':i,
            'isForced':status[0],'openCircuit':status[1],'shortCircuit':status[2]}]}
            for i,status in enumerate(([False,False,False],[True,False,False],[False,None,True]))]
        view=model_review_evidence({'records':rows},compact_status=True)
        for row in view['records']:
            original=rows[int(row['id'])]['tags'][0];col,value,pattern=row['tag_rows'][0]
            self.assertEqual(view['tag_columns'][col],original['id']);self.assertEqual(value,original['value'])
            self.assertEqual(view['tag_status_patterns'][pattern],[original[k] for k in ('isForced','openCircuit','shortCircuit')])

    def test_new_partial_handoff_cannot_select_old_passing_feedback(self):
        write_json(self.root/'runtime-feedback.json',{'verdict':'pass'})
        self.assertEqual(latest_runtime_review(self.root)['stage'],'runtime')
        write_json(self.root/'live-handoff/receipt.json',{'passed':False})
        selected=latest_runtime_review(self.root)
        self.assertEqual(selected['stage'],'live_handoff')
        self.assertEqual(selected['feedback'].name,'restart-feedback.json')
        self.assertFalse(selected['feedback'].exists())

    def test_solver_selection_uses_measured_capability_not_a_requested_tool_flag(self):
        task={'objects':[{'id':'belt','prefab':'BeltConveyor2M'}]}
        catalog={'native_geometry':{'prefabs':{'BeltConveyor2M':{'component_geometry_local':{}}}}}
        self.assertFalse(relative_layout_capability(task,catalog)['supported'])
        catalog['native_geometry']['prefabs']['BeltConveyor2M']['component_geometry_local']['conveyor']={'direction':[0,0,1]}
        self.assertTrue(relative_layout_capability(task,catalog)['supported'])

    def test_transport_overlap_is_rejected_before_load_without_moving_layout(self):
        bounds={'min':[-.5,-.5,-1],'max':[.5,.5,1]}
        native={'coordinate_system':{'axis_order':['X','Y','Z'],'vertical_axis':'Y','grid_metres':.125},
            'prefabs':{'BeltConveyor2M':{'bounds_local_metres':bounds,'component_geometry_local':{}},
                'Emitter':{'bounds_local_metres':bounds,'component_geometry_local':{'emitter':{'origin':[0,0,0]}}}}}
        catalog={'native_geometry':native,'templates':[{'template_id':k,'prefab':k} for k in native['prefabs']]}
        plan={'objects':[{'id':'belt','template_id':'BeltConveyor2M','position':[20,20,20],'yaw_deg':0},
                          {'id':'source','template_id':'Emitter','position':[20,20,20],'yaw_deg':0}]}
        before=deepcopy(plan)
        with self.assertRaisesRegex(ValueError,'one_axis_separation_options_grid'):validate_layout_geometry(plan,catalog)
        self.assertEqual(plan,before)
        resolved,changes=resolve_transport_clearance(plan,catalog)
        self.assertTrue(changes);self.assertEqual(plan,before)
        validate_layout_geometry(resolved,catalog)
        second,changes2=resolve_transport_clearance(resolved,catalog)
        self.assertEqual(second,resolved);self.assertEqual(changes2,[])
        self.assertEqual(resolved['objects'][1]['position'][::2],plan['objects'][1]['position'][::2])


if __name__=='__main__':unittest.main()
