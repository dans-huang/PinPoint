from __future__ import annotations

import base64
import binascii
import json
import threading
import time
import urllib.error
import urllib.request
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class PlaudServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PlaudUserToken:
    access_token: str
    expires_at: datetime


class PlaudPartnerClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        api_key: str,
        domain: str,
        user_token_ttl_seconds: int = 60 * 60,
    ) -> None:
        self.client_id = client_id
        self._client_secret = client_secret
        self._api_key = api_key
        self.domain = domain
        self._user_token_ttl_seconds = user_token_ttl_seconds
        self._partner_token: str | None = None
        self._partner_expiry = 0.0
        self._lock = threading.Lock()
        self._user_tokens: dict[str, PlaudUserToken] = {}
        self._user_lock = threading.Lock()

    def issue_user_token(self, user_id: str) -> PlaudUserToken:
        with self._user_lock:
            cached = self._user_tokens.get(user_id)
            if cached and cached.expires_at.timestamp() - time.time() > 5 * 60:
                return cached
        partner_token = self._get_partner_token()
        payload = self._post_json(
            f"https://{self.domain}/developer/api/open/partner/users/access-token",
            authorization=f"Bearer {partner_token}",
            body={"user_id": user_id, "expires_in": self._user_token_ttl_seconds},
        )
        token = self._extract_token(payload)
        expiry = self._validated_user_token(token, expected_user_id=user_id)
        result = PlaudUserToken(access_token=token, expires_at=expiry)
        with self._user_lock:
            self._user_tokens[user_id] = result
        return result

    def generate_upload(self, *, user_id: str, file_size: int, file_type: str) -> dict[str, Any]:
        user_token = self.issue_user_token(user_id).access_token
        return self._post_json(
            f"https://{self.domain}/developer/api/open/partner/files/upload/generate-presigned-urls",
            authorization=f"Bearer {user_token}",
            body={"filesize": file_size, "filetype": file_type},
        )

    def bind_device(self, *, user_id: str, serial_number: str, device_type: str) -> None:
        self._device_binding_request(
            action="bind",
            user_id=user_id,
            serial_number=serial_number,
            device_type=device_type,
        )

    def unbind_device(self, *, user_id: str, serial_number: str, device_type: str) -> None:
        self._device_binding_request(
            action="unbind",
            user_id=user_id,
            serial_number=serial_number,
            device_type=device_type,
        )

    def complete_upload(
        self,
        *,
        user_id: str,
        file_id: str,
        upload_id: str,
        part_list: list[dict[str, Any]],
        file_type: str,
    ) -> dict[str, Any]:
        user_token = self.issue_user_token(user_id).access_token
        return self._post_json(
            f"https://{self.domain}/developer/api/open/partner/files/upload/complete-upload",
            authorization=f"Bearer {user_token}",
            body={
                "file_id": file_id,
                "upload_id": upload_id,
                "part_list": part_list,
                "filetype": file_type,
            },
        )

    def probe_audio_size(self, file_url: str) -> tuple[str, int]:
        request = urllib.request.Request(
            file_url,
            method="GET",
            headers={
                "Accept": "application/octet-stream",
                "Range": "bytes=0-4095",
                "User-Agent": "PinPoint-Backend/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                final_url = response.geturl()
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", content_range.strip(), re.IGNORECASE)
                if match:
                    size = int(match.group(1))
                else:
                    content_length = response.headers.get("Content-Length")
                    size = int(content_length) if content_length is not None else 0
                # Read only the leading bytes, then close. Besides verifying
                # the object size, this rejects an obviously non-MP3 payload
                # before shared transcription quota is spent.
                header = response.read(4096)
        except urllib.error.HTTPError as exc:
            raise PlaudServiceError(
                "Plaud audio size verification failed",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, ValueError) as exc:
            raise PlaudServiceError("Plaud audio size verification failed") from exc
        if size <= 0:
            raise PlaudServiceError("Plaud audio response had no valid size")
        has_id3 = header.startswith(b"ID3")
        has_frame_sync = any(
            header[index] == 0xFF and header[index + 1] & 0xE0 == 0xE0
            for index in range(max(0, len(header) - 1))
        )
        if not has_id3 and not has_frame_sync:
            raise PlaudServiceError("Completed upload was not a recognizable MP3")
        return final_url, size

    def submit_transcription(self, *, file_url: str, params: dict[str, Any] | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "file_url": file_url,
            "params": params or {
                "transcribe": {"language": "auto", "model": "plaud-fast-whisper"},
                "vad": {"decode_silence": False},
                "diarization": {"enabled": True, "return_embedding": False},
            },
        }
        return self._request_json(
            f"https://{self.domain}/developer/api/open/partner/ai/transcriptions/",
            method="POST",
            headers=self._transcription_headers(),
            body=body,
        )

    def get_transcription(self, transcription_id: str) -> dict[str, Any]:
        return self._request_json(
            f"https://{self.domain}/developer/api/open/partner/ai/transcriptions/{transcription_id}",
            method="GET",
            headers=self._transcription_headers(),
            body=None,
        )

    def _device_binding_request(
        self,
        *,
        action: str,
        user_id: str,
        serial_number: str,
        device_type: str,
    ) -> None:
        # The official lifecycle declares same-owner bind and unbind of an
        # already-unbound recorder idempotent. Retry once only when Plaud
        # explicitly rejects the cached user token; transport ambiguity is
        # left to the durable caller ledger and its later idempotent retry.
        for attempt in range(2):
            user_token = self.issue_user_token(user_id).access_token
            try:
                self._post_status(
                    f"https://{self.domain}/developer/api/open/partner/sdk/{action}",
                    authorization=f"Bearer {user_token}",
                    body={"type": device_type, "sn": serial_number},
                )
                return
            except PlaudServiceError as exc:
                if exc.status_code == 401 and attempt == 0:
                    with self._user_lock:
                        self._user_tokens.pop(user_id, None)
                    continue
                raise
        raise PlaudServiceError("Plaud device lifecycle request failed")

    def _transcription_headers(self) -> dict[str, str]:
        return {
            "X-Client-Id": self.client_id,
            "X-Client-Api-Key": self._api_key,
        }

    def _get_partner_token(self) -> str:
        with self._lock:
            if self._partner_token and self._partner_expiry - time.time() > 60:
                return self._partner_token
            basic = base64.b64encode(f"{self.client_id}:{self._client_secret}".encode()).decode()
            payload = self._post_json(
                f"https://{self.domain}/developer/api/oauth/partner/access-token",
                authorization=f"Basic {basic}",
                body=None,
            )
            token = self._extract_token(payload)
            self._partner_token = token
            try:
                self._partner_expiry = self._jwt_expiry(token).timestamp()
            except PlaudServiceError:
                self._partner_expiry = time.time() + 30 * 60
            return token

    def _post_json(self, url: str, *, authorization: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return self._request_json(
            url,
            method="POST",
            headers={"Authorization": authorization},
            body=body,
        )

    def _post_status(self, url: str, *, authorization: str, body: dict[str, Any]) -> None:
        """Perform a lifecycle POST whose documented contract is HTTP status only."""
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": authorization,
                "User-Agent": "PinPoint-Backend/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                # Drain only a bounded amount so the connection can be reused;
                # the official bind/unbind contract does not define a payload.
                response.read(4096)
        except urllib.error.HTTPError as exc:
            raise PlaudServiceError(
                "Plaud device lifecycle request failed",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise PlaudServiceError("Plaud device lifecycle request failed") from exc

    def _request_json(
        self,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = None if method == "GET" else (b"" if body is None else json.dumps(body).encode("utf-8"))
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "PinPoint-Backend/0.1",
            **headers,
        }
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=request_headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            raise PlaudServiceError(
                "Plaud Partner service request failed",
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise PlaudServiceError("Plaud Partner service request failed") from exc
        if not isinstance(payload, dict):
            raise PlaudServiceError("Plaud Partner service returned invalid JSON")
        return payload

    @staticmethod
    def _extract_token(payload: dict[str, Any]) -> str:
        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = payload.get("access_token") or nested.get("access_token")
        if not isinstance(token, str) or not token:
            raise PlaudServiceError("Plaud Partner response did not include an access token")
        return token

    @staticmethod
    def _jwt_claims(token: str) -> dict[str, Any]:
        try:
            parts = token.strip().split(".")
            if len(parts) != 3:
                raise ValueError("JWT must have three parts")
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.b64decode(payload, altchars=b"-_", validate=True))
            if not isinstance(claims, dict):
                raise ValueError("JWT payload is not an object")
            return claims
        except (
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            binascii.Error,
            UnicodeDecodeError,
        ) as exc:
            raise PlaudServiceError("Plaud access token had an invalid payload") from exc

    @classmethod
    def _jwt_expiry(cls, token: str) -> datetime:
        try:
            expiry = int(cls._jwt_claims(token)["exp"])
            return datetime.fromtimestamp(expiry, timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise PlaudServiceError("Plaud access token did not contain a valid expiry") from exc

    def _validated_user_token(self, token: str, *, expected_user_id: str) -> datetime:
        claims = self._jwt_claims(token)
        subject = claims.get("sub")
        user_id = claims.get("user_id", subject)
        client_id = claims.get("client_id")
        if not isinstance(subject, str) or not subject:
            raise PlaudServiceError("Plaud user token had no handshake subject")
        if not isinstance(user_id, str) or user_id != expected_user_id:
            raise PlaudServiceError("Plaud user token did not match the requested user")
        if not isinstance(client_id, str) or client_id != self.client_id:
            raise PlaudServiceError("Plaud user token did not match this partner client")
        expiry = self._jwt_expiry(token)
        if expiry.timestamp() <= time.time() + 60:
            raise PlaudServiceError("Plaud user token was already expired")
        if expiry.timestamp() > time.time() + self._user_token_ttl_seconds + 5 * 60:
            raise PlaudServiceError("Plaud user token expiry exceeded the requested lifetime")
        return expiry
