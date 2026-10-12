"""Phase-owned verification work; pending work is not an unmet user requirement."""
from .contracts import file_hash, require, write_json


VERIFIERS = {
    'process_scene': {'stage': 'scene', 'owner': 'scene', 'scope': 'required objects and PLC single-item emitter configuration'},
    'scene_visual': {'stage': 'scene_review', 'owner': 'vision', 'scope': 'current loaded scene visual review'},
    'plc_compile': {'stage': 'plc', 'owner': 'tools', 'scope': 'actual compile, device mappings and cyclic task'},
    'modbus_connection': {'stage': 'connection', 'owner': 'tools', 'scope': 'active driver and configured endpoint receipt'},
    'runtime_acceptance': {'stage': 'runtime', 'owner': 'tools', 'scope': 'declared measured process acceptance and runtime visual review'},
}


def validate_verification_tasks(task):
    tasks = task.get('verification_tasks', [])
    require(isinstance(tasks, list), 'verification_tasks must be an array')
    ids = set()
    for row in tasks:
        require(isinstance(row, dict), 'verification task must be object')
        require(isinstance(row.get('id'), str) and bool(row['id']) and row['id'] not in ids, 'verification task id missing or duplicated')
        require(isinstance(row.get('description'), str) and bool(row['description'].strip()), 'verification description missing')
        require(isinstance(row.get('verifier'), str), 'verification task verifier must be string')
        check = VERIFIERS.get(row['verifier'])
        require(check is not None, 'verification task names an unsupported verifier')
        require(row.get('stage') == check['stage'] and row.get('owner') == check['owner'], 'verification task stage/owner differs from verifier')
        require(not set(row) - {'id', 'description', 'verifier', 'stage', 'owner'}, 'verification task contains unsupported fields')
        ids.add(row['id'])


class VerificationLedger:
    def __init__(self, task, output):
        validate_verification_tasks(task)
        self.path = output / 'verification.json'
        self.task_id = task['task_id']
        self.rows = [{**row, 'status': 'pending', 'evidence': []} for row in task.get('verification_tasks', [])]
        self.save()

    def save(self):
        write_json(self.path, {'schema_version': 1, 'task_id': self.task_id, 'checks': self.rows,
                              'all_completed': all(row['status'] == 'passed' for row in self.rows)})

    def invalidate(self, verifiers, versions):
        for row in self.rows:
            if row['verifier'] in verifiers:
                row.update(status='pending', evidence=[], versions=versions)
        self.save()

    def observe(self, verifier, passed, versions, paths):
        require(type(passed) is bool, 'verification result must be boolean')
        evidence = [{'path': str(path.resolve()), 'sha256': file_hash(path)} for path in paths]
        require(bool(evidence), 'verification needs evidence files')
        for row in self.rows:
            if row['verifier'] == verifier:
                row.update(status='passed' if passed else 'failed', versions=versions, evidence=evidence,
                           verified_scope=VERIFIERS[verifier]['scope'])
        self.save()

    def require_complete(self):
        require(all(row['status'] == 'passed' for row in self.rows), 'phase verification tasks remain incomplete')
        for row in self.rows:
            for evidence in row['evidence']:
                from pathlib import Path
                require(file_hash(Path(evidence['path'])) == evidence['sha256'], 'phase verification evidence changed')
