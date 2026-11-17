"""The single exception-to-transport mapping shared by every API surface.

CONTRACT.md calls this "the only place this translation happens". HTTP and the CLI
therefore both map through :func:`describe`; neither restates the table. A second copy
drifts — the first one did, losing ``limit_name`` on a rejected upload and reporting
Python class names where HTTP reported stable codes — and a drifting error contract is
indistinguishable from a broken one to the agent reading it.

No policy is decided here. Statuses come from the contract's table and every value in a
body comes off the exception that was raised, so adding a rule to this module is a
defect in the same way that adding one to a route handler is.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from orchestrator.adapters.assets.models import ProviderConfigurationError
from orchestrator.adapters.audiopost.runner import AudioPostRequestError
from orchestrator.adapters.minimax_h3.core import H3RequestError, H3SetupError
from orchestrator.adapters.tts.core import TtsRequestError, TtsSetupError
from orchestrator.domain.state import ReviewApprovalError, TransitionError
from orchestrator.platform.containment import ContainmentError, UploadRejected
from orchestrator.platform.errors import JobError
from orchestrator.service.assets import AssetRegistrationInvalid
from orchestrator.service.dependencies import ShotProposalWriteFailure
from orchestrator.store.json_store import RecordNotFound, VersionConflict


class MissingAdapter(Exception):
    """An operation is routed, typed and documented, but its adapter does not exist.

    Lives here rather than in a route module because it is part of the error contract:
    the 501 it maps to is what tells the UI and the agent which adapter is missing.
    """

    def __init__(self, adapter: str) -> None:
        super().__init__(f"missing adapter: {adapter}")
        self.adapter = adapter


class RequestMismatch(Exception):
    """A request body contradicts an identifier in its own URL.

    Without this the URL is decorative: a POST to ``.../shots/A/revise`` carrying shot
    ``B`` in its body would revise ``B``, so logs, audit trails and any authorisation
    ever placed on the path would all describe the wrong record.
    """

    def __init__(self, field: str, path_value: str, body_value: str) -> None:
        super().__init__(
            f"{field} is {path_value!r} in the URL but {body_value!r} in the body; "
            "the URL must identify the record being changed"
        )
        self.field = field
        self.path_value = path_value
        self.body_value = body_value


class AdapterRequestInvalid(Exception):
    """A routed job request lacks a valid Shot binding or resource declaration."""


#: Keys promoted to the top level of every body when the exception supplies them, so a
#: client can read them without knowing which exception it is looking at (Amendment 1).
_PROMOTED = ("expected", "actual", "current", "limit", "observed", "remediation")

#: Exception types :func:`describe` maps. Transport-specific failures (a missing token,
#: a malformed request body) are mapped by the surface that can raise them.
MAPPED: tuple[type[BaseException], ...] = (
    VersionConflict, RecordNotFound, ContainmentError, UploadRejected,
    H3RequestError, H3SetupError, TtsRequestError, TtsSetupError, AudioPostRequestError,
    TransitionError, ReviewApprovalError, JobError, MissingAdapter, RequestMismatch,
    AdapterRequestInvalid,
    AssetRegistrationInvalid,
    ProviderConfigurationError,
    ShotProposalWriteFailure,
    ValidationError,
)


def error_body(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build the one error-body shape every surface emits.

    The key set is fixed and independent of the exception: a client parses one shape,
    and an absent value is present as ``None`` rather than as a missing key.
    """
    return {
        "error": code,
        "kind": details.get("kind", code),
        "message": message,
        "retryable": bool(details.get("retryable", False)),
        **{key: details.get(key) for key in _PROMOTED},
        "details": details,
    }


def describe(exc: BaseException) -> tuple[int, dict[str, Any]] | None:
    """Return ``(status, body)`` for a mapped exception, or ``None`` if unmapped.

    ``None`` means "this is not a request failure" and must not be turned into a tidy
    response: over HTTP it stays an unhandled error, which is a 500 and a bug to fix.
    """
    if isinstance(exc, TtsRequestError):
        return 422, error_body("validation_error", str(exc), field="speech_request")
    if isinstance(exc, AudioPostRequestError):
        return 422, error_body("validation_error", str(exc), field="audio_post_request")
    if isinstance(exc, TtsSetupError):
        return 503, error_body("tts_setup_unavailable", str(exc), field="tts")
    if isinstance(exc, H3RequestError):
        return 422, error_body("validation_error", str(exc), field="h3_request")
    if isinstance(exc, H3SetupError):
        return 503, error_body("h3_setup_unavailable", str(exc), field="minimax_h3")
    if isinstance(exc, VersionConflict):
        current = (
            exc.current.model_dump(mode="json")
            if hasattr(exc.current, "model_dump") else exc.current
        )
        # Amendment 1 asks the body to say whether a stale content version or a stale
        # mutation token caused this. It cannot be told from here: both `expected` and
        # `actual` are mutation tokens, and an earlier attempt to infer it by comparing
        # `current.version` against `expected` compared a content version against a
        # token and so misclassified ordinary token conflicts. Guessing is worse than
        # silence, and deciding it here would be exactly the transport-layer policy
        # this contract forbids. The store owns the distinction and must surface it on
        # the exception before this body can carry it.
        return 409, error_body(
            "version_conflict", str(exc), expected=exc.expected, actual=exc.actual,
            current=current, record=exc.record,
        )
    if isinstance(exc, RecordNotFound):
        return 404, error_body("not_found", str(exc))
    if isinstance(exc, ContainmentError):
        return 400, error_body("containment_error", str(exc), field="path", value=exc.path)
    if isinstance(exc, UploadRejected):
        details: dict[str, Any] = {"field": "file"}
        if exc.limit_name is not None:
            details.update(limit_name=exc.limit_name, limit=exc.limit, observed=exc.observed)
        return 400, error_body("upload_rejected", str(exc), **details)
    if isinstance(exc, TransitionError):
        # The statuses go in details, not into the promoted "current": that key means
        # the stored head record everywhere else, and one key with two meanings is a
        # worse contract than two keys.
        return 422, error_body("transition_refused", str(exc), reason=exc.reason,
                               from_status=exc.current.value, to_status=exc.target.value)
    if isinstance(exc, ReviewApprovalError):
        return 422, error_body("review_approval_refused", str(exc), reason=str(exc))
    if isinstance(exc, JobError):
        # FR-27's taxonomy exists for the remediation hint, so it is carried verbatim
        # rather than flattened into the message.
        return (409 if exc.retryable else 422), error_body(
            "job_error", exc.message, kind=exc.kind.value, retryable=exc.retryable,
            remediation=exc.remediation, job_id=exc.job_id, shot_id=exc.shot_id,
            run_id=exc.run_id,
        )
    if isinstance(exc, MissingAdapter):
        return 501, error_body("missing_adapter", str(exc), adapter=exc.adapter)
    if isinstance(exc, RequestMismatch):
        return 400, error_body("request_mismatch", str(exc), field=exc.field,
                               expected=exc.path_value, actual=exc.body_value)
    if isinstance(exc, AdapterRequestInvalid):
        return 422, error_body("validation_error", str(exc), field="payload")
    if isinstance(exc, AssetRegistrationInvalid):
        return 422, error_body("validation_error", str(exc), field="asset")
    if isinstance(exc, ProviderConfigurationError):
        return 422, error_body("provider_configuration_error", str(exc), provider=exc.provider)
    if isinstance(exc, ShotProposalWriteFailure):
        return 500, error_body(
            "partial_write", str(exc), written=list(exc.written),
            not_written=list(exc.not_written), cause=str(exc.cause),
        )
    if isinstance(exc, ValidationError):
        return 422, error_body("validation_error", str(exc), field="payload")
    return None
