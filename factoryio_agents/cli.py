"""Single prompt command-line execution boundary for the external-model agent framework."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import traceback
import uuid

from .contracts import ContractError, file_hash, require, write_json
from .diagnostics import summarize_run
from .llm import ModelClient, ModelOutputError
from .runtime import FactoryIOClient
from .training import Redactor, configured_secret_values
from .workflow import Workflow, load_config
from .memory import ExperienceMemory, inspect_resume, latest_runtime_review
from .supervisor import SupervisorAgent, progress_view
from .progress import ConsoleProgress

ROOT = Path(__file__).resolve().parents[1]


def protected_credentials(config):
    codesys = config.get('codesys', {})
    store = codesys.get('protected_credentials_file')
    if not store: return
    names = codesys.get('online_credentials', {})
    if all(os.getenv(names.get(k, '')) for k in ('username_env', 'password_env')): return
    path = Path(store).resolve()
    require(path.is_file(), 'protected PLC credentials file is missing')
    env = dict(os.environ); env['FACTORYIO_PROTECTED_CREDENTIAL_FILE'] = str(path)
    script = """Add-Type -AssemblyName System.Security
    $blob=[IO.File]::ReadAllBytes($env:FACTORYIO_PROTECTED_CREDENTIAL_FILE)
    $plain=[Security.Cryptography.ProtectedData]::Unprotect($blob,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    [Console]::Out.Write([Text.Encoding]::UTF8.GetString($plain))"""
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        env=env, capture_output=True, timeout=10)
    require(result.returncode == 0, 'PLC credentials cannot be decrypted by the current Windows account')
    try: value = json.loads(result.stdout.decode('utf-8'))
    except (ValueError, UnicodeError): raise ContractError('protected PLC credential format invalid') from None
    for field, key in [('username_env', 'username'), ('password_env', 'password')]:
        require(isinstance(names.get(field), str) and isinstance(value.get(key), str), 'protected credential fields missing')
    for field, key in [('username_env', 'username'), ('password_env', 'password')]:
        if not os.getenv(names[field]): os.environ[names[field]] = value[key]


def read_configuration(path):
    config = load_config(path)
    for key in ('binding_seed_config', 'binding_seed_io', 'protected_credentials_file'):
        if config.get('codesys', {}).get(key):
            config['codesys'][key] = str((path.parent / config['codesys'][key]).resolve())
    config.setdefault('training', {})['enabled'] = True
    return config


def preflight(config):
    client = ModelClient(config['llm'])
    roles = {role: {'provider': config['llm'].get('role_providers', {}).get(role),
                    'model': client.providers.get(config['llm'].get('role_providers', {}).get(role), {}).get('model_name')
                             or config['llm'].get('models', {}).get(role)}
             for role in ('planner', 'scene', 'vision', 'plc')}
    issues = []
    for role, data in roles.items():
        if not data['model']: issues.append(role + ' model is not configured')
    software = {}
    try:
        tags = FactoryIOClient(config['factoryio']).tags()
        software['factoryio'] = {'web_api_responding': True, 'tag_count': len(tags)}
    except (ValueError, OSError) as error:
        software['factoryio'] = {'web_api_responding': False}
        issues.append(str(error))
    try:
        with socket.create_connection((config['protocol']['host'], config['protocol']['port']), timeout=2):
            software['plc_modbus_port_open'] = True
    except OSError:
        software['plc_modbus_port_open'] = False
        issues.append('PLC Modbus endpoint is unavailable')
    commands = config['factoryio']
    for key in ('load_command', 'capture_command', 'connect_command'):
        if not commands.get(key): issues.append(key + ' is not configured')
    command = commands.get('load_command', [])
    if '--ipc' in command:
        ipc = Path(command[command.index('--ipc') + 1])
        sys.path.insert(0, str(ROOT / 'tools/factoryio_bridge'))
        try:
            from client import call
            data = call(ipc, 'status', timeout=3)['data']
            software['bridge'] = {'responding': True, 'app_version': data['app_version'],
                                  'native_save_supported': 'scene_changed' in data}
            if 'scene_changed' not in data: issues.append('loaded bridge has no native save support')
        except (OSError, RuntimeError, TimeoutError) as error:
            software['bridge'] = {'responding': False}
            issues.append(str(error))
    credentials = config['codesys'].get('online_credentials', {})
    software['plc_credentials_available'] = all(os.getenv(credentials.get(k, '')) for k in ('username_env', 'password_env'))
    if config['codesys'].get('deploy') and not software['plc_credentials_available']:
        issues.append('PLC device credentials are not configured')
    return {'models': roles, 'software': software, 'issues': issues,
            'model_calls_issued': False, 'software_modified': False}


def source_snapshot(output):
    sources = list((ROOT / 'factoryio_agents').rglob('*.py'))
    sources += list((ROOT / 'factoryio_agents/prompts').glob('*.md'))
    sources += list((ROOT / 'factoryio_agents/knowledge').glob('*.json'))
    sources += [ROOT / 'run_agents.py', ROOT / 'tools/factoryio_bridge/AgentBridge.cs',
                ROOT / 'tools/factoryio_bridge/client.py', ROOT / 'tools/factoryio_bridge/workflow_hook.py']
    hashes = {}
    for source in sources:
        relative = source.relative_to(ROOT)
        destination = output / 'framework-source' / relative
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, destination)
        hashes[relative.as_posix()] = file_hash(destination)
    return hashes


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='用一个场景描述运行四角色 agent 框架；失败也保留完整记录。')
    parser.add_argument('prompt', nargs='?', help='场景描述，放在引号中')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.agent.json')
    parser.add_argument('--out', type=Path, help='全新的输出目录，省略时自动创建')
    parser.add_argument('--mode', choices=('run', 'prepare'), default='run')
    parser.add_argument('--check', action='store_true', help='只检查配置与本地接口，不调用模型或改变软件')
    parser.add_argument('--scenario-id'); parser.add_argument('--lineage-group')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--show-models', type=Path, help='只读回放指定运行本轮的模型业务输出，不调用模型或软件')
    parser.add_argument('--resume', type=Path, help='从已有运行恢复，保留原始目录并创建新的恢复目录')
    parser.add_argument('--resume-inspect', action='store_true', help='只检查恢复计划，不调用模型或软件写接口')
    parser.add_argument('--revalidate', action='store_true', help='配合 --resume 复用设计并重新执行全部联机验收，允许来源已通过')
    parser.add_argument('--keep-running', action='store_true', help='完整验收后重新启动并实测流程；通过后让场景保持运行')
    parser.add_argument('--restart-from', choices=('auto','planning','scene','plc'), default='auto')
    parser.add_argument('--supervise', type=Path, help='让全局监督模型分析指定运行；不执行软件操作')
    parser.add_argument('--investigate', type=Path, help='用监督模型调查指定运行的最后一次实测失败；不操作软件')
    parser.add_argument('--control', type=Path, help='使用指定运行的已部署工程执行两端生命周期操作，不重新生成场景或 PLC')
    parser.add_argument('--control-action', choices=('stop','reset','restart'), default='reset')
    parser.add_argument('--reset-mode', choices=('warm','cold'), default='warm', help='两端统一复位采用热复位或冷复位，默认热复位')
    args = parser.parse_args(argv)
    if args.show_models:
        if args.prompt or args.out or args.check or args.resume or args.control or args.supervise or args.investigate or args.keep_running:
            parser.error('--show-models 是独立的只读回放命令')
        from .progress import replay_models
        try:
            replay_models(args.show_models,ConsoleProgress(None,quiet=args.quiet))
        except (ValueError,OSError,KeyError) as error:
            print('无法回放：'+str(error),file=sys.stderr); return 2
        return 0
    if not args.check and not args.resume and not args.supervise and not args.investigate and not args.control and not (args.prompt and args.prompt.strip()): parser.error('需要一个非空场景描述')
    if args.control and (args.resume or args.check or args.supervise or args.investigate or args.mode!='run' or args.keep_running or args.prompt):
        parser.error('--control 是独立的两端操作；不与生成或验收模式组合')
    if args.resume_inspect and not args.resume: parser.error('--resume-inspect 需要 --resume')
    if args.revalidate and not args.resume: parser.error('--revalidate 需要 --resume')
    if args.keep_running and (args.mode != 'run' or args.check or args.resume_inspect or args.supervise or args.investigate):
        parser.error('--keep-running 仅用于完整联机运行')
    if args.resume and (args.check or args.supervise or args.investigate or args.mode != 'run'): parser.error('--resume 仅用于完整运行或恢复检查')
    if args.investigate and (args.check or args.supervise or args.mode!='run'): parser.error('--investigate 仅用于独立调查')
    output = (args.out or ROOT / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])).resolve()
    if output.exists(): parser.error('输出目录已存在，请使用新的目录')
    output.mkdir(parents=True)
    redactor = Redactor()
    original_credentials = {}
    workflow = None
    try:
        config = read_configuration(args.config.resolve())
        config.setdefault('factoryio', {})['keep_running'] = args.keep_running
        config['factoryio']['sync_reset_mode'] = args.reset_mode
        resume_plan = inspect_resume(args.resume, config, args.restart_from, revalidate=args.revalidate) if args.resume else None
        if resume_plan:
            require(not args.prompt or args.prompt == resume_plan['requirement'],
                    '恢复必须保留原需求；更改需求应创建新运行')
            args.prompt = resume_plan['requirement']
            from .contracts import read_json
            recorded_training = read_json(args.resume / 'config.snapshot.json').get('training', {})
            for key in ('scenario_id','lineage_group'):
                if recorded_training.get(key): config['training'][key] = recorded_training[key]
        if args.resume_inspect:
            write_json(output / 'resume-plan.json', resume_plan)
            print(json.dumps({'output':str(output), **resume_plan, 'model_calls_issued':False,
                              'software_modified':False}, ensure_ascii=False, indent=2))
            return 0
        original_credentials = {name: os.environ.get(name) for name in
            config.get('codesys', {}).get('online_credentials', {}).values() if isinstance(name, str) and name}
        if not args.supervise and not args.investigate: protected_credentials(config)
        if args.control:
            from .contracts import read_json, canonical_hash
            from .workflow import verify_artifact_versions
            from .synchronized_runtime import SynchronizedRuntime
            from .live_handoff import start_live_handoff
            source = args.control.resolve()
            state = read_json(source/'run.json')
            audit = summarize_run(source, state)
            require(not audit['manual_override_detected'], 'control source contains unverified model artifacts')
            plc = Path(state['plc_dir']).resolve()
            require(plc.is_relative_to(source), 'control PLC revision is outside its source run')
            manifest, contract, _ = verify_artifact_versions(plc.parent)
            task, program, versions = read_json(source/'task.json'), read_json(plc/'program.json'), read_json(plc/'versions.json')
            require(canonical_hash(task) == versions['task'] and canonical_hash(program) == versions['plc'], 'control source versions changed')
            write_json(output/'execution.json', {'entrypoint':'run_agents.py', 'mode':'synchronized_control',
                'source_run':str(source), 'source_versions':versions, 'action':args.control_action,
                'reset_mode':args.reset_mode, 'model_calls_issued':False, 'model_artifacts_modified':False})
            lifecycle = SynchronizedRuntime(config['factoryio'], config['codesys'], plc, output/'lifecycle')
            if args.control_action == 'restart':
                result, _ = start_live_handoff(config['factoryio'], task, manifest, contract, versions,
                    output/'operation-evidence', lifecycle=lifecycle, reset_mode=args.reset_mode)
                passed = result['passed'] and result['left_running']
            else:
                result = lifecycle.stop_both() if args.control_action == 'stop' else lifecycle.reset_both(args.reset_mode)
                passed = result['operation_passed']
            write_json(output/'control-result.json', result)
            print(json.dumps({'operation_passed':passed, 'action':args.control_action, 'reset_mode':args.reset_mode,
                'left_running':result.get('left_running',False), 'model_calls_issued':False,
                'full_generation_pipeline_revalidated':False, 'output':str(output)},ensure_ascii=False,indent=2))
            return 0 if passed else 2
        config['training'].update({k: v for k,v in [('scenario_id', args.scenario_id), ('lineage_group', args.lineage_group)] if v})
        client = ModelClient(config['llm'])
        redactor = Redactor(configured_secret_values(config) + client.secret_values())
        progress = ConsoleProgress(output, quiet=args.quiet, secrets=redactor.values)
        write_json(output / 'config.snapshot.json', redactor.clean(config))
        hashes = source_snapshot(output)
        write_json(output / 'execution.json', {'schema_version': 1, 'entrypoint': 'run_agents.py', 'mode': args.mode,
            'revalidation_requested': args.revalidate,
            'keep_running_requested': args.keep_running,
            'check_only': args.check, 'prompt': redactor.clean(args.prompt), 'config_path': str(args.config.resolve()),
            'source_sha256': hashes, 'model_injection_allowed': False, 'manual_artifact_fallback_allowed': False})
        if args.investigate:
            from .contracts import read_json
            from .coordination import make_handoff
            from .workflow import verify_artifact_versions
            source=args.investigate.resolve(); state=read_json(source/'run.json')
            report=summarize_run(source,state)
            require(not report['manual_override_detected'],'investigation source has unverified model artifacts')
            measured=sorted(p for p in source.glob('scene-*/plc-*') if
                            latest_runtime_review(p)['evidence'].is_file())
            require(bool(measured),'no measured runtime evidence is available for investigation')
            plc=measured[-1]; selected=latest_runtime_review(plc)
            require(selected['feedback'].is_file(),'latest runtime capture has no completed review; old feedback cannot replace it')
            manifest,contract,_=verify_artifact_versions(plc.parent)
            task=read_json(source/'task.json'); program=read_json(plc/'program.json'); versions=read_json(plc/'versions.json')
            evidence=read_json(selected['evidence']); review=read_json(selected['feedback'])
            inputs=[source/'task.json',plc.parent/'manifest.json',plc.parent/'io-contract.json',
                    plc/'program.json',plc/'versions.json',selected['evidence'],selected['feedback']]
            handoff=make_handoff('orchestrator','supervisor','runtime_investigation',versions,inputs,
                                 'runtime-investigation.json',[])
            write_json(output/'investigation-source.json',{'source_run':str(source),'measured_revision':str(plc),
                'runtime_stage':selected['stage'], 'archived_evidence':True,'software_modified':False,'handoff':handoff})
            decision=SupervisorAgent(config,output,progress=progress).investigate(task,manifest,contract,evidence,versions,program,review,handoff)
            write_json(output/'runtime-investigation.json',redactor.clean(decision))
            print(json.dumps({'output':str(output),'verdict':decision['verdict'],'software_modified':False,
                'live_pipeline_passed':False,'measured_revision':str(plc)},ensure_ascii=False,indent=2))
            return 0
        if args.supervise:
            from .contracts import read_json
            source = args.supervise.resolve()
            old_state = read_json(source / 'run.json'); old_report = summarize_run(source, old_state)
            plan = inspect_resume(source, config) if not old_report['live_pipeline_passed'] else None
            view = progress_view(old_state, old_report, plan)
            write_json(output / 'supervisor-input.json', redactor.clean(view))
            decision = SupervisorAgent(config, output, progress=progress).decide(view)
            write_json(output / 'supervisor-decision.json', redactor.clean(decision))
            print(json.dumps({'output':str(output), 'decision':decision, 'software_modified':False}, ensure_ascii=False, indent=2))
            return 0
        if args.check:
            result = redactor.clean(preflight(config)); write_json(output / 'preflight.json', result)
            print(json.dumps({'output': str(output), **result}, ensure_ascii=False, indent=2))
            return 0 if not result['issues'] else 2
        workflow = Workflow(config, output, progress=progress)
        experience = ExperienceMemory(ROOT / 'agent_memory/experiences', redactor)
        experience.import_related_runs(ROOT / 'runs', args.prompt)
        observations = experience.retrieve(args.prompt)
        if resume_plan:
            observations.append({'kind':'observed_failure' if resume_plan['source_failure'] else 'design_checkpoint',
                                 'run':resume_plan['source_run'],'purpose':resume_plan['purpose'],
                                 'failure':resume_plan['source_failure']})
        workflow.memory_context = {'observations': observations[-6:]}
        write_json(output / 'memory-context.json', redactor.clean(workflow.memory_context))
        state = workflow.resume(resume_plan) if resume_plan else getattr(workflow, args.mode)(args.prompt)
        report = summarize_run(output, state)
        changed = [name for name, sha in hashes.items() if not (ROOT / name).is_file() or file_hash(ROOT / name) != sha]
        report['framework_changed_during_run'] = changed
        if changed:
            report['live_pipeline_passed'] = False
            report['failure'] = {'category': 'framework_changed_during_run', 'evidence_status': 'observed',
                'phase': state.get('phase'), 'active_role': 'tools', 'target': 'investigate',
                'observed_error': 'Framework sources changed during execution', 'root_cause_confirmed': False}
        write_json(output / 'failure-analysis.json', redactor.clean(report))
        write_json(output / 'result.json', redactor.clean(report))
        experience.remember(output, args.prompt, report, config['training'].get('scenario_id'))
        try:
            recovery = inspect_resume(output, config) if not report['live_pipeline_passed'] else None
            view = progress_view(state, report, recovery)
            write_json(output / 'supervisor-state.json', redactor.clean(view))
            if config.get('supervisor', {}).get('enabled', False):
                write_json(output / 'supervisor-input.json', redactor.clean(view))
                decision = SupervisorAgent(config, output, progress=progress).decide(view)
                write_json(output / 'supervisor-decision.json', redactor.clean(decision))
        except (ValueError, OSError, KeyError) as exc:
            # An unavailable supervisor cannot erase the task result or grant success.
            write_json(output / 'supervisor-error.json', redactor.clean({'error':str(exc), 'task_result_preserved':True}))
        index = ROOT / 'training_data/run-index.jsonl'
        index.parent.mkdir(parents=True, exist_ok=True)
        with index.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'output': str(output), 'status': state['status'],
                'scenario_id': config['training'].get('scenario_id'), 'lineage_group': config['training'].get('lineage_group'),
                'review_status': 'pending'}, ensure_ascii=False) + '\n')
        failure = report['failure']
        brief = {key: failure.get(key) for key in ('category', 'phase', 'active_role', 'target', 'observed_error')} if failure else None
        budget = workflow.budget.state
        attempt_roles = {}
        for call in budget['calls']:
            role = call['role']
            attempt_roles[role] = attempt_roles.get(role, 0) + 1
        print(json.dumps(redactor.clean({'status': state['status'], 'live_pipeline_passed': report['live_pipeline_passed'],
            'model_calls_this_attempt': attempt_roles, 'cumulative_lineage_roles': report['roles'],
            'failure': brief, 'output': str(output)}), ensure_ascii=False, indent=2))
        return 0 if report['live_pipeline_passed'] or (args.mode == 'prepare' and state['status'] == 'awaiting_scene_review') else 2
    except (Exception, KeyboardInterrupt) as error:
        cancelled = isinstance(error, KeyboardInterrupt)
        state = dict(workflow.state) if workflow is not None and isinstance(workflow.state, dict) else {}
        state.update(status='cancelled' if cancelled else 'failed', error=str(error), error_type=type(error).__name__)
        if cancelled: state['reason'] = 'keyboard_interrupt'
        if not workflow: state.update(phase='cli_or_environment', active_role='tools')
        write_json(output / 'run.json', redactor.clean(state))
        try:
            result = summarize_run(output, state)
        except (ValueError, OSError, KeyError, TypeError):
            result = {'workflow_status': state['status'], 'live_pipeline_passed': False}
        category = ('cancelled' if cancelled else 'model_output_budget' if isinstance(error,ModelOutputError) else 'cli_or_environment' if isinstance(error,
            (ValueError, OSError, subprocess.TimeoutExpired)) else 'framework_error')
        result['failure'] = {'category': category, 'evidence_status': 'observed',
            'phase': state.get('phase'), 'active_role': state.get('active_role'), 'target': 'investigate',
            'error_type': type(error).__name__, 'observed_error': str(error) or None,
            'root_cause_confirmed': False, 'exception_traceback': traceback.format_exc()}
        result = redactor.clean(result)
        write_json(output / 'failure-analysis.json', result); write_json(output / 'result.json', result)
        if args.prompt:
            ExperienceMemory(ROOT / 'agent_memory/experiences', redactor).remember(output, args.prompt, result, args.scenario_id)
        write_json(output / 'supervisor-state.json', redactor.clean(progress_view(state, result)))
        print(json.dumps({'status': state['status'], 'failure': {key: result['failure'][key] for key in
            ('category', 'phase', 'active_role', 'error_type', 'observed_error')},
            'live_pipeline_passed': False, 'output': str(output)}, ensure_ascii=False, indent=2))
        return 2
    finally:
        for name, previous in original_credentials.items():
            if previous is None: os.environ.pop(name, None)
            else: os.environ[name] = previous


if __name__ == '__main__': sys.exit(main())
