"""Secret-safe OAuth token validation and encrypted SSM persistence."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

import httpx


class OAuthTokenError(RuntimeError):
    """A public-safe token lifecycle failure."""


@dataclass(frozen=True)
class TokenBundle:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_type: str
    scopes: frozenset[str]
    expires_at: int
    token_endpoint: str = field(repr=False)
    client_id: str = field(repr=False)
    resource: str = field(repr=False)

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "token_type": self.token_type,
                "scopes": sorted(self.scopes),
                "expires_at": self.expires_at,
                "token_endpoint": self.token_endpoint,
                "client_id": self.client_id,
                "resource": self.resource,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> TokenBundle:
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, Mapping):
                raise ValueError
            scopes = parsed["scopes"]
            if not isinstance(scopes, list) or not all(isinstance(v, str) for v in scopes):
                raise ValueError
            string_fields = (
                "access_token",
                "refresh_token",
                "token_type",
                "token_endpoint",
                "client_id",
                "resource",
            )
            if any(
                not isinstance(parsed.get(name), str) or not parsed[name]
                for name in string_fields
            ):
                raise ValueError
            expires_at = parsed["expires_at"]
            if isinstance(expires_at, bool) or not isinstance(expires_at, int):
                raise ValueError
            bundle = cls(
                access_token=parsed["access_token"],
                refresh_token=parsed["refresh_token"],
                token_type=parsed["token_type"],
                scopes=frozenset(scopes),
                expires_at=expires_at,
                token_endpoint=parsed["token_endpoint"],
                client_id=parsed["client_id"],
                resource=parsed["resource"],
            )
            validate_stored_bundle(bundle)
            return bundle
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OAuthTokenError("oauth_token_bundle_invalid") from exc


def validate_stored_bundle(bundle: TokenBundle, *, now: int | None = None) -> None:
    current = int(time.time()) if now is None else now
    token_url = urlparse(bundle.token_endpoint)
    resource_url = urlparse(bundle.resource)
    valid = (
        bundle.token_type.lower() == "bearer"
        and bundle.scopes == frozenset({"mcp:read"})
        and token_url.scheme == "https"
        and token_url.hostname == "cockroachlabs.cloud"
        and token_url.path == "/mcp/oauth/token"
        and not token_url.username
        and not token_url.password
        and resource_url.scheme == "https"
        and resource_url.hostname == "cockroachlabs.cloud"
        and resource_url.path.rstrip("/") == "/mcp"
        and not resource_url.username
        and not resource_url.password
        and bundle.expires_at <= current + 86_400
    )
    if not valid:
        raise OAuthTokenError("oauth_token_bundle_invalid")


def validated_token_bundle(
    response: Mapping[str, Any],
    *,
    requested_scopes: frozenset[str],
    token_endpoint: str,
    client_id: str,
    resource: str,
    prior_refresh_token: str | None = None,
    now: int | None = None,
) -> TokenBundle:
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthTokenError("oauth_access_token_missing")
    if str(response.get("token_type", "")).lower() != "bearer":
        raise OAuthTokenError("oauth_token_type_invalid")
    try:
        lifetime = int(response.get("expires_in"))
    except (TypeError, ValueError) as exc:
        raise OAuthTokenError("oauth_token_lifetime_invalid") from exc
    if isinstance(response.get("expires_in"), bool) or not 0 < lifetime <= 86_400:
        raise OAuthTokenError("oauth_token_lifetime_invalid")
    scope_value = response.get("scope")
    if scope_value is not None and not isinstance(scope_value, str):
        raise OAuthTokenError("oauth_token_scope_invalid")
    scopes = (
        frozenset(str(scope_value).split())
        if isinstance(scope_value, str) and scope_value.strip()
        else requested_scopes
    )
    if "mcp:read" not in scopes or "mcp:write" in scopes or not scopes <= requested_scopes:
        raise OAuthTokenError("oauth_token_scope_invalid")
    returned_refresh = response.get("refresh_token")
    if returned_refresh is not None and (
        not isinstance(returned_refresh, str) or not returned_refresh
    ):
        raise OAuthTokenError("oauth_refresh_token_invalid")
    refresh_token = returned_refresh or prior_refresh_token
    if not refresh_token:
        raise OAuthTokenError("oauth_refresh_token_missing")
    current = int(time.time()) if now is None else now
    return TokenBundle(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        scopes=scopes,
        expires_at=current + lifetime,
        token_endpoint=token_endpoint,
        client_id=client_id,
        resource=resource,
    )


class SSMClient(Protocol):
    def put_parameter(self, **kwargs: Any) -> Any: ...

    def get_parameter(self, **kwargs: Any) -> Any: ...


class DynamoDBClient(Protocol):
    def put_item(self, **kwargs: Any) -> Any: ...

    def delete_item(self, **kwargs: Any) -> Any: ...


class RefreshLease(Protocol):
    """A short-lived, cross-process lock for one OAuth token bundle."""

    def acquire(self) -> str | None: ...

    def release(self, owner: str) -> bool: ...


class DynamoDBRefreshLease:
    """DynamoDB conditional lease for serializing OAuth refresh-token rotation."""

    def __init__(
        self,
        client: DynamoDBClient,
        *,
        table_name: str,
        bundle_key: str,
        lease_seconds: int = 60,
        clock=time.time,
        owner_factory=lambda: secrets.token_urlsafe(32),
    ) -> None:
        if not table_name or not bundle_key or not 0 < lease_seconds <= 300:
            raise OAuthTokenError("oauth_refresh_lease_invalid")
        self._client = client
        self._table_name = table_name
        self._bundle_key = bundle_key
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._owner_factory = owner_factory

    def acquire(self) -> str | None:
        owner = self._owner_factory()
        if not isinstance(owner, str) or not owner:
            raise OAuthTokenError("oauth_refresh_lease_invalid")
        now = int(self._clock())
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
                    "bundle_key": {"S": self._bundle_key},
                    "owner": {"S": owner},
                    "expires_at": {"N": str(now + self._lease_seconds)},
                },
                ConditionExpression="attribute_not_exists(#bundle_key) OR #expires_at <= :now",
                ExpressionAttributeNames={
                    "#bundle_key": "bundle_key",
                    "#expires_at": "expires_at",
                },
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
        except Exception:
            return None
        return owner

    def release(self, owner: str) -> bool:
        if not isinstance(owner, str) or not owner:
            return False
        try:
            self._client.delete_item(
                TableName=self._table_name,
                Key={"bundle_key": {"S": self._bundle_key}},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": {"S": owner}},
            )
        except Exception:
            return False
        return True


class SSMTokenStore:
    def __init__(self, client: SSMClient, *, parameter_name: str):
        self._client = client
        self.parameter_name = parameter_name

    def save(self, bundle: TokenBundle) -> None:
        validate_stored_bundle(bundle)
        serialized = bundle.to_json()
        failed = False
        try:
            written = self._client.put_parameter(
                Name=self.parameter_name,
                Value=serialized,
                Type="SecureString",
                Overwrite=True,
            )
            readback = self._client.get_parameter(
                Name=self.parameter_name,
                WithDecryption=True,
            )["Parameter"]
            if (
                readback.get("Type") != "SecureString"
                or readback.get("Value") != serialized
                or not isinstance(written.get("Version"), int)
                or readback.get("Version") != written["Version"]
            ):
                raise ValueError
        except Exception:
            failed = True
        if failed:
            raise OAuthTokenError("oauth_token_store_write_failed")

    def load(self) -> TokenBundle:
        failed = False
        try:
            response = self._client.get_parameter(
                Name=self.parameter_name,
                WithDecryption=True,
            )
            parameter = response["Parameter"]
            value = parameter["Value"]
            if parameter.get("Type") != "SecureString" or not isinstance(value, str):
                raise ValueError
            return TokenBundle.from_json(value)
        except OAuthTokenError:
            raise
        except Exception:
            failed = True
        if failed:
            raise OAuthTokenError("oauth_token_store_read_failed")
        raise OAuthTokenError("oauth_token_store_read_failed")


class OAuthTokenManager:
    """Refresh on demand, serialize rotation with an optional shared lease."""

    def __init__(
        self,
        store: SSMTokenStore,
        *,
        http_client: httpx.Client | None = None,
        refresh_margin_seconds: int = 300,
        refresh_lease: RefreshLease | None = None,
        clock=time.time,
        token_clock=time.time,
    ) -> None:
        self._store = store
        self._http = http_client or httpx.Client(timeout=20, follow_redirects=False)
        self._owns_client = http_client is None
        self._margin = refresh_margin_seconds
        self._refresh_lease = refresh_lease
        self._clock = clock
        self._token_clock = token_clock
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> OAuthTokenManager:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def refresh(self) -> tuple[TokenBundle, bool]:
        """Perform one refresh; return bundle and whether rotation occurred."""
        with self._lock:
            current = self._store.load()
            return self._refresh_with_lease(current, refresh_required=True)

    def _refresh_bundle(self, current: TokenBundle) -> tuple[TokenBundle, bool]:
        refresh_failed = False
        try:
            response = self._http.post(
                current.token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current.refresh_token,
                    "client_id": current.client_id,
                    "scope": " ".join(sorted(current.scopes)),
                    "resource": current.resource,
                },
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, Mapping):
                raise ValueError
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            refresh_failed = True
            value = None
        if refresh_failed or not isinstance(value, Mapping):
            raise OAuthTokenError("oauth_refresh_failed")
        refreshed = validated_token_bundle(
            value,
            requested_scopes=current.scopes,
            token_endpoint=current.token_endpoint,
            client_id=current.client_id,
            resource=current.resource,
            prior_refresh_token=current.refresh_token,
            now=int(self._token_clock()),
        )
        rotated = refreshed.refresh_token != current.refresh_token
        try:
            self._store.save(refreshed)
        except OAuthTokenError:
            if rotated:
                raise OAuthTokenError("oauth_rotated_token_persistence_uncertain") from None
            raise
        return refreshed, rotated

    def _refresh_with_lease(
        self,
        observed: TokenBundle,
        *,
        refresh_required: bool,
    ) -> tuple[TokenBundle, bool]:
        """Acquire the cross-process lease, then re-read before refreshing."""
        if self._refresh_lease is None:
            return self._refresh_bundle(observed)
        owner = self._refresh_lease.acquire()
        if owner is None:
            # A fresh value written by the lease holder is safe to use; otherwise
            # do not risk reusing a rotating refresh token without the lease.
            current = self._store.load()
            if current != observed:
                return current, False
            if not refresh_required and current.expires_at - int(self._clock()) > self._margin:
                return current, False
            raise OAuthTokenError("oauth_refresh_lease_unavailable")
        try:
            current = self._store.load()
            if current != observed:
                return current, False
            if not refresh_required and current.expires_at - int(self._clock()) > self._margin:
                return current, False
            return self._refresh_bundle(current)
        finally:
            self._refresh_lease.release(owner)

    def access_token(self) -> tuple[str, bool]:
        """Return a usable token, refreshing once before the safety window."""
        with self._lock:
            current = self._store.load()
            if current.expires_at - int(self._clock()) > self._margin:
                return current.access_token, False
            refreshed, did_refresh = self._refresh_with_lease(current, refresh_required=False)
            return refreshed.access_token, did_refresh

    def refresh_after_unauthorized(self, rejected_token: str) -> tuple[TokenBundle, bool]:
        """Refresh once unless another caller already replaced the rejected token."""
        with self._lock:
            current = self._store.load()
            if not secrets.compare_digest(current.access_token, rejected_token):
                return current, False
            refreshed, did_refresh = self._refresh_with_lease(current, refresh_required=True)
            return refreshed, did_refresh
