"""Public fal diagnostics built from codes, never response text or input URLs."""
import re


class ProviderFailure(RuntimeError):
    """Safe for the job page; the underlying exception must remain private."""


_TYPES = {
    "file_download_error": "fal.ai could not download an input file. Check the saved request in fal.ai before retrying.",
    "image_load_error": "fal.ai could not read an input image. Check the saved request in fal.ai before retrying.",
    "content_policy_violation": "fal.ai rejected the request under its content policy. Review the provider result before continuing.",
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


def fal_diagnostic(exc, request_id=None, phase="collect"):
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
        elif status == 403:
            advice = "fal.ai denied access. Check the key permissions and model access in fal.ai."
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
    info["message"] = prefix + ": " + advice
    return info
