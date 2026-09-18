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
# Terminal queue states. Polling one of these forever is how a job spends
# three hours in "running" with the stage never moving.
_QUEUE_FAILED = {'FAILED', 'ERROR', 'CANCELLED', 'CANCELED', 'TIMED_OUT'}

# Every state a take can be in must have a way out — one the studio takes by
# itself, or one the producer is actually offered on a screen. It was not
# written down, so it was not checked, and the takes accumulated exits with
# no return: a saved request whose answer could never change was re-read on
# every resume, for ever. Each one was found by an episode dying on it.
#
# These are the answers that mean the request produced nothing and never will.
# 404: the queue has no record of it — it cannot deliver later, so there is
# nothing to reconcile and nothing that repeating can duplicate. 422: the run
# finished with no output, whether the model declined the subject or the file
# we handed it could not be read. In every one of these the provider has said
# there is no result, so a fresh submission cannot pay twice for one picture:
# there is no picture.
_NO_OUTPUT = {404, 422}

# A request that has only just been accepted can answer 404 for a moment while
# the queue catches up with itself. Retiring it on the first one would throw
# away work that was about to appear, so it has to say so twice.
_CONFIRM_TWICE = {404}
NO_OUTPUT_ATTEMPTS = 2


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
    def __init__(self, cfg, log, state, budget, inputs, released=(), reconciled=()):
        super().__init__(cfg, log, state, budget, inputs)
        # Request ids an administrator has recorded as unreachable. Each one
        # permits exactly one fresh submission for that take and nothing else.
        self.released = frozenset(released)
        # Takes whose submission never got a request id, and which a producer
        # has since checked with the provider and found delivered nothing.
        # Same rule: one fresh submission for that take and nothing else.
        self.reconciled = frozenset(reconciled)

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

    def _wait(self, endpoint, request_id):  # noqa: C901
        """Poll the queue's own request URLs with the submission credential."""
        take = next((t for t in self.state.data.get('takes', {}).values()
                     if t.get('request_id') == request_id), {})
        base = _request_base(endpoint, request_id)
        status_url = take.get('status_url') or base + '/status'
        response_url = take.get('response_url') or base
        headers = self._auth_headers()
        limit = int(getattr(self.cfg, 'fal_request_timeout_seconds', 1800) or 0)
        started, delay = time.monotonic(), 3
        while True:
            status = requests.get(status_url, headers=headers, timeout=60, allow_redirects=False)
            status.raise_for_status()
            state = (status.json() or {}).get('status')
            if state == 'COMPLETED':
                result = requests.get(response_url, headers=headers, timeout=60, allow_redirects=False)
                result.raise_for_status()
                return result.json()
            if state in _QUEUE_FAILED:
                raise RuntimeError(f'the queue reports request {request_id} as {state}')
            waited = time.monotonic() - started
            if limit and waited > limit:
                # The request is left exactly as it is: the take keeps its id and
                # its submitted status, so resuming polls this same request. No
                # second paid submission comes out of giving up on the wait.
                raise RuntimeError(
                    f'request {request_id} is still {state or "unfinished"} after '
                    f'{int(waited / 60)} minutes. It is kept as it is — resuming this episode '
                    'polls the same request again and does not pay for another.')
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

    def _retire_no_output(self, take, take_id):
        """The queue ran this request and it produced nothing.

        That is a definite answer, not a lost one: no result is waiting to be
        collected, and asking again generates nothing twice. Re-reading it is
        the one thing that cannot work, and it is what happened — the saved
        request was polled on every resume and answered 422 every time, so a
        single portrait stopped a whole reference pack for good.

        The request id is kept in the record; it simply stops being treated as
        a result still to collect. The estimate is charged rather than
        released, because the request did run and may well have been billed.
        """
        amount = take.get('estimated_cost')
        if (type(amount) in (int, float) and math.isfinite(amount)
                and 0 <= amount <= self.budget.reserved + 0.0001):
            self.budget.settle(amount, amount, 'fal.ai produced no output', take_id)
        take.setdefault('no_output', []).append(
            {**(take.get('provider_error') or {}), 'request_id': take.get('request_id'),
             'retired_at': now()})
        take['no_output_attempts'] = attempts = int(take.get('no_output_attempts') or 0) + 1
        for key in ('request_id', 'status_url', 'response_url'):
            take.pop(key, None)
        take['status'] = 'no_output'
        self.state.save()
        return attempts

    def run(self, endpoint, args, take_id, est_cost, what, stub):
        take = self.state.take(take_id)
        if take.get('status') == 'succeeded':
            return take['result'], take
        if (take.get('status') in _UNCERTAIN and take.get('endpoint') == endpoint
                and _known_refusal(take)):
            self._record_refusal(take, take_id)
        stranded = ((take.get('status') in _UNCERTAIN and not take.get('request_id'))
                    or (take.get('status') == 'submission_rejected' and not _known_refusal(take)))
        if stranded and take_id in self.reconciled:
            # The producer has looked at this one with the provider and said
            # it delivered nothing. That decision is theirs to make and only
            # theirs: this is the one case where repeating could pay twice for
            # one picture. It is recorded, the reserve is charged as possibly
            # billed, and exactly this take may be sent once more.
            self._retire_no_output(take, take_id)
            stranded = False
        if stranded:
            # Raised so the producer can see it: as a RuntimeError the take's
            # name never reached the page, so the screen could not offer the
            # one action that resolves this, and the episode had no way out.
            raise ProviderFailure(
                f'{take_id}: submission outcome is unknown. Check this request with the '
                'provider; if it delivered nothing, mark it reconciled on the job page and '
                'only this take is sent again.')
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
                info = fal_diagnostic(exc, phase='submit', what=what)
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
            info = fal_diagnostic(exc, take['request_id'], what=take.get('what') or what)
            take['provider_error'] = info
            status = info.get('http_status')
            if status in _CONFIRM_TWICE and info.get('phase') == 'collect':
                seen = int(take.get('gone_seen') or 0) + 1
                take['gone_seen'] = seen
                self.state.save()
                if seen < 2:
                    raise ProviderFailure(info['message']) from exc
            if status in _NO_OUTPUT and info.get('phase') == 'collect':
                # The picture model declines a borderline subject unevenly —
                # two other angles of the same character went through in this
                # very run — and a file it could not read is one we have since
                # re-signed. One fresh attempt is worth making before a whole
                # reference pack stops for one portrait.
                take.pop('gone_seen', None)
                if self._retire_no_output(take, take_id) < NO_OUTPUT_ATTEMPTS:
                    self.log(f'{take.get("what") or what}: провайдер не выдал результат; новая попытка')
                    return self.run(endpoint, args, take_id, est_cost, what, stub)
                raise ProviderFailure(
                    info['message'] + ' The provider gave no result on either attempt. If this '
                    'is a character or a location, soften how it is described or keep a face '
                    'for it; otherwise the input it was given is what to look at.') from exc
            self.state.save()
            raise ProviderFailure(info['message']) from exc
        take.pop('provider_error', None)
        take.update(status='succeeded', completed_at=now(), result=result,
                    actual_cost=est_cost, actual_cost_source='provider estimate')
        # Result and its cost are committed in one state/checkpoint update.
        self.budget.settle(est_cost, est_cost, what, take_id)
        return result, take
