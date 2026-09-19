"""Public fal diagnostics built from codes, never response text or input URLs."""
import re


class ProviderFailure(RuntimeError):
    """Safe for the job page; the underlying exception must remain private.

    `decided` marks a failure about this one piece of work — the model would
    not make it, and asking again changes nothing. Everything else is about
    the connection or the account, and stops the run where it stands: burning
    every remaining scene's attempts against an outage helps nobody.
    """

    def __init__(self, message, decided=False):
        super().__init__(message)
        self.decided = bool(decided)


_TYPES = {
    "file_download_error": "fal.ai could not download an input file. Check the saved request in fal.ai before retrying.",
    "image_load_error": "fal.ai could not read an input image. Check the saved request in fal.ai before retrying.",
    "content_policy_violation": "fal.ai refused to generate this under its content policy. Nothing is "
                                "retried on its own: change the wording it was given — for a reference that is "
                                "the character's appearance or the wardrobe item, for a clip it is the scene's "
                                "action — and run the episode again.",
    "face_detection_error": "fal.ai could not detect the required face in the input image.",
    "image_too_large": "fal.ai rejected an input file or parameter. Check the saved request details.",
    "file_too_large": "fal.ai rejected an input file or parameter. Check the saved request details.",
    "feature_not_supported": "fal.ai rejected an input file or parameter. Check the saved request details.",
    "bad_request": "fal.ai rejected an input file or parameter. Check the saved request details.",
}
_INFRA = {"request_timeout", "startup_timeout", "runner_scheduling_failure", "runner_connection_timeout",
          "runner_disconnected", "runner_connection_refused", "runner_connection_error",
          "runner_incomplete_response", "runner_server_error", "internal_error"}
for _name in _INFRA:
    _TYPES[_name] = "fal.ai reported a processing failure. Check the saved request status before continuing."


def fal_diagnostic(exc, request_id=None, phase="collect", what=""):
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    status = status if type(status) is int and 400 <= status <= 599 else None
    candidates = [getattr(exc, "error_type", None)]
    if response is not None:
        candidates.append(response.headers.get("x-fal-error-type"))
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            candidates.append(body.get("error_type"))
            if isinstance(body.get("detail"), list):
                candidates += [item.get("type") for item in body["detail"] if isinstance(item, dict)]
    kind = next((x for x in candidates if isinstance(x, str) and x in _TYPES), None)
    advice = _TYPES.get(kind)
    if advice is None:
        if status == 401:
            advice = "fal.ai authentication failed. Open Integrations to compare the loaded key ID and check authentication without generation."
        elif status == 402:
            advice = "fal.ai requires a billing check. Check the fal.ai account balance and billing status."
        elif status == 403 and phase == "collect":
            # A refusal while READING a request the queue already accepted is
            # not evidence that a new submission would be refused. Saying so
            # plainly keeps an operator from rotating a working key.
            advice = ("fal.ai denied access while reading a request it had already accepted. "
                      "This is not proof that a new submission would be refused. The saved "
                      "request is kept, and Resume re-reads it instead of paying again.")
        elif status == 403:
            # A run that had billed a hundred and fifty images that same day
            # stopped here, and this sentence sent its producer to check key
            # permissions that were provably fine. An exhausted balance is
            # refused with this code, and that is where to look first.
            advice = ("fal.ai denied access at submission. A key that has already been billed "
                      "for work in this same run has the permissions it needs, so check the "
                      "fal.ai balance first: an account out of credit is refused this way.")
        elif status == 429:
            advice = "fal.ai reported a request limit. Check the fal.ai request status before continuing."
        elif status == 422:
            advice = "fal.ai rejected an input file or parameter. Check the saved request details."
        else:
            advice = "fal.ai could not complete this operation. Check the saved request status before continuing."
    info = {"provider": "fal.ai", "phase": phase, "http_status": status, "error_type": kind}
    prefix = "fal.ai" + (f" HTTP {status}" if status else "") + (f" ({kind})" if kind else "")
    if isinstance(request_id, str) and re.fullmatch(r"[a-fA-F0-9]{8}-[a-fA-F0-9-]{27,40}", request_id):
        info["request_id"] = request_id
        prefix += f" · request {request_id}"
    # What was being made, so a refusal names the reference or clip to fix
    # instead of only a request id. It is our own label, never provider text.
    if isinstance(what, str) and what.strip():
        prefix += " · " + what.strip()[:120]
    info["message"] = prefix + ": " + advice
    return info
