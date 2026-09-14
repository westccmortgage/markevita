"""One queue submission per run; only proven pre-queue refusals can be resumed.

Submission and result polling share one credential source and use the queue's
own request URLs. Polling through the SDK resolved a second key from the raw
environment and rebuilt the URL itself; a refusal there was indistinguishable
from a refused submission and impossible to diagnose without the response text.
"""
import math
import re
import time
import requests

from serial.fal_auth import key_id_prefix
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


_QUEUE = 'https://queue.fal.run/'


def _request_base(endpoint, request_id):
    """Queue URL for a request whose take predates stored request URLs.

    Status and result live under owner/alias; the endpoint's sub-path
    (e.g. /edit) is not part of the request address.
    """
    from fal_client.client import AppId
    app = AppId.from_endpoint_id(endpoint)
    prefix = f'{app.namespace}/' if app.namespace else ''
    return f'{_QUEUE}{prefix}{app.owner}/{app.alias}/requests/{request_id}'


def _provider_explanation(exc, request_id, secret, log=None):
    """fal.ai's own explanation of a refusal, scrubbed.

    The persisted diagnostic and the error banner deliberately carry codes
    only, never provider text (see provider_errors). But a refusal with no
    reason is unactionable: it sends an operator to rotate a working key,
    repeatedly. So the provider's own wording goes to the two places built
    for exactly that — the host's log stream, and the job log, which the page
    already presents as provider diagnostics in their original language.

    URLs and the credential are removed before either.
    """
    response = getattr(exc, 'response', None)
    if response is None:
        return
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = None
    text = ''
    if isinstance(body, dict):
        for key in ('detail', 'message', 'error', 'msg'):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
            if isinstance(value, list):
                text = ' | '.join(str(item.get('msg') or item.get('type') or '')
                                  for item in value if isinstance(item, dict))
                break
    text = re.sub(r'https?://\S+', '<url>', text or '')
    if secret:
        text = text.replace(secret, '<key>')
    text = re.sub(r'\s+', ' ', text).strip()[:300]
    status = getattr(response, 'status_code', '?')
    line = (f'[fal] HTTP {status} request {request_id or "-"}: '
            f'{text or "(no explanation in response body)"}')
    print(line, flush=True)
    if log is not None:
        try:
            log(line)
        except Exception:
            pass   # A log sink must never mask the provider failure itself.


class DurableFal(Fal):
    def __init__(self, cfg, log, state, budget, inputs, released=()):
        super().__init__(cfg, log, state, budget, inputs)
        # Request ids an administrator has recorded as unreachable. Each one
        # permits exactly one fresh submission for that take and nothing else.
        self.released = frozenset(released)

    def _orphaned(self, take):
        """True when the saved request belongs to a key this server no longer
        holds. A rotated key cannot read back a request it did not create, so
        polling it again can only fail — the take has to be submitted anew."""
        info = take.get('provider_error') or {}
        refused = (info.get('provider') == 'fal.ai' and info.get('phase') == 'collect'
                   and info.get('http_status') in _ACCESS_REFUSALS)
        recorded, current = take.get('key_id'), key_id_prefix(self.cfg.fal_key)
        return bool(refused and recorded and current and recorded != current)

    def _release_unreachable(self, take, take_id, reason):
        """Retire a request that cannot be read back, keeping its id.

        The queue accepted this request, so the provider may have generated and
        billed it. Charging the estimate keeps the episode budget honest; the
        alternative — releasing the reservation at zero — would understate
        spending on work that may well have been done.
        """
        amount = take.get('estimated_cost')
        if (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
                or self.budget.reserved + 0.0001 < amount):
            raise RuntimeError('Provider reservation needs reconciliation before another submission.')
        info = take.get('provider_error') or {}
        take.setdefault('unreachable_requests', []).append({
            'request_id': take['request_id'], 'key_id': take.get('key_id'),
            'current_key_id': key_id_prefix(self.cfg.fal_key), 'reason': reason,
            'http_status': info.get('http_status'), 'phase': info.get('phase'),
            'released_at': now(), 'charged_usd': amount})
        for key in ('request_id', 'status_url', 'response_url', 'provider_error'):
            take.pop(key, None)
        take['status'] = 'planned'
        # settle() persists the whole take in one write, so the retired request
        # and its charge are checkpointed together.
        self.budget.settle(amount, amount, 'fal.ai request unreachable; assumed billed', take_id)

    def _auth_headers(self):
        """The single credential used for submission AND polling."""
        return {'Authorization': 'Key ' + self.cfg.fal_key, 'X-Fal-No-Retry': '1'}

    def _wait(self, endpoint, request_id):
        """Poll the queue's own request URLs with the submission credential."""
        take = next((t for t in self.state.data.get('takes', {}).values()
                     if t.get('request_id') == request_id), {})
        base = _request_base(endpoint, request_id)
        status_url = take.get('status_url') or base + '/status'
        response_url = take.get('response_url') or base
        headers = self._auth_headers()
        delay = 3
        while True:
            status = requests.get(status_url, headers=headers, timeout=60, allow_redirects=False)
            status.raise_for_status()
            if (status.json() or {}).get('status') == 'COMPLETED':
                result = requests.get(response_url, headers=headers, timeout=60, allow_redirects=False)
                result.raise_for_status()
                return result.json()
            time.sleep(delay)
            delay = min(delay + 2, 15)

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
        if take.get('request_id') and take.get('status') == 'submitted':
            if take['request_id'] in self.released:
                self._release_unreachable(take, take_id, 'administrator recorded the saved request as unreachable')
            elif self._orphaned(take):
                self._release_unreachable(take, take_id, 'submitted with a fal key this server no longer holds')
        if not take.get('request_id'):
            # Clear prior evidence BEFORE reserving. If this attempt loses its
            # response, an earlier 403 cannot authorize a further submission.
            take.pop('provider_error', None)
            take.update(provider='fal.ai', endpoint=endpoint, what=what, estimated_cost=est_cost,
                        params={k: v for k, v in args.items() if k not in ('image_url','image_urls','video_url','audio_url')},
                        status='reserved', submitted_at=now(), key_id=key_id_prefix(self.cfg.fal_key))
            try:
                self.budget.reserve(est_cost, what)
            except Exception:
                if self.state.data.get('status') == 'needs_budget_override':
                    take['status'] = 'planned'
                    self.state.save()
                raise
            # requests has no implicit POST retry. Provider retries are disabled too.
            try:
                response = requests.post(_QUEUE + endpoint, json=args,
                    headers=self._auth_headers(), timeout=60, allow_redirects=False)
                response.raise_for_status()
            except requests.exceptions.RequestException as exc:
                _provider_explanation(exc, None, self.cfg.fal_key, self.log)
                info = fal_diagnostic(exc, phase='submit')
                info['submission_rejected'] = _response_refused(exc)
                take['provider_error'] = info
                if _known_refusal(take):
                    self._record_refusal(take, take_id)
                else:
                    self.state.save()
                raise ProviderFailure(info['message']) from exc
            accepted = response.json()
            take['request_id'] = accepted['request_id']
            # The queue's own addresses for this request; polling uses them
            # verbatim instead of rebuilding the path.
            for key in ('status_url', 'response_url'):
                if isinstance(accepted.get(key), str) and accepted[key].startswith(_QUEUE):
                    take[key] = accepted[key]
            take['status'] = 'submitted'
            self.state.save()  # Includes private R2 checkpoint before waiting.
        try:
            result = self._wait(take.get('endpoint') or endpoint, take['request_id'])
        except Exception as exc:
            _provider_explanation(exc, take['request_id'], self.cfg.fal_key, self.log)
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
