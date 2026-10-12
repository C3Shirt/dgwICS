"""Shared per-attempt admission limits, persisted independently of training candidates."""
from datetime import datetime, timezone
from pathlib import Path
import time
import uuid

from .contracts import ContractError, canonical_hash, read_json, require, write_json


class ExecutionLimitError(ContractError):
    """Stop with checkpoints intact; retries cannot consume another identical request."""


DEFAULT_LIMITS = {'max_calls': 24, 'max_total_tokens': 600000,
                  'max_elapsed_seconds': 1800, 'max_payload_chars': 200000,
                  'max_repeated_failures': 2}


def execution_limits(config):
    values = config.get('execution_limits', {})
    require(isinstance(values, dict) and not set(values) - set(DEFAULT_LIMITS), 'unknown execution limit')
    result = {**DEFAULT_LIMITS, **values}
    for key, value in result.items():
        require(type(value) is int and value > 0, key + ' must be a positive integer')
    return result


def _failure_signature(role, payload):
    feedback = payload.get('format_feedback') or payload.get('stage_rejection') or payload.get('feedback') or {}
    error = feedback.get('error') or (feedback.get('diagnostics') or {}).get('error')
    if not error: return None
    previous = feedback.get('previous_response')
    if previous is None:
        previous = next((payload[k] for k in ('previous_task','previous_program','previous_plan')
                         if payload.get(k) is not None), feedback.get('rejected_reply'))
    def semantic(value):
        if isinstance(value, dict): return {k: semantic(v) for k,v in value.items() if k != 'handoff_id'}
        if isinstance(value, list): return [semantic(v) for v in value]
        return value
    return canonical_hash({'role': role, 'phase': (payload.get('handoff') or {}).get('phase'),
        'stage': payload.get('planning_stage', payload.get('scene_stage', payload.get('plc_stage'))),
        'error': error, 'rejected_output': semantic(previous)})


class ExecutionBudget:
    def __init__(self, output, config):
        self.path = Path(output) / 'execution-budget.json'
        self.limits = execution_limits(config)
        if self.path.is_file():
            self.state = read_json(self.path)
            require(self.state['limits'] == self.limits, 'execution limits changed within an attempt')
        else:
            self.state = {'schema_version': 1, 'started_at': time.time(), 'limits': self.limits,
                'calls': [], 'observed_total_tokens': 0, 'usage_missing_calls': 0,
                'failure_signatures': {}, 'stopped_reason': None,
                'token_limit_semantics': 'checked before each call against reported usage; one call can exceed it; missing usage is unknown'}
            self.save()

    def save(self):
        write_json(self.path, self.state)

    def stop(self, reason):
        self.state['stopped_reason'] = reason; self.save()
        raise ExecutionLimitError(reason)

    def check(self):
        if self.state['stopped_reason']: raise ExecutionLimitError(self.state['stopped_reason'])
        if time.time() - self.state['started_at'] - self.state.get('human_wait_seconds',0) >= self.limits['max_elapsed_seconds']:
            self.stop('attempt elapsed-time budget exhausted; preserve checkpoints and inspect recovery')

    def exclude_human_wait(self, seconds):
        require(isinstance(seconds,(int,float)) and seconds>=0,'invalid human waiting duration')
        self.state['human_wait_seconds']=self.state.get('human_wait_seconds',0)+seconds
        self.save()

    def admit(self, role, system, payload):
        from .training import json_text
        self.check()
        if len(self.state['calls']) >= self.limits['max_calls']: self.stop('attempt model-call budget exhausted')
        if self.state['observed_total_tokens'] >= self.limits['max_total_tokens']:
            self.stop('attempt reported-token budget exhausted')
        chars = len(system) + len(json_text(payload))
        if chars > self.limits['max_payload_chars']: self.stop('model context exceeds payload-character budget; reduce role context')
        signature = _failure_signature(role, payload)
        if signature:
            count = self.state['failure_signatures'].get(signature, 0) + 1
            self.state['failure_signatures'][signature] = count
            if count >= self.limits['max_repeated_failures']:
                from .progress import safe_text
                feedback = payload.get('format_feedback') or payload.get('stage_rejection') or payload.get('feedback') or {}
                error = feedback.get('error') or (feedback.get('diagnostics') or {}).get('error')
                stage = payload.get('planning_stage', payload.get('scene_stage', payload.get('plc_stage'))) or role
                self.stop('identical contract failure and rejected output repeated; inspect tools/context instead of retrying; '
                          'subtask=' + safe_text(stage,120) + '; last contract error: ' + safe_text(error,900))
        row = {'id': uuid.uuid4().hex, 'role': role, 'status': 'admitted',
            'started_at': datetime.now(timezone.utc).isoformat(), 'payload_chars': chars,
            'images_accounted_in_chars': False, 'failure_signature': signature}
        self.state['calls'].append(row); self.save()
        return row['id']

    def finish(self, call_id, metadata, error=None):
        row = next(r for r in self.state['calls'] if r['id'] == call_id)
        usage = metadata.get('usage') if isinstance(metadata, dict) else None
        tokens = usage.get('total_tokens') if isinstance(usage, dict) else None
        if type(tokens) is not int or tokens < 0:
            tokens = None; self.state['usage_missing_calls'] += 1
        else: self.state['observed_total_tokens'] += tokens
        row.update(status='failed' if error else 'completed', total_tokens=tokens,
                   error_type=type(error).__name__ if error else None)
        self.save()


class BudgetedClient:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget
        recorder = getattr(client, 'recorder', None)
        if recorder is not None: self.recorder = recorder

    def complete(self, role, system, payload, images=None):
        call_id = self.budget.admit(role, system, payload)
        error = None
        try:
            return self.client.complete(role, system, payload, images) if images else self.client.complete(role, system, payload)
        except BaseException as exc:
            error = exc; raise
        finally:
            from .training import RecordingClient
            raw = self.client.client if isinstance(self.client, RecordingClient) else self.client
            method = getattr(type(raw), 'exchange_metadata', None)
            metadata = method(raw) if callable(method) else {}
            self.budget.finish(call_id, metadata, error)
