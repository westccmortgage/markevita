"""Queue submission with no paid retries; collection reuses durable request ids."""
import requests

from serial.providers import Fal
from serial.state import now


class DurableFal(Fal):
    def run(self, endpoint, args, take_id, est_cost, what, stub):
        take = self.state.take(take_id)
        if take.get('status') == 'succeeded':
            return take['result'], take
        if take.get('status') in ('reserved', 'failed', 'submission_unknown') and not take.get('request_id'):
            raise RuntimeError(f'{take_id}: submission outcome is unknown. Reconcile this request before another paid attempt.')
        if not take.get('request_id'):
            take.update(provider='fal.ai', endpoint=endpoint, what=what, estimated_cost=est_cost,
                        params={k: v for k, v in args.items() if k not in ('image_url','image_urls','video_url','audio_url')},
                        status='reserved', submitted_at=now())
            try:
                self.budget.reserve(est_cost, what)
            except Exception:
                if self.state.data.get('status') == 'needs_budget_override':
                    take['status'] = 'planned'
                    self.state.save()
                raise
            # requests has no implicit POST retry. Provider retries are disabled too.
            response = requests.post('https://queue.fal.run/' + endpoint, json=args,
                headers={'Authorization': 'Key ' + self.cfg.fal_key, 'X-Fal-No-Retry': '1'}, timeout=60)
            response.raise_for_status()
            take['request_id'] = response.json()['request_id']
            take['status'] = 'submitted'
            self.state.save()  # Includes private R2 checkpoint before waiting.
        result = self._wait(endpoint, take['request_id'])
        take.update(status='succeeded', completed_at=now(), result=result,
                    actual_cost=est_cost, actual_cost_source='provider estimate')
        # Result and its cost are committed in one state/checkpoint update.
        self.budget.settle(est_cost, est_cost, what, take_id)
        return result, take
