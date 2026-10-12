"""Serial orchestration and versioned artifacts for the four model roles."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import time
import uuid

from .agents import PLCAgent, PlannerAgent, SceneAgent, VisionAgent
from .binding_tools import BindingConfigurationError
from .contracts import ContractError, canonical_hash, file_hash, read_json, require, version_tuple, write_json
from .coordination import artifact_digest, attach_component_semantics, check_process_scene, make_handoff
from .llm import ModelClient, ModelServiceError, ModelOutputError
from .prompts import policy_manifest
from .runtime import FactoryIOClient, collect_evidence, run_hook, utc_now
from .scene import SceneError, build_catalog, build_scene
from .validation import events_from_single_item_trace, validate_events
from .training import RecordingClient, TrainingRecorder, Redactor, configured_secret_values
from .verification import VerificationLedger, VERIFIERS
from .memory import checkpoint, inspect_resume, restore_resume, plc_io_design_hash
from .execution import ExecutionBudget, BudgetedClient, ExecutionLimitError, execution_limits


def load_config(path: Path) -> dict:
    config = read_json(path)
    execution_limits(config)
    require(config.get("schema_version") == 1, "config schema_version must be 1")
    require(type(config.get('staged_scene_design', False)) is bool, 'staged_scene_design must be boolean')
    require(type(config.get('auto_bool_mappings', False)) is bool, 'auto_bool_mappings must be boolean')
    require(type(config.get('apply_process_constraints', False)) is bool, 'apply_process_constraints must be boolean')
    require(type(config.get('staged_task_design', False)) is bool, 'staged_task_design must be boolean')
    require(type(config.get('staged_measurement_design',False)) is bool, 'staged_measurement_design must be boolean')
    require(not config.get('staged_measurement_design') or config.get('staged_task_design'),
            'staged_measurement_design requires staged_task_design')
    require(type(config.get('tool_plc_io', False)) is bool, 'tool_plc_io must be boolean')
    require(type(config.get('tool_scene_layout', False)) is bool, 'tool_scene_layout must be boolean')
    require(type(config.get('codesys',{}).get('ensure_local_services',False)) is bool,
            'codesys.ensure_local_services must be boolean')
    require(type(config.get('factoryio',{}).get('synchronized_lifecycle',False)) is bool,
            'factoryio.synchronized_lifecycle must be boolean')
    require(config.get('factoryio',{}).get('sync_reset_mode','warm') in ('warm','cold'),
            'synchronized reset mode must be warm or cold')
    for name in ("max_scene_repairs", "max_plc_repairs", "max_layout_repairs"):
        value = config.get(name, 2 if name=='max_layout_repairs' else 3)
        require(type(value) is int and 0 <= value <= 10, f"{name} must be integer 0..10")
        config[name] = value
    config["scene_sources"] = [str((path.parent / p).resolve()) for p in config["scene_sources"]]
    if config.get("llm", {}).get("provider_config"):
        config["llm"]["provider_config"] = str((path.parent / config["llm"]["provider_config"]).resolve())
    return config


@contextmanager
def run_lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    path = output / ".writer.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ContractError("another writer owns this run directory; use a different output directory") from None
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


class Workflow:
    def __init__(self, config: dict, output: Path, client=None, progress=None, interaction=None):
        self.interaction=interaction
        self.config, self.output = config, output.resolve()
        injected_client = client is not None
        client = client or ModelClient(config["llm"])
        raw_client=client
        secrets = configured_secret_values(config) + (client.secret_values() if isinstance(client, ModelClient) else [])
        self.redactor = Redactor(secrets)
        self.progress = progress
        self.memory_context = {'observations': []}
        training_config = config.get("training", {})
        self.training = None
        from .progress import ModelObserver, ObservedClient
        self.model_observer = ModelObserver(self.output, callback=progress, secrets=secrets)
        if training_config.get("enabled", True):
            self.training = TrainingRecorder(self.output / "training-data",
                scenario_id=training_config.get("scenario_id"), lineage_group=training_config.get("lineage_group"),
                source_kind="injected_client" if injected_client else "live_model",
                secrets=secrets, event_callback=self.model_observer.training_event)
        client = ObservedClient(client, self.model_observer, self.training)
        if self.training is not None:
            client = RecordingClient(client, self.training)
        self.budget = ExecutionBudget(self.output, config)
        client = BudgetedClient(client, self.budget)
        if interaction is not None:
            from .interactive import ManualClient, model_info
            client=ManualClient(client,interaction,self.budget,lambda role,payload:model_info(raw_client,role,payload))
        self.planner, self.author = PlannerAgent(client, config.get('staged_task_design', False),config.get('staged_measurement_design',False)), SceneAgent(client, config.get('staged_scene_design', False),
            config.get('auto_bool_mappings', False), config.get('apply_process_constraints', False), config.get('tool_scene_layout',False))
        self.observer, self.plc_agent = VisionAgent(client), PLCAgent(client, config.get('tool_plc_io', False))
        self.state = {"schema_version": 1, "mode": "live", "status": "created", "history": [],
                      "scene_revision": 0, "plc_revision": 0, "created_at": utc_now(),
                      "policy": policy_manifest(), "repairs": [], "handoff_count": 0}
        self.state.update(phase="initialization", active_role="tools")
        self.state["training_collection"] = {"enabled": self.training is not None,
            "directory": str(self.training.directory) if self.training else None,
            "run_id": self.training.run_id if self.training else None,
            "source_kind": self.training.source_kind if self.training else None}

    def trace(self, event: str, **details):
        if event in ('tool_requested', 'model_handoff'): self.budget.check()
        self.output.mkdir(parents=True, exist_ok=True)
        record = self.redactor.clean({"timestamp": utc_now(), "event": event,
            "phase": self.state.get("phase"), "active_role": self.state.get("active_role"),
            "versions": self.state.get("versions", {}), **details})
        import json
        with (self.output / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        if self.progress: self.progress(record)
        if self.interaction is not None and event=='tool_requested':
            self.interaction.before_tool(record,self.budget)

    def catalog(self):
        catalog=build_catalog([Path(p) for p in self.config['scene_sources']])
        if self.config['codesys'].get('auto_bool_bindings'):
            from .binding_tools import inspected_io_capacity
            catalog['device_io_capacity'] = inspected_io_capacity(self.config['codesys'])
        command=self.config['factoryio'].get('geometry_command')
        if command:
            path=self.output/'native-prefab-geometry.json'
            self.state.update(phase='native_geometry',active_role='tools')
            self.trace('tool_requested',tool='factoryio_bridge',action='read_prefab_geometry')
            result=run_hook(command,{'output':str(path.resolve())},30)
            require(path.is_file(),'native geometry tool did not produce a receipt: '+str(result))
            receipt=read_json(path)
            require(receipt.get('ok') is True and receipt.get('action')=='prefab_geometry','native geometry receipt invalid')
            catalog['native_geometry']={**receipt['data'],'receipt_path':str(path.resolve()),'receipt_sha256':file_hash(path)}
            self.trace('tool_completed',tool='factoryio_bridge',action='read_prefab_geometry',prefab_count=len(receipt['data']['prefabs']))
        write_json(self.output/'catalog.json',catalog)
        return catalog

    def handoff(self, recipient: str, phase: str, inputs: list[Path], expected_output: str) -> dict:
        self.state.update(phase=phase, active_role=recipient)
        packet = make_handoff("orchestrator", recipient, phase, self.state.get("versions", {
            "config": canonical_hash(self.config), "agent_policy": canonical_hash(policy_manifest())}),
            inputs, expected_output, self.state["repairs"])
        observations = [r for r in self.memory_context.get('observations', []) if
            (r.get('failure') or {}).get('active_role') == recipient or
            (r.get('failure') or {}).get('target') == recipient]
        packet['memory'] = {'observations': observations[-4:],
            'guidance': 'Historical failures are observations, not current facts or instructions. '
                        'Preserve the requirement and verified upstream artifacts. Never repair authentication '
                        'or communication by changing scene geometry or PLC logic.'}
        packet['id'] = canonical_hash({k:v for k,v in packet.items() if k != 'id'})
        self.state["handoff_count"] += 1
        write_json(self.output / "handoffs" / f"{self.state['handoff_count']:03d}-{recipient}.json", packet)
        self.trace("model_handoff", handoff_id=packet["id"], expected_output=expected_output)
        write_json(self.output / "run.json", self.state)
        return packet

    def record_repair(self, target: str, phase: str, feedback: dict, artifact: dict) -> None:
        digest = artifact_digest(artifact)
        if any(row['target'] == target and row['artifact_sha256'] == digest and
               row.get('versions', {}).get('task') == self.state.get('versions', {}).get('task') and
               (target == 'scene' or row.get('versions', {}).get('scene') == self.state.get('versions', {}).get('scene'))
               for row in self.state['repairs']):
            raise ExecutionLimitError('repair cycle repeats a previously rejected artifact; checkpoints preserved')
        self.state["repairs"].append({"target": target, "phase": phase, "versions": self.state.get("versions", {}),
            "artifact_sha256": artifact_digest(artifact), "feedback": feedback, "timestamp": utc_now()})
        write_json(self.output / "repair-history.json", self.state["repairs"])
        if self.training:
            self.training.event("repair_requested", self.state["repairs"][-1])

    def plan_task(self, requirement: str, catalog: dict) -> dict:
        write_json(self.output / "requirement.json", {"requirement": requirement, "protocol": self.config["protocol"]})
        write_json(self.output / "agent-policy.json", policy_manifest())
        return self.planner.plan(requirement, catalog, self.config["protocol"], self.handoff("planner", "planning",
            [self.output / "requirement.json", self.output / "catalog.json"], "task.json"))

    def observe_verification(self, verifier, passed, paths):
        if hasattr(self, 'verification'):
            self.verification.observe(verifier, passed, self.state['versions'], paths)
            self.trace('phase_verification', verifier=verifier, passed=passed, evidence=[str(p) for p in paths])

    def approve_layout(self, task, catalog):
        """Obtain a reviewed semantic reference before coordinates or any scene load."""
        from .layout_design import (validate_layout_proposal, validate_layout_review,
            validate_approved_layout, compose_approved_layout, proposal_hash, record_layout_approval)
        proposal_path=self.output/'layout-proposal.json'
        review_path=self.output/'layout-review.json'
        approved_path=self.output/'approved-layout.json'
        if approved_path.is_file():
            approved=validate_approved_layout(task,read_json(approved_path),read_json(proposal_path),read_json(review_path))
            self.approved_layout=approved
            self.trace('artifact_reused',artifact='approved_layout',sha256=canonical_hash(approved))
            checkpoint(self.output,'layout',[proposal_path,review_path,approved_path],self.config,self.state['versions'])
            return approved
        limit=self.config.get('max_layout_repairs',2)
        require(type(limit) is int and limit>=0,'max_layout_repairs must be a nonnegative integer')
        previous,feedback=None,None
        rejected=set()
        for attempt in range(limit+1):
            directory=self.output/'layout'/f'attempt-{attempt:03d}'
            inputs=[self.output/'task.json',self.output/'catalog.json']
            if previous is not None: inputs += [proposal_path,review_path]
            proposal=self.author.propose_layout(task,catalog,previous,feedback,
                self.handoff('scene','layout_proposal',inputs,'layout-proposal.json'))
            validate_layout_proposal(proposal,task)
            write_json(directory/'proposal.json',proposal); write_json(proposal_path,proposal)
            if proposal_hash(proposal) in rejected:
                self.transition('repair_limit_reached',phase='layout_review',target='scene',reason='no_progress',
                                error='scene agent repeated a rejected layout proposal')
                return None
            review=self.planner.review_layout(task,proposal,catalog,
                self.handoff('planner','layout_review',[self.output/'task.json',self.output/'catalog.json',proposal_path],
                             'layout-review.json'))
            validate_layout_review(review,proposal)
            write_json(directory/'review.json',review); write_json(review_path,review)
            self.trace('layout_review_completed',attempt=attempt,verdict=review['verdict'],
                       summary=review['summary'],findings=review['findings'])
            if review['verdict']=='approved':
                approved=compose_approved_layout(task,proposal,review)
                self.approved_layout=approved
                write_json(approved_path,approved)
                record_layout_approval(self.author.client,task,proposal,review,approved)
                checkpoint(self.output,'layout',[proposal_path,review_path,approved_path],self.config,self.state['versions'])
                self.transition('layout_approved',phase='layout_review',active_role='planner',
                                layout_reference_sha256=canonical_hash(approved))
                return approved
            if review['verdict']=='uncertain':
                self.transition('needs_attention',phase='layout_review',target='planner',feedback=review)
                return None
            rejected.add(proposal_hash(proposal)); previous,feedback=proposal,review
        self.transition('repair_limit_reached',phase='layout_review',target='scene',feedback=feedback)
        return None

    def connect_runtime(self, directory: Path, scene_path: Path, versions: dict) -> dict:
        command = self.config["factoryio"].get("connect_command")
        require(command, "factoryio.connect_command must be configured to verify the active driver")
        request = {"schema_version": 1, "request_id": uuid.uuid4().hex, "versions": versions,
            "scene_path": str(scene_path), "scene_sha256": file_hash(scene_path),
            **{k: self.config["protocol"][k] for k in ("host", "port", "unit_id")}}
        request_path, receipt_path = directory / "connection-request.json", directory / "connection-receipt.json"
        write_json(request_path, request)
        run_hook(command, {"request": str(request_path), "confirmation": str(receipt_path),
                          "output": str(directory)}, 30)
        receipt = read_json(receipt_path)
        require(isinstance(receipt, dict), "connection receipt must be object")
        require(isinstance(receipt.get("modbus"), dict), "connection receipt has no observed Modbus state")
        require(all(receipt.get(k) == v for k, v in request.items()) and
                receipt.get("connection_confirmed") is True and receipt.get("driver") == "ModbusTCPClient" and
                receipt.get("modbus", {}).get("connected") is True and all(
                    receipt["modbus"].get(k) == request[k] for k in ("host", "port", "unit_id")),
                "connection receipt does not confirm the active driver and configured endpoint")
        return receipt

    def services_ready(self, receipt_path, phase):
        """Check before model work and again before deployment; never cache service Run."""
        if not (self.config['codesys'].get('deploy') and self.config['codesys'].get('ensure_local_services')):
            return True
        from .codesys_services import service_control
        self.state.update(phase=phase,active_role='tools')
        self.trace('tool_requested',tool='codesys_services',action='preflight')
        services=[service_control(kind,start=True) for kind in ('gateway','runtime')]
        write_json(receipt_path,{'schema_version':1,'services':services,'plc_application_running_verified':False})
        ready=all(s.get('status')=='ok' and s.get('service_running') is True for s in services)
        self.trace('tool_completed',tool='codesys_services',ready=ready,receipt=str(receipt_path))
        if not ready:
            self.transition('needs_configuration',phase=phase,target='tools',failure_kind='tool_configuration',
                error='CODESYS local services are unavailable',diagnostics={'services':services})
        return ready

    def transition(self, status: str, **details) -> dict:
        self.state["status"] = status
        self.state.update(details)
        self.state["history"].append({"status": status, "timestamp": utc_now(), **details})
        write_json(self.output / "run.json", self.state)
        from .supervisor import progress_view
        write_json(self.output / 'supervisor-state.json', progress_view(self.state))
        self.trace("workflow_transition", status=status, **details)
        if self.training:
            self.training.event("workflow_transition", {"status": status, "versions": self.state.get("versions", {}), **details})
        return self.state

    def revision(self, task: dict, plan: dict, catalog: dict, plc: dict | None = None) -> tuple[Path, dict]:
        revision = self.state["scene_revision"]
        directory = self.output / f"scene-{revision:03d}"
        require(not directory.exists(), "revision directory already exists; choose a fresh run output")
        directory.mkdir(parents=True)
        write_json(directory / 'scene-plan.json', plan)
        reference=getattr(self,'approved_layout',None)
        if reference is not None: write_json(directory/'layout-reference.json',reference)
        built = build_scene(plan, catalog, directory / "scene.factoryio", self.config['protocol'],
                            auto_bool_mappings=self.config.get('auto_bool_mappings', False),
                            execution_contract=task.get('execution_contract') if self.config.get('apply_process_constraints') else None)
        actual = {o["id"]: o["prefab"] for o in built["manifest"]["objects"]}
        if reference is not None: built['manifest']['layout_reference_sha256']=canonical_hash(reference)
        mismatches = [{'object_id': o['id'], 'required_prefab': o['prefab'], 'actual_prefab': actual.get(o['id'])}
                      for o in task['objects'] if actual.get(o['id']) != o['prefab']]
        if mismatches:
            raise SceneError('scene omitted or substituted a required task object: ' + str(mismatches))
        contract = built["io_contract"]
        contract["protocol"] = self.config["protocol"]
        if self.config['codesys'].get('auto_bool_bindings'):
            from .binding_tools import bool_bindings
            self.state.update(phase='device_binding', active_role='tools')
            self.trace('tool_requested', tool='codesys_binding', action='derive_verified_bool_channels')
            effective = bool_bindings(self.config['codesys'], contract)
            self.trace('tool_completed', tool='codesys_binding', binding_count=len(effective['bindings']))
            self._effective_codesys = effective
            self.state.update(phase='authoring', active_role='scene')
        else:
            effective = self.config['codesys']
        contract["transport_confirmed"] = effective.get("transport_confirmed", False)
        contract["bindings"] = effective.get("bindings", [])
        if self.config["codesys"].get("wire_encodings"):
            contract["wire_encodings"] = self.config["codesys"]["wire_encodings"]
        attach_component_semantics(built["manifest"], contract, None)
        built["process_validation"] = check_process_scene(task, built["manifest"], contract)
        versions = version_tuple(task, built["manifest"]["scene_sha256"], contract, plc, self.config)
        for name, value in (("scene-plan", plan), ("manifest", built["manifest"]), ("io-contract", contract),
                            ("structural-validation", built["validation"]),
                            ("process-validation", built["process_validation"]), ("versions", versions)):
            write_json(directory / (name + ".json"), value)
        self.transition("scene_generated", scene_dir=str(directory), versions=versions)
        checkpoint(self.output, 'scene', [directory / n for n in
            ('scene-plan.json', 'scene.factoryio', 'structural-validation.json', 'process-validation.json')] +
            ([directory/'layout-reference.json'] if reference is not None else []), self.config, versions)
        if hasattr(self, 'verification'):
            self.verification.invalidate(VERIFIERS, versions)
            self.observe_verification('process_scene', built['process_validation']['passed'], [directory / 'process-validation.json'])
        return directory, built

    def prepare(self, requirement: str) -> dict:
        with run_lock(self.output):
            try:
                require(not (self.output / "task.json").exists(), "run already exists; choose a fresh output directory")
                catalog = self.catalog()
                self.transition("planning")
                task = self.plan_task(requirement, catalog)
                write_json(self.output / "task.json", task)
                self.state['versions'] = {'task':canonical_hash(task),'scene':None,'io':None,'plc':None,
                    'config':canonical_hash(self.config),'agent_policy':canonical_hash(policy_manifest())}
                checkpoint(self.output, 'task', [self.output / 'task.json', self.output / 'catalog.json'], self.config, self.state['versions'])
                self.verification = VerificationLedger(task, self.output)
                require(not task['unresolved'],'task has unresolved requirements: '+'; '.join(task['unresolved']))
                approved_layout=self.approve_layout(task,catalog)
                if approved_layout is None: return self.state
                plan = self.author.plan(task, catalog, None, None, self.handoff("scene", "authoring",
                    [self.output / "task.json", self.output / "catalog.json",self.output/'approved-layout.json'], "scene-plan.json"),
                    layout_reference=approved_layout)
                directory, built = self.revision(task, plan, catalog)
                if not built["process_validation"]["passed"]:
                    return self.transition("needs_attention", target="scene", phase="scene_contract",
                                           diagnostics=built["process_validation"])
                self.transition("awaiting_scene_review", missing=["load confirmation and current visual evidence"],
                                task_unresolved=task["unresolved"])
                return self.state
            except ExecutionLimitError as exc:
                return self.transition('needs_attention',target='tools',failure_kind='execution_limit',error=str(exc))
            except ModelOutputError as exc:
                return self.transition('needs_configuration',target='tools',failure_kind='model_output',
                                       failed_role=self.state.get('active_role'),error=str(exc))
            except ModelServiceError as exc:
                return self.transition('needs_attention', target='tools', failure_kind='model_service',
                                       failed_role=self.state.get('active_role'), error=str(exc))
            except BindingConfigurationError as exc:
                return self.transition('needs_configuration', target='tools', failure_kind='tool_configuration', error=str(exc))
            except KeyboardInterrupt:
                return self.transition('cancelled', reason='keyboard_interrupt')
            except (ContractError, ValueError, OSError) as exc:
                return self.transition("needs_configuration" if "configured" in str(exc) else "failed", error=str(exc))

    def resume(self, plan: dict) -> dict:
        require(not (self.output / 'task.json').exists(), 'resume destination must be a new run')
        restore_resume(plan, self.output)
        self.state['resume'] = {k:plan[k] for k in ('source_run', 'reusable', 'invalidation_reasons')}
        # Retain static identities even if the environment gate fails before a new model call.
        if 'task' in plan['reusable']:
            task=read_json(self.output/'task.json')
            manifest=read_json(self.output/plan['scene_relative']/'manifest.json') if plan.get('scene_relative') else {}
            contract=read_json(self.output/plan['scene_relative']/'io-contract.json') if plan.get('scene_relative') else {}
            program=read_json(self.output/plan['plc_relative']/'program.json') if plan.get('plc_relative') else None
            self.state['versions']=version_tuple(task,manifest.get('scene_sha256'),contract,program,self.config)
            if plan.get('scene_relative'):self.state['scene_dir']=str(self.output/plan['scene_relative'])
            if plan.get('plc_relative'):self.state['plc_dir']=str(self.output/plan['plc_relative'])
        self.trace('resume_restored', **self.state['resume'])
        return self.run(plan['requirement'], resume_seed=plan)

    def run(self, requirement: str, resume_seed=None) -> dict:
        from .plc import export_program, run_codesys
        with run_lock(self.output):
            try:
                require(resume_seed is not None or not (self.output / "task.json").exists(), "run already exists; choose a fresh output directory")
                write_json(self.output/'requirement.json',{'requirement':requirement,'protocol':self.config['protocol']})
                if not self.services_ready(self.output/'environment-preflight.json','environment_preflight'):
                    return self.state
                self.transition("planning", phase="planning", active_role="planner")
                reuse = set((resume_seed or {}).get('reusable', []))
                if 'task' in reuse:
                    catalog, task = read_json(self.output / 'catalog.json'), read_json(self.output / 'task.json')
                    write_json(self.output / 'requirement.json', {'requirement':requirement, 'protocol':self.config['protocol']})
                    write_json(self.output / 'agent-policy.json', policy_manifest())
                    self.trace('artifact_reused', artifact='task', source_run=resume_seed['source_run'])
                else:
                    catalog = self.catalog()
                    task = self.plan_task(requirement, catalog)
                write_json(self.output / "task.json", task)
                self.state['versions'] = {'task':canonical_hash(task),'scene':None,'io':None,'plc':None,
                    'config':canonical_hash(self.config),'agent_policy':canonical_hash(policy_manifest())}
                checkpoint(self.output, 'task', [self.output / 'task.json', self.output / 'catalog.json'], self.config,
                           {'task':canonical_hash(task)})
                self.verification = VerificationLedger(task, self.output)
                require(not task["unresolved"], "task has unresolved requirements: " + "; ".join(task["unresolved"]))
                approved_layout=self.approve_layout(task,catalog)
                if approved_layout is None: return self.state
                if 'scene' in reuse:
                    directory = self.output / resume_seed['scene_relative']
                    plan = read_json(directory / 'scene-plan.json')
                else:
                    seed=(resume_seed or {}).get('repair_seed')
                    if seed:
                        require(seed.get('accepted') is False and seed['role']=='scene','invalid repair seed')
                        require(all(file_hash(Path(ref['path']))==ref['sha256'] for ref in seed['inputs']),
                                'rejected layout evidence changed after recovery inspection')
                    plan = self.author.plan(task, catalog, read_json(Path(seed['previous_plan_path'])) if seed else None,
                        seed['feedback'] if seed else None, self.handoff("scene", "repair" if seed else "authoring",
                        [self.output / "task.json", self.output / "catalog.json",self.output/'approved-layout.json'] +
                        ([Path(ref['path']) for ref in seed['inputs']] if seed else []), "scene-plan.json"),
                        layout_reference=approved_layout)
                previous_program, plc_feedback = None, None
                scene_feedback = None
                for scene_attempt in range(self.config["max_scene_repairs"] + 1):
                    self.state["scene_revision"] = scene_attempt + (int(Path(resume_seed['scene_relative']).name.split('-')[1])
                        if 'scene' in reuse else 0)
                    if scene_feedback:
                        previous_plan = plan
                        plan = self.author.plan(task, catalog, plan, scene_feedback, self.handoff("scene", "repair",
                            [self.output / "task.json", directory / "scene-plan.json",self.output/'approved-layout.json',
                             self.output / "repair-history.json"], "complete corrected scene-plan.json"),
                            layout_reference=approved_layout)
                        if artifact_digest(plan) == artifact_digest(previous_plan):
                            return self.transition("repair_limit_reached", phase="scene", reason="no_progress",
                                                   error="scene repair returned an unchanged artifact")
                    try:
                        if scene_attempt == 0 and 'scene' in reuse:
                            directory = self.output / resume_seed['scene_relative']
                            manifest, contract, _ = verify_artifact_versions(directory)
                            require(read_json(directory/'layout-reference.json')==approved_layout and
                                    manifest.get('layout_reference_sha256')==canonical_hash(approved_layout),
                                    'reused scene belongs to another approved layout')
                            if self.config['codesys'].get('auto_bool_bindings'):
                                from .binding_tools import bool_bindings
                                self._effective_codesys = bool_bindings(self.config['codesys'], contract)
                            built = {'manifest':manifest, 'io_contract':contract,
                                     'process_validation':check_process_scene(task,manifest,contract)}
                            self.state['versions'] = version_tuple(task,manifest['scene_sha256'],contract,None,self.config)
                            self.transition('scene_reused', scene_dir=str(directory))
                            self.observe_verification('process_scene', built['process_validation']['passed'],
                                                      [directory / 'process-validation.json'])
                            self.trace('artifact_reused',artifact='scene',source_run=resume_seed['source_run'])
                            checkpoint(self.output, 'scene', [directory / n for n in ('scene-plan.json', 'scene.factoryio',
                                'structural-validation.json', 'process-validation.json','layout-reference.json')], self.config, self.state['versions'])
                        else:
                            directory, built = self.revision(task, plan, catalog)
                    except SceneError as error:
                        # A rejected model plan is repaired by Agent2 with the actual validator error.
                        scene_feedback = {"phase": "scene_contract", "diagnostics": {"error": str(error)}}
                        self.record_repair("scene", "scene_contract", scene_feedback, plan)
                        self.transition("scene_contract_rejected", error=str(error))
                        # The following handoff needs a committed previous candidate even if no XML was written.
                        directory = self.output / f"scene-{self.state['scene_revision']:03d}"
                        write_json(directory / "scene-plan.json", plan)
                        continue
                    manifest, contract = built["manifest"], built["io_contract"]
                    if not built["process_validation"]["passed"]:
                        scene_feedback = {"phase": "scene_contract", "versions": self.state["versions"],
                                          "diagnostics": built["process_validation"]}
                        self.record_repair("scene", "scene_contract", scene_feedback, plan)
                        continue
                    scene_path = (directory / "scene.factoryio").resolve()
                    request = {"schema_version": 1, "request_id": uuid.uuid4().hex,
                               "scene_path": str(scene_path), "scene_sha256": manifest["scene_sha256"],
                               "versions": self.state["versions"]}
                    write_json(directory / "load-request.json", request)
                    receipt_path = directory / "load-receipt.json"
                    self.state.update(phase="scene_load", active_role="tools")
                    self.trace("tool_requested", tool="factoryio_bridge", action="load", scene_sha256=manifest['scene_sha256'])
                    load_result = run_hook(self.config["factoryio"].get("load_command"),
                        {"scene": str(scene_path), "output": str(directory),
                         "request": str(directory / "load-request.json"), "confirmation": str(receipt_path)}, 30)
                    self.transition("scene_load_requested", load_result=load_result)
                    if not receipt_path.is_file():
                        return self.transition("needs_attention", phase="scene_load", target="tools",
                                               error="loader did not confirm this scene revision")
                    receipt = read_json(receipt_path)
                    require(isinstance(receipt, dict), "loader receipt must be JSON object")
                    require(receipt.get("schema_version") == 1 and receipt.get("request_id") == request["request_id"] and
                            receipt.get("software_load_confirmed") is True and
                            receipt.get("scene_sha256") == request["scene_sha256"] == file_hash(scene_path) and
                            Path(receipt.get("scene_path", "")).resolve() == scene_path and
                            isinstance(receipt.get("confirmation_method"), str) and bool(receipt["confirmation_method"]),
                            "loader receipt does not confirm the requested scene revision")
                    self.trace('tool_completed', tool='factoryio_bridge', action='load', receipt=str(receipt_path),
                               receipt_sha256=file_hash(receipt_path))
                    # Scope knowledge to the application version observed by this exact loader receipt.
                    attach_component_semantics(manifest, contract, receipt.get("app_version"))
                    from .geometry_tools import loaded_geometry, attach_button_semantics
                    geometry=loaded_geometry(receipt,manifest)
                    write_json(directory/'geometry-validation.json',geometry)
                    attach_button_semantics(contract,manifest,receipt)
                    self.state["versions"] = version_tuple(task, manifest["scene_sha256"], contract, None, self.config)
                    write_json(directory / "manifest.json", manifest)
                    write_json(directory / "io-contract.json", contract)
                    write_json(directory / "versions.json", self.state["versions"])
                    checkpoint(self.output, 'scene_io', [directory / 'manifest.json', directory / 'io-contract.json',
                        directory / 'versions.json'], self.config, self.state['versions'])
                    if not geometry.get('spatial_checks_applied'):
                        return self.transition('needs_attention',phase='scene_geometry',target='tools',
                            error=geometry.get('reason','Spatial connections were not checked; a visual pass cannot replace missing native measurements.'),
                            diagnostics=geometry)
                    if not geometry['passed']:
                        scene_feedback={'phase':'scene_geometry','versions':self.state['versions'],'diagnostics':geometry,
                                        'native_geometry':receipt['native_geometry']}
                        self.record_repair('scene','scene_geometry',scene_feedback,plan)
                        continue
                    evidence = collect_evidence(self.config["factoryio"], directory / "visual",
                                                self.state["versions"], contract, seconds=0)
                    evidence["records"].append({"id": "scene-load-receipt", "kind": "scene_load_receipt",
                                                "timestamp": utc_now(), "receipt": receipt})
                    evidence["records"].append({"id": "scene-geometry-result", "kind": "scene_geometry",
                                                "timestamp": utc_now(), "result": geometry})
                    write_json(directory / "visual" / "evidence.json", evidence)
                    require(evidence["reconciliation"] and evidence["reconciliation"]["passed"],
                            "loaded scene tags do not match generated scene")
                    self.transition("scene_loaded")
                    approved_layout_path=directory/'layout-reference.json'
                    require(read_json(approved_layout_path)==approved_layout,'scene layout reference changed during execution')
                    scene_feedback = self.observer.review(task, manifest, contract, evidence,
                        self.state["versions"], "scene", None, self.handoff("vision", "scene_review",
                            [directory / "manifest.json", directory / "io-contract.json",
                             directory / "visual" / "evidence.json"] +
                            ([approved_layout_path] if approved_layout is not None else []), "scene-feedback.json"),
                        **({'approved_layout':approved_layout} if approved_layout is not None else {}))
                    write_json(directory / "scene-feedback.json", scene_feedback)
                    self.observe_verification('scene_visual', scene_feedback['verdict'] == 'pass',
                                              [directory / 'scene-feedback.json', receipt_path, directory / 'visual/evidence.json'])
                    if scene_feedback["verdict"] != "pass":
                        targets = {f["target"] for f in scene_feedback["findings"]}
                        if scene_feedback["verdict"] == "inconclusive" or targets != {"scene"}:
                            return self.transition("needs_attention", feedback=scene_feedback)
                        self.record_repair("scene", "scene_review", scene_feedback, plan)
                        continue
                    self.transition("scene_verified")
                    for plc_attempt in range(self.config["max_plc_repairs"] + 1):
                        self.state["plc_revision"] = plc_attempt
                        codesys = getattr(self, '_effective_codesys', self.config['codesys'])
                        reuse_plc = scene_attempt == 0 and plc_attempt == 0 and 'plc' in reuse
                        if reuse_plc:
                            source_contract = read_json(Path(resume_seed['source_run']) / resume_seed['scene_relative'] / 'io-contract.json')
                            reuse_plc = plc_io_design_hash(source_contract) == plc_io_design_hash(contract)
                            if not reuse_plc:
                                self.trace('artifact_invalidated',artifact='plc',reason='fresh loaded I/O semantics changed')
                                self.state['resume']['reusable'].remove('plc')
                                self.state['resume']['invalidation_reasons'].append('fresh loaded I/O semantics changed')
                        if reuse_plc:
                            program = read_json(self.output / resume_seed['plc_relative'] / 'program.json')
                            self.trace('artifact_reused',artifact='plc',source_run=resume_seed['source_run'])
                        else:
                            program = self.plc_agent.program(task, contract, plc_feedback, previous_program,
                                codesys, self.handoff("plc", "repair" if plc_feedback else "generation",
                                    [self.output / "task.json", directory / "io-contract.json"] +
                                    ([self.output / "repair-history.json", plc_dir / "program.json"] if plc_feedback else []),
                                    "complete model logic fragment; tools own I/O globals and bindings" if self.config.get('tool_plc_io') else
                                    "complete program.json with declarations, globals and bindings"))
                        if plc_feedback and previous_program and artifact_digest(program) == artifact_digest(previous_program):
                            return self.transition("repair_limit_reached", phase="plc", reason="no_progress",
                                                   error="PLC repair returned an unchanged artifact")
                        previous_program = program
                        plc_dir = directory / f"plc-{plc_attempt:03d}"
                        export_program(program, contract, plc_dir)
                        versions = version_tuple(task, manifest["scene_sha256"], contract, program, self.config)
                        write_json(plc_dir / "versions.json", versions)
                        self.transition("plc_generated", plc_dir=str(plc_dir), versions=versions)
                        checkpoint(self.output, 'plc', [plc_dir / 'program.json', plc_dir / 'versions.json'], self.config, versions)
                        self.verification.invalidate(('plc_compile', 'modbus_connection', 'runtime_acceptance'), versions)
                        self.state.update(phase="compile", active_role="tools")
                        if not self.services_ready(plc_dir/'service-preflight.json','deployment_preflight'):
                            return self.state
                        self.state.update(phase='compile',active_role='tools')
                        self.trace("tool_requested", tool="codesys", action="compile_and_deploy")
                        if codesys.get('deploy') and self.config['factoryio'].get('prepare_deployment_command'):
                            receipt_path = plc_dir / 'driver-disconnect-receipt.json'
                            run_hook(self.config['factoryio']['prepare_deployment_command'],
                                     {'output':str(receipt_path.resolve())},20)
                            receipt = read_json(receipt_path)
                            require(receipt.get('ok') is True and
                                    receipt.get('data',{}).get('modbus',{}).get('socket_connected') is False,
                                    'Factory I/O disconnect was not confirmed before PLC deployment')
                        if codesys.get('auto_bool_bindings'):
                            from .binding_tools import compile_bound_program
                            result = compile_bound_program(codesys, plc_dir, program, contract, codesys.get('deploy', False))
                        else:
                            result = run_codesys(codesys, plc_dir, program=program,
                                                 io_contract=contract, deploy=codesys.get("deploy", False))
                        write_json(plc_dir / "codesys-result.json", result)
                        self.observe_verification('plc_compile', all(result.get(k) is True for k in
                            ('compiled', 'bindings_verified', 'task_program_verified')), [plc_dir / 'codesys-result.json'])
                        self.trace('tool_completed', tool='codesys', compiled=result.get('compiled', False),
                                   started=result.get('started', False), result=str(plc_dir / 'codesys-result.json'))
                        if not result.get("compiled", False):
                            bridge_result = result.get("bridge_result", {})
                            verified_errors = (bridge_result.get("build_completed") is True and
                                bridge_result.get("messages_verified") is True and
                                type(result.get("error_count")) is int and result["error_count"] > 0 and
                                result.get("source_unchanged") is True)
                            if verified_errors:
                                plc_feedback = {"phase": "compile", "versions": versions, "diagnostics": result}
                                self.record_repair("plc", "compile", plc_feedback, program)
                                continue
                            return self.transition("needs_configuration" if not result.get("command") else "needs_attention",
                                                   phase="compile", target="tools", diagnostics=result)
                        self.transition("plc_compiled")
                        if not result.get('started',False):
                            return self.transition('needs_configuration',phase='deployment',target='tools',
                                error='PLC compile passed; deployment/start was not confirmed',diagnostics=result)
                        require(self.config["factoryio"].get("runtime_control") is True,
                                "runtime_control must be configured for automatic scene run/reset")
                        try:
                            self.state.update(phase="connection", active_role="tools")
                            self.trace("tool_requested", tool="factoryio_bridge", action="connect_modbus")
                            connection = self.connect_runtime(plc_dir, scene_path, versions)
                            self.observe_verification('modbus_connection', True, [plc_dir / 'connection-receipt.json'])
                        except (ContractError, OSError, ValueError) as exc:
                            return self.transition("needs_attention", phase="connection", target="tools", error=str(exc))
                        runtime = FactoryIOClient(self.config["factoryio"])
                        startup = None
                        try:
                            self.state.update(phase="runtime", active_role="tools")
                            self.trace("tool_requested", tool="factoryio_web_api", action="run_and_collect")
                            try:
                                if self.config['factoryio'].get('synchronized_lifecycle'):
                                    from .synchronized_runtime import SynchronizedRuntime
                                    startup = SynchronizedRuntime(self.config['factoryio'], codesys, plc_dir,
                                        plc_dir/'runtime-startup')
                                    stopped = startup.reset_both(self.config['factoryio'].get('sync_reset_mode','warm'))
                                    running = startup.start_both()
                                else:
                                    runtime.control("run", False)
                                    stopped = runtime.wait_running(False)
                                    runtime.control("reset", True)
                                    runtime.control("reset", False)
                                    runtime.control("run", True)
                                    running = runtime.wait_running(True)
                                evidence = collect_evidence(self.config["factoryio"], plc_dir / "runtime", versions, contract, manifest=manifest, task=task)
                            except ContractError as exc:
                                # Preserve the failed start state before cleanup changes either end.
                                diagnostics = getattr(exc, 'diagnostics', {})
                                if startup is not None:
                                    try: diagnostics = {**diagnostics, 'native_status': startup.native_call('status')}
                                    except (ContractError, OSError, ValueError) as observation_error:
                                        diagnostics = {**diagnostics, 'native_status_error':str(observation_error)}
                                write_json(plc_dir/'runtime-control-failure.json',
                                    {'timestamp':utc_now(), 'versions':versions, 'error':str(exc), 'diagnostics':diagnostics})
                                raise
                            finally:
                                if self.config['factoryio'].get('synchronized_lifecycle'):
                                    from .synchronized_runtime import SynchronizedRuntime
                                    SynchronizedRuntime(self.config['factoryio'], codesys, plc_dir,
                                        plc_dir/'runtime-stop').stop_both()
                                else:
                                    runtime.control("run", False)
                                    runtime.wait_running(False)
                        except ContractError as exc:
                            return self.transition("needs_attention", phase="runtime_control", target="tools", error=str(exc))
                        require(evidence["reconciliation"]["passed"], "scene changed during runtime capture")
                        evidence["records"].append({"id": "codesys-diagnostics", "kind": "plc_diagnostics",
                                                    "timestamp": utc_now(), "result": result})
                        evidence["records"].append({"id": "runtime-startup", "kind": "startup_receipt",
                            "timestamp": utc_now(), "connection": connection, "stopped": stopped, "running": running})
                        if self.config.get("event_decoder"):
                            evidence["records"].extend(events_from_single_item_trace(evidence, self.config["event_decoder"]))
                        write_json(plc_dir / "runtime" / "evidence.json", evidence)
                        acceptance = validate_events(task, evidence, versions, contract)
                        write_json(plc_dir / "acceptance.json", acceptance)
                        evidence["records"].append({"id": "deterministic-acceptance", "kind": "acceptance_result",
                            "timestamp": utc_now(), "result": acceptance,
                            'source_record_ids':list(dict.fromkeys(ref for rule in acceptance.get('rules',[])
                                for ref in rule.get('source_record_ids',[])))})
                        write_json(plc_dir / "runtime" / "evidence.json", evidence)
                        feedback = self.observer.review(task, manifest, contract, evidence, versions, "runtime", program,
                            self.handoff("vision", "runtime_review", [plc_dir / "program.json",
                                plc_dir / "runtime" / "evidence.json", plc_dir / "acceptance.json"], "runtime-feedback.json"))
                        write_json(plc_dir / "runtime-feedback.json", feedback)
                        review_evidence_path = plc_dir / 'runtime/evidence.json'
                        review_feedback_path = plc_dir / 'runtime-feedback.json'
                        review_acceptance_path = plc_dir / 'acceptance.json'
                        self.observe_verification('runtime_acceptance', acceptance['passed'] and feedback['verdict'] == 'pass',
                            [plc_dir / 'acceptance.json', plc_dir / 'runtime-feedback.json', plc_dir / 'runtime/evidence.json'])
                        if acceptance["passed"] and feedback["verdict"] == "pass":
                            self.verification.require_complete()
                            if not self.config['factoryio'].get('keep_running'):
                                return self.transition("passed", acceptance=acceptance)
                            from .live_handoff import start_live_handoff
                            from .plc_observation import observe_current_plc
                            self.state.update(phase='live_handoff', active_role='tools')
                            self.trace('tool_requested', tool='factoryio_web_api', action='restart_and_keep_running')
                            lifecycle = None
                            if self.config['factoryio'].get('synchronized_lifecycle'):
                                from .synchronized_runtime import SynchronizedRuntime
                                lifecycle = SynchronizedRuntime(self.config['factoryio'], codesys, plc_dir,
                                    plc_dir/'live-handoff-lifecycle')
                            handoff, handoff_evidence = start_live_handoff(self.config['factoryio'], task,
                                manifest, contract, versions, plc_dir / 'live-handoff',
                                failure_observer=lambda: observe_current_plc(codesys, plc_dir, program),
                                lifecycle=lifecycle, reset_mode=self.config['factoryio'].get('sync_reset_mode','warm'))
                            self.trace('tool_completed', tool='factoryio_web_api', action='restart_and_keep_running',
                                       passed=handoff['passed'], left_running=handoff['left_running'])
                            if handoff['passed'] and handoff['left_running']:
                                return self.transition('passed', acceptance=acceptance, live_handoff=handoff)
                            # Diagnose and repair a real restart failure using the normal agents.
                            # Preserve full acceptance, plus the distinct scoped restart receipt.
                            evidence = handoff_evidence
                            acceptance = handoff['acceptance']
                            evidence['records'].append({'id':'live-handoff-acceptance', 'kind':'acceptance_result',
                                'timestamp':utc_now(), 'result':acceptance,
                                'scope':'restart startup and ongoing operation; full Stop/Reset tests already ran',
                                'full_acceptance_path':str((plc_dir/'acceptance.json').resolve()),
                                'source_record_ids':list(dict.fromkeys(ref for rule in acceptance.get('rules',[])
                                    for ref in rule.get('source_record_ids',[])))})
                            write_json(plc_dir/'live-handoff/evidence.json', evidence)
                            feedback = self.observer.review(task, manifest, contract, evidence, versions, 'runtime', program,
                                self.handoff('vision', 'restart_review', [plc_dir/'program.json',
                                    plc_dir/'live-handoff/evidence.json', plc_dir/'live-handoff/receipt.json'],
                                    'restart-feedback.json'))
                            write_json(plc_dir/'restart-feedback.json', feedback)
                            review_evidence_path = plc_dir / 'live-handoff/evidence.json'
                            review_feedback_path = plc_dir / 'restart-feedback.json'
                            review_acceptance_path = plc_dir / 'live-handoff/acceptance.json'
                            self.observe_verification('runtime_acceptance', False,
                                [plc_dir/'live-handoff/receipt.json', plc_dir/'restart-feedback.json'])
                        targets = {f['target'] for f in feedback['findings']}
                        if self.config.get('supervisor',{}).get('enabled') and not acceptance['passed'] and \
                                feedback['verdict'] in ('repair','inconclusive') and targets and targets <= {'plc','investigate'}:
                            from .supervisor import SupervisorAgent
                            investigation = SupervisorAgent(self.config,self.output,budget=self.budget,progress=self.progress).investigate(task,manifest,contract,
                                evidence,versions,program,feedback,self.handoff('supervisor','runtime_investigation',
                                [self.output/'task.json',plc_dir/'program.json',review_evidence_path,
                                 review_acceptance_path,review_feedback_path],'runtime-investigation.json'))
                            write_json(plc_dir/'runtime-investigation.json',investigation)
                            self.trace('runtime_investigated',verdict=investigation['verdict'])
                            feedback = investigation
                        targets = {f["target"] for f in feedback["findings"]}
                        if feedback["verdict"] == "repair" and targets == {"scene"}:
                            scene_feedback = feedback
                            self.record_repair("scene", "runtime_review", feedback, plan)
                            # Geometry repair invalidates PLC/evidence, even if I/O names remain the same.
                            previous_program, plc_feedback = None, None
                            break
                        if feedback["verdict"] == "repair" and targets == {"plc"}:
                            plc_feedback = feedback
                            self.record_repair("plc", "runtime_review", feedback, program)
                            continue
                        return self.transition("needs_attention", feedback=feedback, acceptance=acceptance)
                    else:
                        return self.transition("repair_limit_reached", phase="plc")
                return self.transition("repair_limit_reached", phase="scene")
            except ExecutionLimitError as exc:
                return self.transition('needs_attention',target='tools',failure_kind='execution_limit',error=str(exc))
            except ModelOutputError as exc:
                return self.transition('needs_configuration',target='tools',failure_kind='model_output',
                                       failed_role=self.state.get('active_role'),error=str(exc))
            except ModelServiceError as exc:
                return self.transition("needs_attention", target="tools", failure_kind="model_service",
                                       failed_role=self.state.get("active_role"), error=str(exc))
            except BindingConfigurationError as exc:
                return self.transition('needs_configuration', target='tools', failure_kind='tool_configuration', error=str(exc))
            except KeyboardInterrupt:
                return self.transition("cancelled", reason="keyboard_interrupt")
            except (ContractError, ValueError, OSError) as exc:
                if any(s in str(exc) for s in ("diagnosis", "hypothesis", "another handoff", "handoff input", "feedback")):
                    return self.transition("needs_attention", target="investigate", phase="review_contract", error=str(exc))
                return self.transition("needs_configuration" if any(s in str(exc) for s in
                    ("configured", "requires current image", "template", "not completed")) else "failed", error=str(exc))


def verify_artifact_versions(directory: Path) -> tuple[dict, dict, dict]:
    manifest = read_json(directory / "manifest.json")
    contract = read_json(directory / "io-contract.json")
    versions = read_json(directory / "versions.json")
    require(file_hash(directory / "scene.factoryio") == versions["scene"] == manifest["scene_sha256"],
            "scene file was modified after generation")
    require(canonical_hash(contract) == versions["io"], "I/O contract was modified after generation")
    return manifest, contract, versions
