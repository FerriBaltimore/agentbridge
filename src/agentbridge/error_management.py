"""Public local error-learning operations, separate from execution policy."""
from .error_learning import Learning
from . import error_diagnosis


class ErrorManagementMixin:
    @property
    def error_learning(self):
        return Learning(self.store)

    def error_cases(self, *, limit=100, cursor=0):
        return self.error_learning.list_cases(limit=limit, cursor=cursor)

    def error_case(self, case_id):
        return self.error_learning.get_case(case_id)

    def error_diagnose(self, case_id, *, account_ref, model, idempotency_key, timeout=60):
        return error_diagnosis.diagnose(self, case_id, account_ref=account_ref, model=model,
                                        idempotency_key=idempotency_key, timeout=timeout)

    def error_diagnosis(self, idempotency_key):
        return error_diagnosis.get(self.store, idempotency_key)

    def error_propose(self, case_id, result):
        return self.error_learning.propose(case_id, result)

    def error_proposal(self, proposal_id):
        return self.error_learning.get_proposal(proposal_id)

    def error_validate(self, proposal_id):
        return self.error_learning.validate(proposal_id)

    def error_rule(self, rule_id):
        return self.error_learning.get_rule(rule_id)

    def error_activate(self, proposal_id, expected_revision):
        return self.error_learning.activate(proposal_id, expected_revision)

    def error_deactivate(self, rule_id, expected_revision):
        return self.error_learning.deactivate(rule_id, expected_revision)
