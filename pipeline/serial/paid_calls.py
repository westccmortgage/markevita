"""Durable, at-most-once synchronous paid operations for the hosted runner."""
import hashlib
import json


# Providers whose interrupted call leaves nothing behind to reconcile.
#
# A text completion is delivered over the same connection that requested it:
# when that connection fails there is no request id to look up, no artifact
# waiting on the provider's side, and no way for the answer to arrive later.
# The next attempt is an ordinary new request, exactly as any API client would
# make. What is at stake is the tokens already spent, so those are charged in
# full at the reserved estimate and the episode is allowed to carry on.
#
# Media providers are deliberately absent. Their interrupted submission may
# still be running and may still deliver a file, so repeating it is paying
# twice for one clip. Those keep the hard stop and a person decides.
RESUBMITTABLE = frozenset({'anthropic'})


class PaidCalls:
    def __init__(self, state, budget):
        self.state, self.budget = state, budget

    def _abandon(self, key, rec, provider):
        """Charge an interrupted text call and let the work be attempted again."""
        records = self.state.data.setdefault('paid_operations', {})
        records.pop(key, None)
        spent = float(rec.get('estimated_cost') or 0.0)
        self.state.data.setdefault('abandoned_operations', []).append(
            {'provider': provider, 'charged': spent})
        self.state.save()
        if spent:
            self.budget.settle(spent, spent, provider + ':interrupted', key)

    def once(self, provider, params, reserve, call, actual=None):
        key = provider + ':' + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        records = self.state.data.setdefault('paid_operations', {})
        rec = records.get(key)
        if rec:
            if rec['status'] == 'succeeded':
                return rec['result']
            if provider in RESUBMITTABLE:
                # Left over from a worker that died mid-call. Charge it and ask
                # again rather than leaving the episode unable to ever continue.
                self._abandon(key, rec, provider)
            else:
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
        try:
            result = call()  # Exceptions retain the reserved record for reconciliation.
        except Exception:
            if provider in RESUBMITTABLE:
                self._abandon(key, records[key], provider)
            raise
        rec = records[key]
        rec.update(status='succeeded', result=result)
        self.budget.settle(reserve, actual(result) if actual else reserve, provider, key)
        return result
