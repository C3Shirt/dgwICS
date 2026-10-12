"""Global progress view and a model supervisor constrained to evidence-backed next actions."""
from copy import deepcopy

from .contracts import require, canonical_hash, write_json
from .llm import ModelClient
from .training import RecordingClient, TrainingRecorder, configured_secret_values
from .execution import BudgetedClient, ExecutionBudget, ExecutionLimitError


SUPERVISOR_PROMPT = """You supervise an industrial simulation multi-agent workflow.
Use only supplied progress, verification, checkpoint and failure evidence. Agent3 owns visual review;
you own the next workflow action. Historical passes never prove current software state. Preserve
verified upstream artifacts and original requirements. Route service, authentication and connection
failures to tools, not PLC/scene design. Never write ST, scene geometry, scripts or shell commands.
Return ONLY {schema_version:1,decision:<one allowed_actions entry>,next_role:<its role>,
reason:<nonempty explanation>,evidence_ids:[<IDs from input.evidence>]}.
Do not invent a root cause, relax acceptance, add optional requirements or declare success without
input.live_pipeline_passed. Your recommendation is checked by tools; you cannot mark stages passed.
"""


def progress_view(state, report=None, resume_plan=None):
    report = report or {}
    failure = report.get('failure') or {}
    target = failure.get('target') or state.get('target')
    category = failure.get('category')
    reusable = (resume_plan or {}).get('reusable', [])
    if report.get('live_pipeline_passed'):
        actions = [{'action':'done', 'role':'none'}]
    elif category == 'execution_limit' or state.get('failure_kind') == 'execution_limit':
        actions = [{'action':'inspect_limits', 'role':'tools'}]
    elif target == 'scene': actions = [{'action':'repair_scene', 'role':'scene'}]
    elif target == 'plc' or category == 'plc_compile': actions = [{'action':'repair_plc', 'role':'plc'}]
    elif category in ('software_or_adapter', 'model_service', 'codesys_tool_or_environment', 'tool_configuration',
                      'cli_or_environment') or target in ('tools', 'investigate'):
        actions = [{'action':'investigate_tools', 'role':'tools'}]
        if reusable: actions.append({'action':'resume', 'role':'tools'})
    elif resume_plan is not None:
        actions = [{'action':'resume', 'role':'tools'}] if 'task' in reusable else [{'action':'replan', 'role':'planner'}]
    else:
        actions = [{'action':'continue_workflow', 'role':state.get('active_role', 'tools')}]
    from .review_context import compact_progress_evidence
    evidence = compact_progress_evidence(state, report, resume_plan)
    observed = {r['status'] for r in state.get('history', [])}
    stages = [{'stage':'task_design','artifact_available':bool(state.get('versions', {}).get('task'))},
        {'stage':'scene_design','artifact_available':bool(state.get('scene_dir')),
         'visual_review_observed':'scene_verified' in observed},
        {'stage':'plc_program','artifact_available':bool(state.get('plc_dir')),
         'compile_observed':'plc_compiled' in observed},
        {'stage':'runtime_acceptance','passed':report.get('live_pipeline_passed',False)}]
    return {'schema_version':1, 'current_phase':state.get('phase'), 'current_role':state.get('active_role'),
        'status':state.get('status'), 'live_pipeline_passed':report.get('live_pipeline_passed', False),
        'reusable_artifacts':reusable, 'stage_progress':stages,'allowed_actions':actions, 'evidence':evidence,
        'completion_authority':'deterministic acceptance plus current visual review and provenance audit'}


class SupervisorAgent:
    def __init__(self, config, output, client=None, budget=None, progress=None):
        llm = deepcopy(config['llm'])
        # Reuse the configured text service unless a separate supervisor service is supplied.
        for key in ('role_providers', 'models', 'request_options'):
            values = llm.setdefault(key, {})
            if 'supervisor' not in values and 'planner' in values: values['supervisor'] = values['planner']
        self.config, self.output, self.llm = config, output, llm
        self.raw_client, self.budget = client, budget
        self._client = None
        from .progress import ModelObserver
        self.observer = ModelObserver(output, callback=progress, secrets=configured_secret_values(config))
        self.recorder = TrainingRecorder(output / 'training-data',
            scenario_id=config.get('training', {}).get('scenario_id'),
            lineage_group=config.get('training', {}).get('lineage_group'),
            source_kind='injected_client' if client is not None else 'live_model',
            event_callback=self.observer.training_event)

    @property
    def client(self):
        if self._client is None:
            raw = self.raw_client or ModelClient(self.llm)
            from .progress import ObservedClient
            from .training import Redactor
            secrets = configured_secret_values(self.config) + (raw.secret_values() if isinstance(raw,ModelClient) else [])
            self.recorder.secrets = tuple(secrets)
            self.observer.redactor = Redactor(secrets)
            self.budget = self.budget or ExecutionBudget(self.output, self.config)
            self._client = BudgetedClient(RecordingClient(ObservedClient(raw,self.observer,self.recorder), self.recorder), self.budget)
        return self._client

    def decide(self, view):
        unique = len(view['allowed_actions']) == 1
        if unique:
            action = view['allowed_actions'][0]
            require(action['action'] != 'done' or view.get('live_pipeline_passed') is True,
                    'deterministic routing cannot grant unaudited success')
            value = {'schema_version':1, 'decision':action['action'], 'next_role':action['role'],
                'reason':'Exactly one evidence-constrained next action is available.',
                'evidence_ids':[r['id'] for r in view['evidence']]}
            self.observer.emit('supervisor_rule_decision',{'active_role':'supervisor','phase':'decision'},
                               decision=value['decision'],next_role=value['next_role'],
                               reason='当前证据只允许一个动作，采用规则路由，未调用模型')
        else:
            value = self.client.complete('supervisor', SUPERVISOR_PROMPT, view)
        require(isinstance(value,dict) and set(value) == {'schema_version','decision','next_role','reason','evidence_ids'},
                'supervisor must return only the decision contract')
        require(value['schema_version'] == 1, 'supervisor schema_version invalid')
        require({'action':value['decision'],'role':value['next_role']} in view['allowed_actions'],
                'supervisor decision exceeds allowed recovery actions')
        require(isinstance(value['reason'],str) and bool(value['reason'].strip()), 'supervisor reason missing')
        ids = {r['id'] for r in view['evidence']}
        require(isinstance(value['evidence_ids'],list) and bool(value['evidence_ids']) and
                all(isinstance(k,str) and k in ids for k in value['evidence_ids']), 'supervisor cited unknown evidence')
        write_json(self.output / 'supervisor-routing.json', {'schema_version':1,
            'decision_source':'deterministic_rule' if unique else 'model',
            'model_call_issued':not unique, 'input_sha256':canonical_hash(view), 'decision':value,
            'training_candidate_created':not unique})
        return value

    def investigate(self, task, manifest, io_contract, evidence, versions, program, review, handoff):
        from .agents import complete_role, manifest_for_review
        from .contracts import ContractError, canonical_hash, validate_feedback
        from .coordination import model_review_evidence, validate_diagnoses
        from .prompts import PROMPTS
        require(evidence.get('versions')==versions,'investigation evidence is stale')
        selected = model_review_evidence(evidence, max_samples=24, io_contract=io_contract, compact_status=True)
        source = {'id':'current-plc-source','kind':'plc_source','sha256':canonical_hash(program),
                  'program_reference':'input.plc (one source copy)', 'versions':versions}
        selected['records'] = selected['records'] + [source]
        payload = {'task':task,'manifest':manifest_for_review(manifest),'io_contract':io_contract,
                   'evidence':selected,'versions':versions,'phase':'runtime','plc':program,
                   'review_feedback':review,'review_process_ids':[p['id'] for p in task['process']]}
        for attempt in range(2):
            value = complete_role(self.client,'supervisor',PROMPTS['supervisor'],payload,handoff)
            try:
                require(value.get('phase')=='runtime','investigation feedback has wrong phase')
                result = validate_feedback(value,versions,manifest,{r['id'] for r in selected['records']})
                require(result['verdict'] in ('repair','inconclusive'),'investigation cannot declare a pass')
                validate_diagnoses(result,task,io_contract,selected['records'])
                tags = {r['id'] for r in selected['records'] if r['kind']=='tag_sample'}
                for finding in result['findings']:
                    require(finding['target'] in ('plc','investigate'),'investigation exceeds its scope')
                    if result['verdict']=='repair':
                        require(finding['target']=='plc' and 'current-plc-source' in finding['evidence_ids'] and
                                bool(tags.intersection(finding['evidence_ids'])),
                                'supported investigation needs current source and measured tag evidence')
                return result
            except ExecutionLimitError:
                raise
            except ContractError as exc:
                if attempt==1: raise
                payload['format_feedback']={'error':str(exc),'previous_response':value,
                    'instruction':'Repair only the response contract; preserve uncertainty when evidence is insufficient.'}
