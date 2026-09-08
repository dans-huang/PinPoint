from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Path as APIPath, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlparse

from .abuse import AbuseControlMiddleware
from .config import AuthenticationMode, ConfigurationError, Settings
from .intelligence_provider import (
    OpenAIResponsesProvider,
    SummaryProviderAmbiguous,
    SummaryProviderRejected,
)
from .intelligence_state import (
    DEFAULT_TEMPLATE_ID,
    IntelligenceStore,
    IntelligenceStoreError,
    InvalidVocabularyTerm,
    StaleSummaryApproval,
    SummaryConcurrencyLimitReached,
    SummaryDailyLimitReached,
    SummaryJobConflict,
    SummaryJobUnavailable,
    TemplateImmutable,
    TemplateLimitReached,
    TemplateUnavailable,
    VocabularyLimitReached,
    VocabularyTermExists,
)
from .plaud import PlaudPartnerClient, PlaudServiceError, PlaudUserToken
from .public_links import (
    apple_app_site_association,
    invitation_code_is_valid,
    invitation_landing_html,
)
from .security import AuthenticationError, SecurityService
from .state import (
    BetaAuthorizationStatus,
    DeviceBindingUnavailable,
    LocalActivationUnavailable,
    QuotaExceeded,
    RecorderClaimConflict,
    StateStore,
    StateStoreError,
    TesterMembershipUnavailable,
    UploadAttemptMismatch,
    UploadAttemptRetired,
    UploadJobUnavailable,
)


class AppleSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_token: str = Field(min_length=20, max_length=16_384)
    authorization_code: str | None = Field(default=None, min_length=1, max_length=4_096)
    nonce: str = Field(min_length=20, max_length=4_096)
    invite_code: str | None = Field(default=None, max_length=128)


class SessionResponse(BaseModel):
    session_token: str
    plaud_user_access_token: str
    user_id: str
    plaud_domain: str
    session_expires_at: datetime
    plaud_token_expires_at: datetime
    deployment_mode: AuthenticationMode


class NonceResponse(BaseModel):
    nonce: str


class LocalSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_code: str = Field(
        min_length=36,
        max_length=128,
        pattern=r"^ppl_[A-Za-z0-9_-]{32,100}$",
    )


class DeviceBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    serial_number: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    device_type: Literal["notepins", "notepro"]


def _lifecycle_error(code: str, message: str) -> dict[str, str]:
    """Return a stable machine code alongside changeable user-facing copy."""
    return {"code": code, "message": message}


def _session_error(code: str, message: str) -> dict[str, str]:
    """Keep enrollment recovery independent from changeable user-facing copy."""
    return {"code": code, "message": message}


def _upload_error(code: str, message: str) -> dict[str, str]:
    """Keep recovery decisions independent from changeable error wording."""
    return {"code": code, "message": message}


class UploadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_size: int = Field(gt=0)
    file_type: str = Field(min_length=2, max_length=12)
    idempotency_key: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    source_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class CompletedPartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    part_number: int = Field(alias="PartNumber", gt=0, le=10_000)
    etag: str = Field(alias="ETag", min_length=1, max_length=256)


class UploadCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_list: list[CompletedPartRequest] = Field(min_length=1, max_length=10_000)
    idempotency_key: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class UploadAbandonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class UploadHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class IntelligenceSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_summary_enabled: bool
    default_template_id: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class IntelligenceTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=4_000)


class IntelligenceTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    instructions: str | None = Field(default=None, min_length=1, max_length=4_000)


class IntelligenceVocabularyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str = Field(min_length=1, max_length=64)


class SummaryJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    template_id: str | None = Field(default=None, min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal["generate", "improve"] = "generate"
    execution: Literal["inline", "background"] = "inline"
    marked_moments: list[Annotated[int, Field(ge=0, le=7 * 24 * 60 * 60)]] = Field(
        default_factory=list,
        max_length=64,
    )


class SummaryApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    base_version: int = Field(ge=0)
    accepted_terms: list[str] = Field(default_factory=list, max_length=20)


@dataclass
class Runtime:
    settings: Settings
    security: SecurityService
    plaud: PlaudPartnerClient
    state: StateStore
    intelligence: IntelligenceStore | None = None
    summary_provider: OpenAIResponsesProvider | None = None


@lru_cache(maxsize=1)
def runtime() -> Runtime:
    settings = Settings.from_environment()
    state_store = StateStore(settings.state_db_path)
    provider = None
    if settings.intelligence_base_url is not None:
        provider = OpenAIResponsesProvider(
            base_url=settings.intelligence_base_url,
            api_key=settings.intelligence_api_key or "",
            model=settings.intelligence_model or "",
            timeout_seconds=settings.intelligence_timeout_seconds,
            max_transcript_chars=settings.intelligence_max_transcript_chars,
            max_output_chars=settings.intelligence_max_output_chars,
        )
    return Runtime(
        settings=settings,
        security=SecurityService(
            session_secret=settings.session_secret,
            user_id_secret_v1=settings.user_id_secret_v1,
            apple_audience=settings.apple_audience,
            session_ttl_seconds=settings.session_ttl_seconds,
            nonce_ttl_seconds=settings.nonce_ttl_seconds,
            state_store=state_store,
            session_absolute_ttl_seconds=settings.session_absolute_ttl_seconds,
            invite_codes=settings.beta_invite_codes,
            apple_token_custody_enabled=settings.apple_token_custody_enabled,
            apple_team_id=settings.apple_team_id,
            apple_key_id=settings.apple_key_id,
            apple_private_key_path=settings.apple_private_key_path,
            apple_refresh_token_key_v1=settings.apple_refresh_token_key_v1,
        ),
        plaud=PlaudPartnerClient(
            client_id=settings.plaud_client_id,
            client_secret=settings.plaud_client_secret,
            api_key=settings.plaud_api_key,
            domain=settings.plaud_api_domain,
            user_token_ttl_seconds=settings.plaud_user_token_ttl_seconds,
        ),
        state=state_store,
        intelligence=IntelligenceStore(settings.state_db_path),
        summary_provider=provider,
    )


app = FastAPI(title="PinPoint Backend", version="0.1.0", docs_url=None, redoc_url=None)
app.add_middleware(AbuseControlMiddleware, runtime_provider=runtime)


@app.middleware("http")
async def prevent_sensitive_response_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/v1/"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    try:
        active = runtime()
        active.state.assert_healthy()
    except (ConfigurationError, StateStoreError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PinPoint backend is not ready",
        ) from exc
    return {"status": "ok"}


@app.get("/.well-known/apple-app-site-association", include_in_schema=False)
def get_apple_app_site_association(
    active: Runtime = Depends(runtime),
) -> JSONResponse:
    _require_auth_mode(active, "hosted")
    if active.settings.apple_app_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return JSONResponse(
        apple_app_site_association(active.settings.apple_app_id),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/invite", response_class=HTMLResponse, include_in_schema=False)
def get_invitation_landing(
    code: str = "",
    active: Runtime = Depends(runtime),
) -> HTMLResponse:
    _require_auth_mode(active, "hosted")
    if active.settings.invite_base_url is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not invitation_code_is_valid(code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invitation")
    return HTMLResponse(
        invitation_landing_html(code),
        headers={
            "Cache-Control": "no-store, private",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/v1/session/nonce", response_model=NonceResponse)
def create_nonce(active: Runtime = Depends(runtime)) -> NonceResponse:
    _require_auth_mode(active, "hosted")
    return NonceResponse(nonce=active.security.issue_nonce())


@app.post("/v1/session/apple", response_model=SessionResponse)
async def create_apple_session(
    request: AppleSessionRequest,
    active: Runtime = Depends(runtime),
) -> SessionResponse:
    _require_auth_mode(active, "hosted")
    try:
        identity = await asyncio.to_thread(
            active.security.verify_apple_identity,
            request.identity_token,
            request.nonce,
        )
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_error(
                "apple_identity_invalid",
                "Apple sign-in could not be verified. Please try again.",
            ),
        ) from exc
    user_id = active.security.stable_user_id(identity.subject)
    if (
        getattr(active.security, "apple_token_custody_enabled", False)
        and request.authorization_code is None
    ):
        # Do not consume a one-time invitation unless this sign-in has
        # everything required to finish the opt-in Apple revocation custody.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_error(
                "apple_authorization_code_required",
                "Apple did not return the authorization needed to finish sign-in. Please try again.",
            ),
        )
    authorization = active.security.beta_authorization_status(user_id, request.invite_code)
    if authorization is BetaAuthorizationStatus.INVITATION_REQUIRED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_session_error(
                "invitation_required",
                "Enter the invitation code from your PinPoint invitation.",
            ),
        )
    if authorization is BetaAuthorizationStatus.INVITATION_UNAVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_session_error(
                "invitation_unavailable",
                "This invitation is invalid, expired, revoked, or already used.",
            ),
        )
    if authorization is BetaAuthorizationStatus.MEMBERSHIP_DISABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_session_error(
                "membership_disabled",
                "This PinPoint access is disabled. Contact the service operator.",
            ),
        )
    if getattr(active.security, "apple_token_custody_enabled", False):
        try:
            await asyncio.to_thread(
                active.security.capture_apple_refresh_token,
                user_id=user_id,
                apple_subject=identity.subject,
                authorization_code=request.authorization_code,
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_session_error(
                    "apple_token_custody_failed",
                    "Apple sign-in could not finish securely. Please try again.",
                ),
            ) from exc
    try:
        return await _issue_session(active, user_id)
    except AuthenticationError as exc:
        if not active.state.beta_user_is_active(user_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_session_error(
                    "membership_disabled",
                    "This PinPoint access is disabled. Contact the service operator.",
                ),
            ) from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@app.post("/v1/session/local", response_model=SessionResponse)
async def create_local_session(
    activation: LocalSessionRequest,
    http_request: Request,
    active: Runtime = Depends(runtime),
) -> SessionResponse:
    _require_auth_mode(active, "self_hosted")
    if not _is_strict_loopback_request(http_request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_session_error(
                "local_activation_requires_loopback",
                "Local activation is only available directly from this Mac.",
            ),
        )

    verifier = active.security.local_activation_verifier(
        activation.activation_code
    )
    reservation_id = "res_" + secrets.token_urlsafe(18)
    user_id = active.security.local_user_id()
    try:
        reserved = active.state.reserve_local_activation(
            activation_verifier=verifier,
            reservation_id=reservation_id,
            user_id=user_id,
            membership_verifier=active.security.local_membership_verifier(),
        )
    except StateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_session_error(
                "local_activation_unavailable",
                "Local activation is temporarily unavailable.",
            ),
        ) from exc
    if not reserved:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_session_error(
                "local_activation_invalid",
                "This activation code is invalid, expired, or already used.",
            ),
        )

    try:
        response = await _issue_session(active, user_id)
    except (HTTPException, AuthenticationError):
        active.state.release_local_activation(
            activation_verifier=verifier,
            reservation_id=reservation_id,
        )
        raise

    try:
        active.state.consume_local_activation(
            activation_verifier=verifier,
            reservation_id=reservation_id,
        )
    except (LocalActivationUnavailable, StateStoreError) as exc:
        try:
            active.security.revoke_session(response.session_token)
        except AuthenticationError:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_session_error(
                "local_activation_unavailable",
                "Local activation could not finish safely. Create a new activation code.",
            ),
        ) from exc
    return response


@app.post("/v1/session/refresh", response_model=SessionResponse)
async def refresh_session(
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> SessionResponse:
    token = _bearer_token(authorization)
    try:
        user_id = active.security.verify_session(token)
        return await _issue_session(active, user_id, previous_session_token=token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@app.get("/v1/session/status")
def get_session_status(
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict[str, str]:
    _authorized_user(active, authorization)
    return {"status": "active"}


@app.post("/v1/session/logout")
def logout_session(
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict[str, str]:
    token = _bearer_token(authorization)
    try:
        active.security.revoke_session(token)
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return {"status": "signed_out"}


@app.post("/v1/devices/bind")
async def bind_device(
    request: DeviceBindingRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict[str, str]:
    user_id = _authorized_user(active, authorization)
    _validate_device_identity(request.serial_number, request.device_type)

    def perform_lifecycle() -> None:
        with active.state.device_lifecycle_lock(
            user_id=user_id,
            serial_number=request.serial_number,
        ):
            active.state.begin_device_binding(
                user_id=user_id,
                serial_number=request.serial_number,
                device_type=request.device_type,
            )
            try:
                active.plaud.bind_device(
                    user_id=user_id,
                    serial_number=request.serial_number,
                    device_type=request.device_type,
                )
            except PlaudServiceError as exc:
                if exc.status_code == 403:
                    active.state.mark_device_not_owned(
                        user_id=user_id,
                        serial_number=request.serial_number,
                    )
                raise
            active.state.mark_device_bound(
                user_id=user_id,
                serial_number=request.serial_number,
            )

    try:
        await asyncio.to_thread(perform_lifecycle)
    except TesterMembershipUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RecorderClaimConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_lifecycle_error(
                "recorder_claimed_elsewhere",
                "This recorder is already claimed by another PinPoint user.",
            ),
        ) from exc
    except DeviceBindingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_lifecycle_error(
                "recorder_lifecycle_conflict",
                (
                    "This recorder has an unfinished release or mismatched lifecycle. "
                    "Complete the release or contact PinPoint support."
                ),
            ),
        ) from exc
    except PlaudServiceError as exc:
        if exc.status_code == 403:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_lifecycle_error(
                    "recorder_claimed_elsewhere",
                    "This recorder is bound to another account.",
                ),
            ) from exc
        # The request might have reached Plaud. Keep the durable `binding`
        # state so a later retry is idempotent and account deletion unbinds it.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_lifecycle_error(
                "recorder_ownership_unconfirmed",
                "PinPoint could not confirm recorder ownership. Try again.",
            ),
        ) from exc
    except StateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_lifecycle_error(
                "recorder_lifecycle_unavailable",
                "PinPoint could not secure recorder ownership.",
            ),
        ) from exc
    return {"status": "bound"}


@app.post("/v1/devices/unbind")
async def unbind_device(
    request: DeviceBindingRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict[str, str]:
    user_id = _authorized_user(active, authorization)
    _validate_device_identity(request.serial_number, request.device_type)

    def perform_lifecycle() -> None:
        with active.state.device_lifecycle_lock(
            user_id=user_id,
            serial_number=request.serial_number,
        ):
            should_unbind = active.state.begin_device_release(
                user_id=user_id,
                serial_number=request.serial_number,
                device_type=request.device_type,
            )
            if not should_unbind:
                return
            active.plaud.unbind_device(
                user_id=user_id,
                serial_number=request.serial_number,
                device_type=request.device_type,
            )
            active.state.mark_device_released(
                user_id=user_id,
                serial_number=request.serial_number,
            )

    try:
        await asyncio.to_thread(perform_lifecycle)
    except TesterMembershipUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RecorderClaimConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_lifecycle_error(
                "recorder_claimed_elsewhere",
                "This recorder is already claimed by another PinPoint user.",
            ),
        ) from exc
    except DeviceBindingUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_lifecycle_error(
                "recorder_lifecycle_conflict",
                (
                    "This account has no releasable claim for that recorder model. "
                    "Refresh the device state or contact PinPoint support."
                ),
            ),
        ) from exc
    except PlaudServiceError as exc:
        # Unbind is idempotent. Keep release_pending on any ambiguous result;
        # the next explicit release or account deletion can safely retry it.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_lifecycle_error(
                "recorder_release_unconfirmed",
                "PinPoint could not confirm the recorder release. Try again.",
            ),
        ) from exc
    except StateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_lifecycle_error(
                "recorder_lifecycle_unavailable",
                "PinPoint could not secure the recorder release.",
            ),
        ) from exc
    return {"status": "released"}


@app.post("/v1/uploads")
async def create_upload(
    request: UploadCreateRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    file_type = request.file_type.lower().lstrip(".")
    if file_type != "mp3":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Only MP3 audio is accepted")
    if request.file_size > active.settings.max_audio_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Recording exceeds the PinPoint upload limit")
    job_id = secrets.token_urlsafe(24)
    try:
        existing = active.state.reserve_upload_job(
            job_id=job_id,
            user_id=user_id,
            file_size=request.file_size,
            file_type=file_type,
            request_id=request.idempotency_key,
            source_id=request.source_id,
            user_daily_limit=active.settings.user_daily_recording_limit,
            global_daily_limit=active.settings.global_daily_recording_limit,
            user_active_limit=active.settings.user_active_recording_limit,
            user_daily_bytes_limit=active.settings.user_daily_audio_bytes_limit,
            global_daily_bytes_limit=active.settings.global_daily_audio_bytes_limit,
            upload_attempt_lease_seconds=active.settings.upload_attempt_lease_seconds,
        )
    except QuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    except StateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PinPoint could not secure this recording source.",
        ) from exc
    if existing is not None:
        if active.state.completion_is_unknown(existing.job_id, user_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_upload_error(
                    "upload_completion_unconfirmed",
                    "Plaud may already have completed this upload. PinPoint will not repeat it.",
                ),
            )
        if existing.state == "submit_unknown":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_upload_error(
                    "transcription_submission_unconfirmed",
                    "Plaud may already be processing this recording. PinPoint will not submit it twice.",
                ),
            )
        if existing.state == "submitted" and existing.transcription_id:
            return {
                "UploadJobId": existing.job_id,
                "State": existing.state,
                "TranscriptionId": existing.transcription_id,
            }
        if existing.state in {"generating", "uploading", "failed", "expired"} and (
            existing.file_size != request.file_size or existing.file_type != file_type
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_upload_error(
                    "recording_source_conflict",
                    "This recorder source now has different audio metadata. Contact PinPoint support before uploading it.",
                ),
            )
        if existing.state in {"failed", "expired"}:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=_upload_error(
                    "upload_attempt_retired",
                    "This upload attempt ended safely. PinPoint can start a fresh attempt for the same recording.",
                ),
            )
        owns_attempt = active.state.upload_attempt_matches(
            existing.job_id,
            user_id,
            request.idempotency_key,
        )
        stored_plan = active.state.upload_plan_json(existing.job_id, user_id)
        if existing.state != "uploading" or not stored_plan or not owns_attempt:
            return {"UploadJobId": existing.job_id, "State": existing.state}
        try:
            replayed = json.loads(stored_plan)
            plan = _validated_upload_plan(
                replayed,
                file_size=request.file_size,
                allowed_suffixes=active.settings.audio_host_suffixes,
            )
        except (json.JSONDecodeError, TypeError, HTTPException) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PinPoint could not restore this upload job.",
            ) from exc
        return {
            "UploadJobId": existing.job_id,
            "ChunkSize": plan["ChunkSize"],
            "Parts": plan["Parts"],
        }
    try:
        response = await asyncio.to_thread(
            active.plaud.generate_upload,
            user_id=user_id,
            file_size=request.file_size,
            file_type=file_type,
        )
    except PlaudServiceError as exc:
        active.state.fail_upload_job(
            job_id,
            user_id,
            expected_state="generating",
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=_upload_error(
                "upload_attempt_retired",
                "Plaud did not create this upload attempt. PinPoint can safely try again.",
            ),
        ) from exc
    try:
        plan = _validated_upload_plan(
            response,
            file_size=request.file_size,
            allowed_suffixes=active.settings.audio_host_suffixes,
        )
        active.state.attach_upload(
            job_id=job_id,
            user_id=user_id,
            file_id=plan["FileId"],
            plaud_upload_id=plan["UploadId"],
            part_count=len(plan["Parts"]),
            upload_plan_json=json.dumps(plan, separators=(",", ":")),
        )
    except (StateStoreError, HTTPException) as exc:
        active.state.fail_upload_job(
            job_id,
            user_id,
            expected_state="generating",
        )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PinPoint could not secure this upload.",
        ) from exc
    return {
        "UploadJobId": job_id,
        "ChunkSize": plan["ChunkSize"],
        "Parts": plan["Parts"],
    }


@app.post("/v1/uploads/{job_id}/heartbeat")
async def heartbeat_upload(
    job_id: str,
    request: UploadHeartbeatRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict[str, str]:
    user_id = _authorized_user(active, authorization)
    if not _safe_identifier(job_id, maximum=100):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload job")
    try:
        active.state.heartbeat_upload_attempt(
            job_id=job_id,
            user_id=user_id,
            request_id=request.idempotency_key,
            upload_attempt_lease_seconds=active.settings.upload_attempt_lease_seconds,
        )
    except UploadAttemptMismatch as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "upload_attempt_mismatch",
                "This PinPoint session does not own the canonical upload attempt.",
            ),
        ) from exc
    except UploadAttemptRetired as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=_upload_error(
                "upload_attempt_retired",
                "This upload attempt ended safely. PinPoint can start a fresh attempt for the same recording.",
            ),
        ) from exc
    except UploadJobUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except StateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PinPoint could not renew this upload attempt.",
        ) from exc
    return {"state": "uploading"}


async def _submit_uploaded_job(*, job_id: str, user_id: str, active: Runtime) -> dict:
    job = active.state.upload_job(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload job not found")
    if job.state == "submitted" and job.transcription_id:
        return {"transcription_id": job.transcription_id}
    if job.state == "submit_unknown":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_unconfirmed",
                "Plaud may already be processing this recording. PinPoint will not submit it twice.",
            ),
        )
    if job.state == "submitting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_in_progress",
                "Another PinPoint request is submitting this recording. Poll its status.",
            ),
        )
    if job.state != "uploaded" or not job.download_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload job is not ready for transcription recovery",
        )
    try:
        verified_url, actual_size = await asyncio.to_thread(
            active.plaud.probe_audio_size,
            job.download_url,
        )
        _validate_audio_url(verified_url, active.settings.audio_host_suffixes)
    except PlaudServiceError as exc:
        if exc.status_code in {401, 403, 404, 410}:
            # The completed object can be safely uploaded again because no
            # transcription side effect has started yet. Retire with a CAS:
            # another request may have moved this row to ``submitting`` while
            # the remote probe was in flight.
            retired = active.state.fail_upload_job(
                job_id,
                user_id,
                expected_state="uploaded",
            )
            if not retired:
                return _upload_state_after_retirement_race(
                    job_id=job_id,
                    user_id=user_id,
                    active=active,
                    safe_status=status.HTTP_410_GONE,
                    safe_detail=_upload_error(
                        "upload_attempt_retired",
                        "The completed upload link expired. PinPoint can safely upload the local recording again.",
                    ),
                )
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=_upload_error(
                    "upload_attempt_retired",
                    "The completed upload link expired. PinPoint can safely upload the local recording again.",
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="PinPoint could not verify the completed recording size.",
        ) from exc
    if actual_size != job.file_size:
        retired = active.state.fail_upload_job(
            job_id,
            user_id,
            expected_state="uploaded",
        )
        if not retired:
            return _upload_state_after_retirement_race(
                job_id=job_id,
                user_id=user_id,
                active=active,
                safe_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
                safe_detail="Completed recording size did not match its upload job.",
            )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Completed recording size did not match its upload job.",
        )
    try:
        submission_job = active.state.begin_transcription_submission(job_id, user_id)
    except UploadJobUnavailable as exc:
        current = active.state.upload_job(job_id, user_id)
        if current is not None and current.state == "submitted" and current.transcription_id:
            return {"transcription_id": current.transcription_id}
        if current is not None and current.state == "submitting":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_upload_error(
                    "transcription_submission_in_progress",
                    "Another PinPoint request is submitting this recording. Poll its status.",
                ),
            ) from exc
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if submission_job.state == "submitted" and submission_job.transcription_id:
        return {"transcription_id": submission_job.transcription_id}
    try:
        response = await asyncio.to_thread(
            active.plaud.submit_transcription,
            file_url=verified_url,
            params=None,
        )
    except PlaudServiceError as exc:
        # The request may have reached Plaud even when its response was lost.
        # Without a Plaud idempotency key or lookup-by-client-job API, retrying
        # would risk double billing. Freeze the job for operator reconciliation.
        active.state.mark_submission_unknown(job_id, user_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_unconfirmed",
                "Plaud may already be processing this recording. PinPoint will not submit it twice.",
            ),
        ) from exc
    transcription_id = _transcription_identifier(response)
    if transcription_id is None:
        active.state.mark_submission_unknown(job_id, user_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_unconfirmed",
                "Plaud accepted an ambiguous response. PinPoint will not submit the recording twice.",
            ),
        )
    try:
        active.state.mark_upload_submitted(
            job_id=job_id,
            user_id=user_id,
            transcription_id=transcription_id,
            source_url=verified_url,
        )
    except StateStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PinPoint could not secure this transcription.",
        ) from exc
    return response


def _upload_state_after_retirement_race(
    *,
    job_id: str,
    user_id: str,
    active: Runtime,
    safe_status: int,
    safe_detail: object,
) -> dict:
    """Report the state that won a failed safe-retirement compare-and-set."""
    current = active.state.upload_job(job_id, user_id)
    if current is not None and current.state == "submitted" and current.transcription_id:
        return {"transcription_id": current.transcription_id}
    if current is not None and current.state == "submitting":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_in_progress",
                "Another PinPoint request is submitting this recording. Poll its status.",
            ),
        )
    if current is not None and current.state == "submit_unknown":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_unconfirmed",
                "Plaud may already be processing this recording. PinPoint will not submit it twice.",
            ),
        )
    if active.state.completion_is_unknown(job_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "upload_completion_unconfirmed",
                "Plaud may already have completed this upload. PinPoint will not repeat it.",
            ),
        )
    if current is not None and current.state in {"failed", "expired"}:
        raise HTTPException(status_code=safe_status, detail=safe_detail)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Upload job changed while PinPoint was verifying it.",
    )


@app.post("/v1/uploads/{job_id}/complete")
async def complete_upload(
    job_id: str,
    request: UploadCompleteRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    if not _safe_identifier(job_id, maximum=100):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload job")
    parts = [
        {"PartNumber": part.part_number, "ETag": part.etag}
        for part in request.part_list
    ]
    existing = active.state.expire_and_get_upload_job(
        job_id,
        user_id,
        upload_attempt_lease_seconds=active.settings.upload_attempt_lease_seconds,
    )
    if existing is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload job not found")
    if not active.state.upload_attempt_matches(
        job_id,
        user_id,
        request.idempotency_key,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "upload_attempt_mismatch",
                "This PinPoint session does not own the canonical upload attempt.",
            ),
        )
    if existing.state == "submitted" and existing.transcription_id:
        return {"transcription_id": existing.transcription_id}
    if existing.state == "submit_unknown":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_unconfirmed",
                "Plaud may already be processing this recording. PinPoint will not submit it twice.",
            ),
        )
    if active.state.completion_is_unknown(job_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "upload_completion_unconfirmed",
                "Plaud may already have completed this upload. PinPoint will not repeat it.",
            ),
        )
    if existing.state in {"failed", "expired"}:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=_upload_error(
                "upload_attempt_retired",
                "This upload attempt ended safely. PinPoint can start a fresh attempt for the same recording.",
            ),
        )
    if existing.part_count is None or sorted(part["PartNumber"] for part in parts) != list(range(1, existing.part_count + 1)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Uploaded parts do not match this upload job.",
        )
    if any(any(ord(character) < 32 for character in part["ETag"]) for part in parts):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload ETag")

    try:
        job = active.state.begin_upload_completion(
            job_id,
            user_id,
            active.settings.upload_attempt_lease_seconds,
        )
    except UploadJobUnavailable as exc:
        current = active.state.upload_job(job_id, user_id)
        if active.state.completion_is_unknown(job_id, user_id):
            detail = _upload_error(
                "upload_completion_unconfirmed",
                "Plaud may already have completed this upload. PinPoint will not repeat it.",
            )
        elif current is not None and current.state == "submit_unknown":
            detail = _upload_error(
                "transcription_submission_unconfirmed",
                "Plaud may already be processing this recording. PinPoint will not submit it twice.",
            )
        elif current is not None and current.state in {"failed", "expired"}:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=_upload_error(
                    "upload_attempt_retired",
                    "This upload attempt ended safely. PinPoint can start a fresh attempt for the same recording.",
                ),
            ) from exc
        else:
            detail = str(exc)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc

    download_url = job.download_url
    if job.state == "completing":
        if not job.file_id or not job.plaud_upload_id:
            active.state.fail_upload_job(
                job_id,
                user_id,
                expected_state="completing",
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload job is incomplete")
        try:
            completed = await asyncio.to_thread(
                active.plaud.complete_upload,
                user_id=user_id,
                file_id=job.file_id,
                upload_id=job.plaud_upload_id,
                part_list=parts,
                file_type=job.file_type,
            )
            download_url = _download_url(completed)
            _validate_audio_url(download_url, active.settings.audio_host_suffixes)
            active.state.save_completed_upload(job_id, user_id, download_url)
        except (HTTPException, PlaudServiceError, StateStoreError) as exc:
            # From the moment the external complete call starts, an error can
            # no longer prove that Plaud did not finish the multipart object.
            # Freeze this job rather than risk repeating completion.
            try:
                active.state.mark_completion_unknown(job_id, user_id)
            except StateStoreError as state_error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="PinPoint could not secure an uncertain upload completion.",
                ) from state_error
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_upload_error(
                    "upload_completion_unconfirmed",
                    "Plaud may already have completed this upload. PinPoint will not repeat it.",
                ),
            ) from exc

    if not download_url:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Completed upload is unavailable")
    return await _submit_uploaded_job(job_id=job_id, user_id=user_id, active=active)


@app.post("/v1/uploads/{job_id}/resume")
async def resume_upload_submission(
    job_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    if not _safe_identifier(job_id, maximum=100):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload job")
    return await _submit_uploaded_job(job_id=job_id, user_id=user_id, active=active)


@app.post("/v1/uploads/{job_id}/abandon")
async def abandon_upload(
    job_id: str,
    request: UploadAbandonRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict[str, str]:
    user_id = _authorized_user(active, authorization)
    if not _safe_identifier(job_id, maximum=100):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload job")
    if not active.state.upload_attempt_matches(job_id, user_id, request.idempotency_key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "upload_attempt_mismatch",
                "This PinPoint session does not own the canonical upload attempt.",
            ),
        )
    try:
        active.state.abandon_upload_job(job_id, user_id)
    except UploadJobUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"state": "failed"}


@app.get("/v1/uploads/{job_id}")
async def get_upload_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    if not _safe_identifier(job_id, maximum=100):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid upload job")
    job = active.state.expire_and_get_upload_job(
        job_id,
        user_id,
        upload_attempt_lease_seconds=active.settings.upload_attempt_lease_seconds,
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload job not found")
    if active.state.completion_is_unknown(job_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "upload_completion_unconfirmed",
                "Plaud may already have completed this upload. PinPoint will not repeat it.",
            ),
        )
    if job.state == "submit_unknown":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_upload_error(
                "transcription_submission_unconfirmed",
                "Plaud may already be processing this recording. PinPoint will not submit it twice.",
            ),
        )
    if job.state in {"failed", "expired"}:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=_upload_error(
                "upload_attempt_retired",
                "This upload attempt ended safely. PinPoint can start a fresh attempt for the same recording.",
            ),
        )
    result: dict[str, str] = {"state": job.state}
    if job.transcription_id:
        result["transcription_id"] = job.transcription_id
    return result


@app.get("/v1/transcriptions/{transcription_id}")
async def get_transcription(
    transcription_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    if not _safe_identifier(transcription_id, maximum=200):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid transcription id")
    if not active.state.owns_transcription(transcription_id, user_id):
        # Return the same result for an unknown id and another user's id so the
        # endpoint does not disclose which transcription identifiers exist.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcription not found")
    try:
        return await asyncio.to_thread(active.plaud.get_transcription, transcription_id)
    except PlaudServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud transcription is unavailable.") from exc


@app.get("/v1/intelligence/settings")
def get_intelligence_settings(
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    settings = _intelligence_store(active).get_settings(user_id)
    return _settings_response(settings)


@app.put("/v1/intelligence/settings")
def put_intelligence_settings(
    request: IntelligenceSettingsRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    try:
        settings = _intelligence_store(active).put_settings(
            user_id=user_id,
            auto_summary_enabled=request.auto_summary_enabled,
            default_template_id=request.default_template_id,
        )
    except TemplateUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found") from exc
    return _settings_response(settings)


@app.get("/v1/intelligence/templates")
def list_intelligence_templates(
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    return {
        "templates": [
            _template_response(template)
            for template in _intelligence_store(active).list_templates(user_id)
        ]
    }


@app.post("/v1/intelligence/templates", status_code=status.HTTP_201_CREATED)
def create_intelligence_template(
    request: IntelligenceTemplateCreateRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    try:
        template = _intelligence_store(active).create_template(
            user_id=user_id,
            template_id="tpl_" + secrets.token_urlsafe(12),
            name=request.name,
            instructions=request.instructions,
        )
    except TemplateLimitReached as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntelligenceStoreError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return _template_response(template)


@app.patch("/v1/intelligence/templates/{template_id}")
def update_intelligence_template(
    template_id: str,
    request: IntelligenceTemplateUpdateRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    if request.name is None and request.instructions is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="No template changes supplied")
    try:
        template = _intelligence_store(active).update_template(
            user_id=user_id,
            template_id=template_id,
            name=request.name,
            instructions=request.instructions,
        )
    except TemplateUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found") from exc
    except TemplateImmutable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntelligenceStoreError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return _template_response(template)


@app.delete("/v1/intelligence/templates/{template_id}")
def delete_intelligence_template(
    template_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    try:
        outcome = _intelligence_store(active).delete_template(
            user_id=user_id,
            template_id=template_id,
        )
    except TemplateUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found") from exc
    except TemplateImmutable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"status": outcome}


@app.get("/v1/intelligence/vocabulary")
def list_intelligence_vocabulary(
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    return {
        "terms": [
            {"term_id": term.term_id, "term": term.term, "created_at": term.created_at}
            for term in _intelligence_store(active).list_vocabulary(user_id)
        ]
    }


@app.post("/v1/intelligence/vocabulary", status_code=status.HTTP_201_CREATED)
def add_intelligence_vocabulary(
    request: IntelligenceVocabularyRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    try:
        term = _intelligence_store(active).add_vocabulary_term(
            user_id=user_id,
            term=request.term,
        )
    except VocabularyTermExists as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except VocabularyLimitReached as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except InvalidVocabularyTerm as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return {"term_id": term.term_id, "term": term.term, "created_at": term.created_at}


@app.delete("/v1/intelligence/vocabulary/{term_id}")
def delete_intelligence_vocabulary(
    term_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    if not _intelligence_store(active).delete_vocabulary_term(user_id=user_id, term_id=term_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vocabulary term not found")
    return {"status": "deleted"}


@app.get("/v1/transcriptions/{transcription_id}/summary")
def get_intelligence_summary(
    transcription_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    _require_owned_transcription(active, transcription_id, user_id)
    summary = _intelligence_store(active).canonical_summary(transcription_id, user_id)
    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary not found")
    return _summary_response(summary)


@app.post("/v1/transcriptions/{transcription_id}/summary-jobs")
async def create_intelligence_summary_job(
    transcription_id: str,
    request: SummaryJobCreateRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
    background_tasks: BackgroundTasks = None,
) -> dict:
    user_id = _authorized_user(active, authorization)
    _require_owned_transcription(active, transcription_id, user_id)
    store = _intelligence_store(active)
    settings = store.get_settings(user_id)
    template_id = request.template_id or settings.default_template_id or DEFAULT_TEMPLATE_ID
    template = store.usable_template(user_id, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    canonical_before = store.canonical_summary(transcription_id, user_id)
    if request.mode == "improve" and canonical_before is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Generate a summary before improving its wording")
    marked_moments = tuple(sorted(set(request.marked_moments)))
    fingerprint_document = {
        "transcription_id": transcription_id,
        "template_id": template_id,
        "mode": request.mode,
    }
    # Omitting the empty field preserves fingerprints made by clients released
    # before marked moments were supported.
    if marked_moments:
        fingerprint_document["marked_moments"] = list(marked_moments)
    if request.mode == "improve":
        fingerprint_document["base_version"] = canonical_before.version if canonical_before else 0
        fingerprint_document["base_hash"] = canonical_before.summary_hash if canonical_before else None
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    job_id = "sum_" + secrets.token_urlsafe(15)
    try:
        canonical_job_id, existing = store.reserve_summary_job(
            job_id=job_id,
            user_id=user_id,
            transcription_id=transcription_id,
            template_id=template_id,
            request_id=request.idempotency_key,
            request_fingerprint=fingerprint,
            mode=request.mode,
            user_active_limit=active.settings.intelligence_user_active_generation_limit,
            user_daily_limit=active.settings.intelligence_user_daily_generation_limit,
        )
    except SummaryJobConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (SummaryConcurrencyLimitReached, SummaryDailyLimitReached) as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    if existing is not None:
        return _summary_job_response(existing, mode=request.mode)
    if active.summary_provider is None:
        store.fail_summary_job(job_id=canonical_job_id, user_id=user_id, reason="provider_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PinPoint Intelligence is not configured yet",
        )
    execution_arguments = {
        "active": active,
        "store": store,
        "job_id": canonical_job_id,
        "user_id": user_id,
        "transcription_id": transcription_id,
        "template": template,
        "mode": request.mode,
        "marked_moments": marked_moments,
        "canonical_before": canonical_before,
    }
    if request.execution == "background":
        if background_tasks is None:
            store.fail_summary_job(
                job_id=canonical_job_id,
                user_id=user_id,
                reason="background_executor_unavailable",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Background summary execution is unavailable",
            )
        background_tasks.add_task(
            _run_intelligence_summary_job_in_background,
            **execution_arguments,
        )
        job = store.summary_job(canonical_job_id, user_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Summary job could not be reserved",
            )
        return _summary_job_response(job, mode=request.mode)
    return await _execute_intelligence_summary_job(**execution_arguments)


async def _execute_intelligence_summary_job(
    *,
    active: Runtime,
    store: IntelligenceStore,
    job_id: str,
    user_id: str,
    transcription_id: str,
    template,
    mode: str,
    marked_moments: tuple[int, ...],
    canonical_before,
) -> dict:
    try:
        transcription = await asyncio.to_thread(active.plaud.get_transcription, transcription_id)
    except PlaudServiceError as exc:
        store.fail_summary_job(job_id=job_id, user_id=user_id, reason="transcript_unavailable")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud transcription is unavailable") from exc
    transcript_text = _completed_transcript_text(transcription)
    if transcript_text is None:
        store.fail_summary_job(job_id=job_id, user_id=user_id, reason="transcript_not_ready")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Transcription is not ready")
    transcript_duration = _transcript_duration_seconds(transcription)
    if transcript_duration is not None and any(
        offset > math.ceil(transcript_duration) for offset in marked_moments
    ):
        store.fail_summary_job(job_id=job_id, user_id=user_id, reason="marked_moment_out_of_range")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A marked moment is outside the recording duration",
        )
    vocabulary = tuple(term.term for term in store.list_vocabulary(user_id))
    current_summary = canonical_before.summary_text if mode == "improve" and canonical_before else None
    try:
        proposal = await asyncio.to_thread(
            active.summary_provider.generate_summary,
            transcript=transcript_text,
            template_name=template.name,
            template_instructions=template.instructions,
            vocabulary=vocabulary,
            marked_moments=marked_moments,
            current_summary=current_summary,
        )
    except SummaryProviderAmbiguous as exc:
        store.mark_summary_job_unknown(job_id=job_id, user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The summary result could not be confirmed. PinPoint will not repeat it automatically.",
        ) from exc
    except SummaryProviderRejected as exc:
        store.fail_summary_job(job_id=job_id, user_id=user_id, reason="provider_rejected")
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Summary generation failed") from exc
    try:
        _require_owned_transcription(active, transcription_id, user_id)
    except HTTPException:
        store.discard_generating_job(job_id=job_id, user_id=user_id)
        raise
    canonical_after = store.canonical_summary(transcription_id, user_id)
    canonical_changed = (
        (canonical_before is None and canonical_after is not None)
        or (
            canonical_before is not None
            and (
                canonical_after is None
                or canonical_after.version != canonical_before.version
                or canonical_after.summary_hash != canonical_before.summary_hash
            )
        )
    )
    if canonical_changed:
        store.discard_generating_job(job_id=job_id, user_id=user_id)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The summary changed while this suggestion was being prepared")
    try:
        job = store.save_summary_proposal(
            job_id=job_id,
            user_id=user_id,
            summary_text=proposal.summary_text,
            proposed_terms=proposal.proposed_terms,
            expected_base_version=canonical_before.version if canonical_before else 0,
        )
        if mode == "generate":
            summary, _ = store.approve_summary_job(
                job_id=job.job_id,
                user_id=user_id,
                proposal_hash=job.proposal_hash or "",
                base_version=job.base_summary_version or 0,
                accepted_terms=(),
            )
            approved = store.summary_job(job.job_id, user_id)
            response = _summary_job_response(approved or job, mode=mode)
            response["summary"] = _summary_response(summary)
            return response
        return _summary_job_response(job, mode=mode)
    except StaleSummaryApproval as exc:
        try:
            store.discard_generating_job(job_id=job_id, user_id=user_id)
        except SummaryJobUnavailable:
            try:
                store.discard_summary_job(job_id=job_id, user_id=user_id)
            except (SummaryJobUnavailable, SummaryJobConflict):
                pass
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (SummaryJobUnavailable, SummaryJobConflict) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


async def _run_intelligence_summary_job_in_background(**execution_arguments) -> None:
    store: IntelligenceStore = execution_arguments["store"]
    job_id = execution_arguments["job_id"]
    user_id = execution_arguments["user_id"]
    try:
        await _execute_intelligence_summary_job(**execution_arguments)
    except HTTPException:
        # Expected failures are already persisted by the executor. The client
        # learns the terminal state through its authenticated polling request.
        return
    except Exception:
        # The failure may have happened after the external model accepted the
        # request, so an unexpected background crash is conservatively frozen
        # as unknown and is never auto-repeated.
        try:
            store.mark_summary_job_unknown(job_id=job_id, user_id=user_id)
        except SummaryJobUnavailable:
            pass


@app.get("/v1/intelligence/summary-jobs/by-request/{request_id}")
def get_intelligence_summary_job_by_request(
    request_id: Annotated[
        str,
        APIPath(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$"),
    ],
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    job = _intelligence_store(active).summary_job_for_request(request_id, user_id)
    if job is None or not active.state.owns_transcription(job.transcription_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary job not found")
    return _summary_job_response(job)


@app.get("/v1/intelligence/summary-jobs/{job_id}")
def get_intelligence_summary_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    job = _intelligence_store(active).summary_job(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary job not found")
    return _summary_job_response(job)


@app.post("/v1/intelligence/summary-jobs/{job_id}/approve")
def approve_intelligence_summary_job(
    job_id: str,
    request: SummaryApprovalRequest,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    store = _intelligence_store(active)
    job = store.summary_job(job_id, user_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary job not found")
    if not active.state.owns_transcription(job.transcription_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary job not found")
    try:
        summary, added_terms = store.approve_summary_job(
            job_id=job_id,
            user_id=user_id,
            proposal_hash=request.proposal_hash,
            base_version=request.base_version,
            accepted_terms=tuple(request.accepted_terms),
        )
    except SummaryJobUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary job not found") from exc
    except (SummaryJobConflict, StaleSummaryApproval) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (InvalidVocabularyTerm, VocabularyLimitReached) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return {"summary": _summary_response(summary), "added_terms": list(added_terms)}


@app.post("/v1/intelligence/summary-jobs/{job_id}/discard")
def discard_intelligence_summary_job(
    job_id: str,
    authorization: str | None = Header(default=None),
    active: Runtime = Depends(runtime),
) -> dict:
    user_id = _authorized_user(active, authorization)
    try:
        _intelligence_store(active).discard_summary_job(job_id=job_id, user_id=user_id)
    except SummaryJobUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Summary job not found") from exc
    except SummaryJobConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"status": "discarded"}


def _intelligence_store(active: Runtime) -> IntelligenceStore:
    if active.intelligence is None:
        active.intelligence = IntelligenceStore(active.settings.state_db_path)
    return active.intelligence


def _require_owned_transcription(active: Runtime, transcription_id: str, user_id: str) -> None:
    if not _safe_identifier(transcription_id, maximum=200):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid transcription id",
        )
    if not active.state.owns_transcription(transcription_id, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcription not found")


def _completed_transcript_text(document: dict) -> str | None:
    status_value = document.get("status")
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    task_status = data.get("task_status")
    status_text = str(status_value if status_value is not None else task_status or "").upper()
    if status_text != "SUCCESS":
        return None
    results = data.get("results")
    if isinstance(results, list):
        paragraphs = [
            rendered
            for item in results
            if isinstance(item, dict)
            for rendered in [_structured_transcript_turn(item)]
            if rendered is not None
        ]
        if paragraphs:
            return "\n\n".join(paragraphs)
    direct = data.get("text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return None


def _structured_transcript_turn(item: dict) -> str | None:
    raw_text = item.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    text = raw_text.strip()
    speaker = _first_metadata_value(
        item,
        ("speaker_name", "speaker_label", "speaker"),
    )
    if speaker is None:
        speaker_id = _first_metadata_value(item, ("speaker_id", "speakerId"))
        if speaker_id is not None:
            speaker = (
                speaker_id
                if speaker_id.casefold().startswith("speaker")
                else f"Speaker {speaker_id}"
            )
    timestamp = _transcript_timestamp(item)
    labels: list[str] = []
    if timestamp is not None:
        labels.append(timestamp)
    if speaker is not None:
        labels.append(speaker)
    return (f"[{' · '.join(labels)}] " if labels else "") + text


def _first_metadata_value(document: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = document.get(key)
        if value is None or isinstance(value, (dict, list, tuple, bool)):
            continue
        cleaned = " ".join(str(value).split())
        if cleaned and len(cleaned) <= 80 and cleaned.isprintable():
            return cleaned
    return None


def _transcript_timestamp(item: dict) -> str | None:
    for key in ("start_time_ms", "start_ms", "timestamp_ms"):
        seconds = _nonnegative_number(item.get(key))
        if seconds is not None:
            return _format_transcript_time(seconds / 1_000)
    for key in ("start_time", "start", "timestamp", "offset", "startTime"):
        value = item.get(key)
        seconds = _nonnegative_number(value)
        if seconds is not None:
            return _format_transcript_time(seconds)
        if isinstance(value, str):
            cleaned = " ".join(value.split())
            if cleaned and len(cleaned) <= 32 and cleaned.isprintable():
                return cleaned
    return None


def _format_transcript_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1_000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, fraction = divmod(remainder, 1_000)
    prefix = f"{hours}:{minutes:02d}:{whole_seconds:02d}" if hours else f"{minutes}:{whole_seconds:02d}"
    if fraction:
        return prefix + f".{fraction:03d}".rstrip("0")
    return prefix


def _transcript_duration_seconds(document: dict) -> float | None:
    data = document.get("data") if isinstance(document.get("data"), dict) else {}
    for container in (data, document):
        for key in ("duration_seconds", "audio_duration_seconds"):
            value = _nonnegative_number(container.get(key))
            if value is not None:
                return value
        for key in ("duration_ms", "audio_duration_ms"):
            value = _nonnegative_number(container.get(key))
            if value is not None:
                return value / 1_000
        for key in ("duration", "audio_duration"):
            value = _nonnegative_number(container.get(key))
            if value is not None:
                return value
    results = data.get("results")
    if isinstance(results, list):
        endings: list[float] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            for key, divisor in (
                ("end_time_ms", 1_000),
                ("end_ms", 1_000),
                ("end_time", 1),
                ("end", 1),
                ("endTime", 1),
            ):
                value = _nonnegative_number(item.get(key))
                if value is not None:
                    endings.append(value / divisor)
                    break
        if endings:
            return max(endings)
    return None


def _nonnegative_number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _settings_response(value) -> dict:
    return {
        "auto_summary_enabled": value.auto_summary_enabled,
        "default_template_id": value.default_template_id,
    }


def _template_response(value) -> dict:
    return {
        "template_id": value.template_id,
        "name": value.name,
        "instructions": value.instructions,
        "builtin": value.builtin,
        "state": value.state,
        "version": 1,
    }


def _summary_response(value) -> dict:
    return {
        "transcription_id": value.transcription_id,
        "template_id": value.template_id,
        "summary_text": value.summary_text,
        "version": value.version,
        "summary_hash": value.summary_hash,
        "updated_at": value.updated_at,
    }


def _summary_job_response(value, *, mode: str | None = None) -> dict:
    return {
        "job_id": value.job_id,
        "transcription_id": value.transcription_id,
        "template_id": value.template_id,
        "mode": mode or value.mode,
        "state": value.state,
        "base_summary_version": value.base_summary_version,
        "proposed_summary": value.proposed_summary,
        "proposal_hash": value.proposal_hash,
        "proposed_terms": list(value.proposed_terms),
        "failure_reason": value.failure_reason,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


async def _issue_session(
    active: Runtime,
    user_id: str,
    previous_session_token: str | None = None,
) -> SessionResponse:
    try:
        plaud_token: PlaudUserToken = await asyncio.to_thread(active.plaud.issue_user_token, user_id)
    except PlaudServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Plaud Partner service is temporarily unavailable.",
        ) from exc
    if previous_session_token:
        session_token, session_expiry = active.security.refresh_session(previous_session_token)
    else:
        session_token, session_expiry = active.security.issue_session(user_id)
    return SessionResponse(
        session_token=session_token,
        plaud_user_access_token=plaud_token.access_token,
        user_id=user_id,
        plaud_domain=active.settings.plaud_api_domain,
        session_expires_at=session_expiry,
        plaud_token_expires_at=plaud_token.expires_at,
        deployment_mode=getattr(active.settings, "auth_mode", "hosted"),
    )


def _require_auth_mode(active: Runtime, expected: AuthenticationMode) -> None:
    configured = getattr(getattr(active, "settings", None), "auth_mode", "hosted")
    if configured != expected:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _is_strict_loopback_request(request: Request) -> bool:
    """Reject proxy-derived or remotely routed activation attempts.

    Self-hosted activation is a localhost capability, not an alternate public
    login method. Both the transport peer and Host header must name loopback,
    and proxy forwarding metadata is never trusted for this route.
    """
    if any(
        name.lower() == "forwarded" or name.lower().startswith("x-forwarded-")
        for name in request.headers.keys()
    ):
        return False
    client = request.client
    if client is None:
        return False
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not address.is_loopback:
        return False

    host = request.headers.get("host", "").strip().lower()
    if re.fullmatch(r"localhost(?::[0-9]{1,5})?", host):
        return _valid_host_port(host)
    if re.fullmatch(r"127\.0\.0\.1(?::[0-9]{1,5})?", host):
        return _valid_host_port(host)
    if re.fullmatch(r"\[::1\](?::[0-9]{1,5})?", host):
        return _valid_host_port(host)
    return False


def _valid_host_port(host: str) -> bool:
    if host in {"localhost", "127.0.0.1", "[::1]"}:
        return True
    if host.startswith("[::1]:"):
        port_text = host[len("[::1]:") :]
    elif ":" in host:
        port_text = host.rsplit(":", 1)[1]
    else:
        return False
    try:
        return 1 <= int(port_text) <= 65_535
    except ValueError:
        return False


def _bearer_token(value: str | None) -> str:
    if not value or not value.startswith("Bearer ") or len(value) <= 7:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing PinPoint session.")
    return value[7:]


def _authorized_user(active: Runtime, authorization: str | None) -> str:
    try:
        return active.security.verify_session(_bearer_token(authorization))
    except AuthenticationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def _validate_device_identity(serial_number: str, device_type: str) -> None:
    expected_prefix = {"notepins": "882", "notepro": "881"}[device_type]
    if not serial_number.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Recorder serial number does not match its supported Plaud model.",
        )


def _validate_audio_url(value: str, allowed_suffixes: tuple[str, ...]) -> None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(host == suffix or host.endswith("." + suffix) for suffix in allowed_suffixes)
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid audio URL") from exc
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or bool(parsed.fragment)
        or not parsed.path
        or not allowed
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Audio URL is not from an approved Plaud storage host.",
        )


def _transcription_identifier(payload: dict) -> str | None:
    direct = payload.get("transcription_id")
    if isinstance(direct, str) and direct:
        return direct
    data = payload.get("data")
    if isinstance(data, dict):
        task_id = data.get("task_id")
        if isinstance(task_id, str) and task_id:
            return task_id
    return None


def _validated_upload_plan(
    payload: dict,
    *,
    file_size: int,
    allowed_suffixes: tuple[str, ...],
) -> dict:
    file_id = payload.get("FileId")
    upload_id = payload.get("UploadId")
    raw_chunk = payload.get("ChunkSize")
    chunk_size = raw_chunk if isinstance(raw_chunk, int) and raw_chunk > 0 else 5 * 1024 * 1024
    parts = payload.get("Parts")
    expected_count = math.ceil(file_size / chunk_size)
    if (
        not isinstance(file_id, str)
        or not file_id
        or not isinstance(upload_id, str)
        or not upload_id
        or not isinstance(parts, list)
        or len(parts) != expected_count
        or expected_count > 10_000
    ):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud returned an invalid upload plan.")
    safe_parts: list[dict] = []
    numbers: list[int] = []
    for part in parts:
        if not isinstance(part, dict):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud returned an invalid upload plan.")
        number = part.get("PartNumber")
        url = part.get("PresignedUrl")
        if not isinstance(number, int) or not isinstance(url, str):
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud returned an invalid upload plan.")
        _validate_audio_url(url, allowed_suffixes)
        numbers.append(number)
        safe_parts.append({"PartNumber": number, "PresignedUrl": url})
    if sorted(numbers) != list(range(1, expected_count + 1)):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud returned an invalid upload plan.")
    return {"FileId": file_id, "UploadId": upload_id, "ChunkSize": chunk_size, "Parts": safe_parts}


def _download_url(payload: dict) -> str:
    value = payload.get("DownloadUrl")
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Plaud returned no completed audio URL.")
    return value


def _safe_identifier(value: str, *, maximum: int) -> bool:
    return bool(value) and len(value) <= maximum and value.replace("-", "").replace("_", "").isalnum()
