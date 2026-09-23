"""Public lifecycle operation for disposable evaluation instances."""


class EvaluationMixin:
    def instance_discard_evaluation(self, instance_id, *, account_ref=None):
        account_id = (self.account_service.resolve(account_ref, include_retired=True).id
                      if account_ref is not None else None)
        result = self.store.discard_evaluation(instance_id, account_id=account_id)
        if result['discarded']:
            for turn_id, process in list(self._children.items()):
                if process.poll() is not None:
                    process.wait()
                    self._children.pop(turn_id, None)
        return result
