"""One queue submission per run; only proven pre-queue refusals can be resumed."""
import math
import requests

from serial.providers import Fal
from serial.state import now
from .provider_errors import ProviderFailure, fal_diagnostic


_ACCESS_REFUSALS = {401, 402, 403}
_UNCERTAIN = {'reserved', 'failed', 'submission_unknown'}


def _known_refusal(take):
    """Also recognizes the old reserved records that retained a submit HTTP code.

    A collection error, any assigned request id, or a transport failure must
    never open the submission gate. An explicit False is a newer record with
    conflicting response evidence; it must not be treated as legacy evidence.
    """
    info = take.get('provider_error') or {}
    return (take.get('provider') == 'fal.ai' and not take.get('request_id')
            and info.get('provider') == 'fal.ai' and info.get('phase') == 'submit'
            and info.get('http_status') in _ACCESS_REFUSALS
            and info.get('submission_rejected') is not False
            and not info.get('request_id') and not info.get('error_type'))


def _response_refused(exc):
    """Classify only an explicit HTTP refusal, never a lost response or result."""
    response = getattr(exc, 'response', None)
    if not isinstance(exc, requests.exceptions.HTTPError) or response is None:
        return False
    if (response.status_code not in _ACCESS_REFUSALS or response.history
            or response.headers.get('x-fal-error-type')):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return (isinstance(body, dict)
            and not isinstance(body.get('detail'), (dict, list))
            and not any(body.get(k) for k in ('request_id', 'status_url', 'response_url', 'cancel_url', 'error_type')))


class DurableFal(Fal):
    def _record_refusal(self, take, take_id):
        amount = take.get('estimated_cost')
        if (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
                or self.budget.reserved + 0.0001 < amount):
            raise RuntimeError('Provider reservation needs reconciliation before another submission.')
        # The refusal, audit record, and release are one durable checkpoint.
        take.update(status='submission_rejected', rejected_at=now())
        take.setdefault('submission_rejections', []).append({
            **take['provider_error'], 'submitted_at': take.get('submitted_at'),
            'rejected_at': take['rejected_at'], 'reservation_released_usd': amount})
        self.budget.settle(amount, 0, 'fal.ai submission rejected before queue acceptance', take_id)

    def run(self, endpoint, args, take_id, est_cost, what, stub):
        take = self.state.take(take_id)
        if take.get('status') == 'succeeded':
            return take['result'], take
        if (take.get('status') in _UNCERTAIN and take.get('endpoint') == endpoint
                and _known_refusal(take)):
            self._record_refusal(take, take_id)
        if take.get('status') in _UNCERTAIN and not take.get('request_id'):
            raise RuntimeError(f'{take_id}: submission outcome is unknown. Reconcile this request before another paid attempt.')
        if take.get('status') == 'submission_rejected' and not _known_refusal(take):
            raise RuntimeError(f'{take_id}: submission outcome is unknown. Reconcile this request before another paid attempt.')
        if not take.get('request_id'):
            # Clear prior evidence BEFORE reserving. If this attempt loses its
            # response, an earlier 403 cannot authorize a further submission.
            take.pop('provider_error', None)
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
            try:
                response = requests.post('https://queue.fal.run/' + endpoint, json=args,
                    headers={'Authorization': 'Key ' + self.cfg.fal_key, 'X-Fal-No-Retry': '1'},
                    timeout=60, allow_redirects=False)
                response.raise_for_status()
            except requests.exceptions.RequestException as exc:
                info = fal_diagnostic(exc, phase='submit')
                info['submission_rejected'] = _response_refused(exc)
                take['provider_error'] = info
                if _known_refusal(take):
                    self._record_refusal(take, take_id)
                else:
                    self.state.save()
                raise ProviderFailure(info['message']) from exc
            take['request_id'] = response.json()['request_id']
            take['status'] = 'submitted'
            self.state.save()  # Includes private R2 checkpoint before waiting.
        try:
            result = self._wait(take.get('endpoint') or endpoint, take['request_id'])
        except Exception as exc:
            info = fal_diagnostic(exc, take['request_id'])
            take['provider_error'] = info
            self.state.save()
            raise ProviderFailure(info['message']) from exc
        take.pop('provider_error', None)
        take.update(status='succeeded', completed_at=now(), result=result,
                    actual_cost=est_cost, actual_cost_source='provider estimate')
        # Result and its cost are committed in one state/checkpoint update.
        self.budget.settle(est_cost, est_cost, what, take_id)
        return result, take
