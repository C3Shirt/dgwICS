"""Phase verification never turns future work or stale files into proof."""
from pathlib import Path
import tempfile
import unittest

from factoryio_agents.contracts import ContractError, read_json
from factoryio_agents.verification import VerificationLedger, validate_verification_tasks


class VerificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.task = {'task_id': 'fixture', 'verification_tasks': [{'id': 'geometry', 'description': 'current loaded visual check',
             'verifier': 'scene_visual', 'stage': 'scene_review', 'owner': 'vision'}]}
        self.evidence = self.root / 'proof.json'; self.evidence.write_text('{}')

    def test_pending_or_failed_verification_does_not_pass(self):
        ledger = VerificationLedger(self.task, self.root)
        with self.assertRaises(ContractError): ledger.require_complete()
        ledger.observe('scene_visual', False, {'scene': 's'}, [self.evidence])
        with self.assertRaises(ContractError): ledger.require_complete()
        ledger.observe('scene_visual', True, {'scene': 's'}, [self.evidence])
        ledger.require_complete()

    def test_geometry_revision_invalidates_previous_review(self):
        ledger = VerificationLedger(self.task, self.root)
        ledger.observe('scene_visual', True, {'scene': 's'}, [self.evidence])
        ledger.invalidate(['scene_visual'], {'scene': 'next'})
        with self.assertRaises(ContractError): ledger.require_complete()
        self.assertFalse(read_json(ledger.path)['all_completed'])

    def test_changed_evidence_cannot_close_a_phase_task(self):
        ledger = VerificationLedger(self.task, self.root)
        ledger.observe('scene_visual', True, {'scene': 's'}, [self.evidence])
        self.evidence.write_text('changed')
        with self.assertRaisesRegex(ContractError, 'evidence changed'): ledger.require_complete()

    def test_invented_verifier_or_wrong_owner_is_rejected(self):
        for key,value in [('verifier','trust_model'),('owner','planner'),('stage','planning')]:
            row = self.task['verification_tasks'][0]
            original = row[key]; row[key] = value
            with self.assertRaises(ContractError): validate_verification_tasks(self.task)
            row[key] = original
