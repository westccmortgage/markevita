"""Durable, at-most-once synchronous paid operations for the hosted runner."""
import hashlib
import json


class PaidCalls:
    def __init__(self, state, budget):
        self.state, self.budget = state, budget

    def once(self, provider, params, reserve, call, actual=None):
        key = provider + ':' + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        records = self.state.data.setdefault('paid_operations', {})
        rec = records.get(key)
        if rec:
            if rec['status'] == 'succeeded':
                return rec['result']
            raise RuntimeError(f'{provider}: interrupted request needs reconciliation; no automatic paid retry.')
        records[key] = {'provider': provider, 'status': 'reserved', 'estimated_cost': reserve}
        try:
            self.budget.reserve(reserve, provider)
        except Exception:
            # No call occurred; only discard an intent when the reserve itself
            # was refused. A failed remote checkpoint must stop this worker.
            if self.state.data.get('status') == 'needs_budget_override':
                records.pop(key, None)
                self.state.save()
            raise
        result = call()  # Exceptions retain the reserved record for reconciliation.
        rec = records[key]
        rec.update(status='succeeded', result=result)
        self.budget.settle(reserve, actual(result) if actual else reserve, provider, key)
        return result
