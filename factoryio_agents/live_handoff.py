"""Restart a validated scene and prove it still operates before leaving it running.

This reuses the planner's native startup actions and observable process checks.
Stop/reset tests remain in the preceding full acceptance; this separate receipt
never substitutes for that acceptance or changes the model-authored task.
"""
from copy import deepcopy
from pathlib import Path

from .contracts import canonical_hash, require, write_json
from .runtime import FactoryIOClient, collect_evidence, utc_now
from .validation import validate_events


def operating_projection(task):
    projection = deepcopy(task)
    omitted = []
    for rule in projection['acceptance']:
        require(rule['kind'] == 'signal_contract',
                'keep-running needs model-authored native startup and signal checks')
        probes = rule.get('probes', [])
        phases = {p['id'] + '.' + a['label'] for p in probes if p['stage'] == 'after_capture'
                  for a in p['actions']}
        rule['probes'] = [p for p in probes if p['stage'] == 'startup']
        omitted.extend(c['id'] for c in rule['checks'] if c.get('phase') in phases)
        rule['checks'] = [c for c in rule['checks'] if c.get('phase') not in phases]
        require(any(c['kind'] in ('sequence', 'duration', 'alternation') for c in rule['checks']),
                'keep-running requires measured process progress, not just Running or lamps')
    require(bool(projection['acceptance']), 'keep-running needs a nonempty measurement contract')
    return projection, omitted


def start_live_handoff(config, task, manifest, io_contract, versions, output: Path, failure_observer=None,
                       lifecycle=None, reset_mode='warm'):
    """On any incomplete restart, stop the simulation and retain failure evidence."""
    output.mkdir(parents=True, exist_ok=True)
    projection, omitted = operating_projection(task)
    write_json(output / 'measurement-scope.json', {
        'schema_version': 1, 'source_task_sha256': canonical_hash(task),
        'purpose': 'restart and ongoing operation after full acceptance',
        'omitted_after_capture_checks': omitted,
        'acceptance': projection['acceptance'], 'full_acceptance_replaced': False})
    runtime = FactoryIOClient(config)
    evidence = None
    receipt = {'schema_version': 1, 'versions': versions, 'started_at': utc_now(),
               'purpose': 'live_handoff', 'passed': False, 'left_running': False}
    try:
        if lifecycle is not None:
            receipt['synchronized_reset'] = lifecycle.reset_both(reset_mode)
            receipt['synchronized_start'] = lifecycle.start_both()
        else:
            runtime.control('run', False)
            runtime.wait_running(False)
            runtime.control('reset', True)
            runtime.control('reset', False)
            runtime.control('pause', False)
            runtime.control('run', True)
        receipt['running_before_capture'] = runtime.wait_running(True)
        # One useful view also leaves the user's camera aimed at the process.
        capture_config = {**config, 'capture_views': ['iso_a'],
                          'execute_model_probes': True, 'operator_startup': False}
        evidence = collect_evidence(capture_config, output, versions, io_contract,
                                    manifest=manifest, task=projection)
        require(evidence['reconciliation']['passed'], 'scene changed during live handoff')
        acceptance = validate_events(projection, evidence, versions, io_contract)
        write_json(output / 'acceptance.json', acceptance)
        receipt['acceptance'] = acceptance
        receipt['passed'] = acceptance['passed']
        if not acceptance['passed'] and failure_observer is not None:
            observation = failure_observer()
            evidence['records'].append({'id':'restart-plc-state', 'kind':'plc_diagnostics',
                'timestamp':utc_now(), 'result':observation})
            write_json(output/'evidence.json', evidence)
            receipt['plc_state_observation'] = observation
        if acceptance['passed']:
            receipt['running_at_handoff'] = runtime.wait_running(True)
            paused = [t for t in runtime.tags() if t['name'] == 'FACTORY I/O (Paused)'
                      and t['kind'].lower() == 'input']
            require(len(paused) == 1 and paused[0]['value'] is False
                    and paused[0].get('isForced') is False, 'handoff is paused or status unverified')
            receipt['left_running'] = True
    except BaseException as exc:
        receipt.update(passed=False, error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        if not receipt['left_running']:
            runtime.control('run', False)
            receipt['stopped_after_failure'] = runtime.wait_running(False)
            if lifecycle is not None: receipt['synchronized_stop'] = lifecycle.stop_both()
        receipt['finished_at'] = utc_now()
        write_json(output / 'receipt.json', receipt)
    return receipt, evidence
