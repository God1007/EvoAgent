"""Authentication, signed sessions and tenant-aware role checks."""

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from .errors import AccessDeniedError, ClientInputError
from .json_boundary import strict_json_loads
from .metrics import metrics
from .ports import AuthStorePort

ROLE_PERMISSIONS = {
    "platform_admin": {"read", "review", "fix", "manage", "audit", "platform"},
    "admin": {"read", "review", "fix", "manage", "audit"},
    "maintainer": {"read", "review", "fix"},
    "auditor": {"read", "audit"},
}
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$310000$RXZvQWdlbnREdW1teVB3ZA$uaWkxY3pgeLDlH41S2jAe5kM_WCnboZy_elQfKpai6w"
)
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,63}$")
PASSWORD_ROUNDS = 310_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    if len(password) < 10:
        raise ClientInputError("password must contain at least 10 characters")
    try:
        password_bytes = password.encode("utf-8")
    except UnicodeEncodeError:
        raise ClientInputError("password must be valid UTF-8") from None
    if len(password_bytes) > 1024:
        raise ClientInputError("password must contain at most 1024 UTF-8 bytes")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, PASSWORD_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        PASSWORD_ROUNDS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        password_bytes = password.encode("utf-8")
        if len(password_bytes) > 1024 or len(encoded) > 256:
            return False
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256" or int(rounds) != PASSWORD_ROUNDS:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        if len(salt) != 16 or len(expected) != hashlib.sha256().digest_size:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, PASSWORD_ROUNDS)
        return hmac.compare_digest(actual, expected)
    except (UnicodeError, ValueError, TypeError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    tenant_id: str
    role: str
    credential_version: int = 0

    def can(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set())


class AuthManager:
    def __init__(
        self,
        store: AuthStorePort,
        secret: str,
        ttl_seconds: int = 3600,
        bootstrap_username: str = "",
        bootstrap_password: str = "",
        default_tenant_id: str = "default",
        previous_secret: str = "",
    ):
        if not isinstance(secret, str) or not isinstance(previous_secret, str):
            raise ValueError("authentication secrets must be strings")
        try:
            secret_bytes = secret.encode("utf-8")
            previous_secret_bytes = previous_secret.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("authentication secrets must be valid UTF-8") from None
        if secret_bytes and len(secret_bytes) < 32:
            raise ValueError("authentication secret must be empty or contain at least 32 bytes")
        if previous_secret_bytes and (not secret_bytes or len(previous_secret_bytes) < 32):
            raise ValueError(
                "previous authentication secret requires a current secret and at least 32 bytes"
            )
        self.store = store
        self.secret = secret_bytes
        self._verification_secrets = tuple(
            dict.fromkeys(value for value in (self.secret, previous_secret_bytes) if value)
        )
        self.ttl_seconds = ttl_seconds
        self.default_tenant_id = default_tenant_id
        if bootstrap_username and bootstrap_password:
            self.store.create_user(
                str(uuid.uuid5(uuid.NAMESPACE_URL, "evoagent:" + bootstrap_username)),
                bootstrap_username,
                hash_password(bootstrap_password),
                default_tenant_id,
                "platform_admin",
            )

    def login(self, username: str, password: str, tenant_id: str = "") -> dict[str, object]:
        user = self.store.get_user(username)
        password_hash = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
        password_valid = verify_password(password, password_hash)
        if not user or not user["active"] or not password_valid:
            raise AccessDeniedError("invalid username or password")
        memberships = {item["tenant_id"]: item["role"] for item in user["memberships"]}
        selected = tenant_id or (next(iter(memberships)) if len(memberships) == 1 else "")
        if not selected or selected not in memberships:
            raise AccessDeniedError("user is not a member of the requested tenant")
        now = int(time.time())
        payload = {
            "sub": user["id"],
            "username": user["username"],
            "tenant": selected,
            "role": memberships[selected],
            "credential_version": int(user.get("credential_version", 0)),
            "iat": now,
            "exp": now + self.ttl_seconds,
        }
        token = self._encode(payload)
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": self.ttl_seconds,
            "tenant_id": selected,
            "role": memberships[selected],
        }

    def authenticate(self, authorization: str) -> Principal:
        if not authorization.startswith("Bearer "):
            raise AccessDeniedError("Bearer token is required")
        return self._principal(self._decode(authorization[7:].strip()))

    def issue_state(
        self,
        principal: Principal,
        purpose: str,
        claims: dict[str, object] | None = None,
        ttl_seconds: int = 600,
    ) -> str:
        if not self.secret:
            raise RuntimeError("signed state requires authentication")
        reserved = {
            "sub",
            "username",
            "tenant",
            "role",
            "credential_version",
            "purpose",
            "iat",
            "exp",
            "jti",
        }
        if reserved.intersection(claims or {}):
            raise ValueError("signed state claims contain a reserved name")
        now = int(time.time())
        payload: dict[str, object] = {
            "sub": principal.user_id,
            "username": principal.username,
            "tenant": principal.tenant_id,
            "role": principal.role,
            "credential_version": principal.credential_version,
            "purpose": purpose,
            "iat": now,
            "exp": now + max(1, min(ttl_seconds, 900)),
            "jti": uuid.uuid4().hex,
        }
        payload.update(claims or {})
        return self._encode(payload)

    def authenticate_state(
        self,
        token: str,
        purpose: str,
        consume: bool = False,
    ) -> tuple[Principal, dict[str, object]]:
        payload = self._decode(token)
        if not hmac.compare_digest(str(payload.get("purpose", "")), purpose):
            raise AccessDeniedError("invalid signed state purpose")
        principal = self._principal(payload)
        if consume:
            jti = payload.get("jti")
            expires_at = payload.get("exp")
            if (
                not isinstance(jti, str)
                or not jti
                or len(jti) > 128
                or not isinstance(expires_at, int)
                or isinstance(expires_at, bool)
                or not self.store.consume_auth_state(jti, purpose, expires_at)
            ):
                raise AccessDeniedError("signed state has already been used")
        return principal, payload

    def change_password(
        self,
        principal: Principal,
        current_password: str,
        new_password: str,
    ) -> None:
        user = self.store.get_user(principal.username)
        if (
            not user
            or not user["active"]
            or str(user["id"]) != principal.user_id
            or not verify_password(current_password, user["password_hash"])
        ):
            raise AccessDeniedError("current password is invalid")
        if current_password == new_password:
            raise ClientInputError("new password must differ from the current password")
        if not self.store.change_user_password(
            principal.user_id,
            user["password_hash"],
            hash_password(new_password),
            principal.username,
            principal.tenant_id,
        ):
            raise AccessDeniedError("password changed concurrently; authenticate again")

    def provision_user(
        self,
        principal: Principal,
        username: str,
        password: str,
        role: str,
    ) -> dict[str, object]:
        self.require(principal, ("manage",))
        if not USERNAME.fullmatch(username):
            raise ClientInputError("username must be one 1-64 character local identifier")
        if role not in ROLE_PERMISSIONS:
            raise ClientInputError("role must be platform_admin, admin, maintainer or auditor")
        if role == "platform_admin" and not principal.can("platform"):
            raise AccessDeniedError("only a platform administrator can grant platform access")
        user_id = str(uuid.uuid4())
        if not self.store.create_user(
            user_id,
            username,
            hash_password(password),
            principal.tenant_id,
            role,
            principal.username,
        ):
            raise ClientInputError("username already exists")
        return {
            "id": user_id,
            "username": username,
            "active": True,
            "tenant_id": principal.tenant_id,
            "role": role,
        }

    def set_user_active(
        self,
        principal: Principal,
        username: str,
        active: bool,
    ) -> dict[str, object]:
        self.require(principal, ("platform",))
        if not USERNAME.fullmatch(username):
            raise ClientInputError("username must be one 1-64 character local identifier")
        user = self.store.get_user(username)
        if not user:
            raise ClientInputError("user does not exist")
        if not active and str(user["id"]) == principal.user_id:
            raise ClientInputError("a platform administrator cannot disable their own account")
        if not self.store.set_user_active(
            str(user["id"]), active, principal.username, principal.tenant_id
        ):
            raise ClientInputError("user does not exist")
        return {"id": str(user["id"]), "username": username, "active": active}

    def _principal(self, payload: dict[str, object]) -> Principal:
        username = str(payload["username"])
        tenant_id = str(payload["tenant"])
        user = self.store.get_user(username)
        if not user or not user["active"] or str(user["id"]) != str(payload["sub"]):
            raise AccessDeniedError("token identity is no longer active")
        memberships = {item["tenant_id"]: item["role"] for item in user["memberships"]}
        role = memberships.get(tenant_id)
        if role not in ROLE_PERMISSIONS:
            raise AccessDeniedError("token tenant membership is no longer active")
        credential_version = payload.get("credential_version", 0)
        if (
            not isinstance(credential_version, int)
            or isinstance(credential_version, bool)
            or credential_version != int(user.get("credential_version", 0))
        ):
            raise AccessDeniedError("token credentials are no longer current")
        return Principal(
            str(user["id"]),
            str(user["username"]),
            tenant_id,
            role,
            credential_version,
        )

    @staticmethod
    def require(principal: Principal, permissions: Iterable[str]) -> None:
        missing = [permission for permission in permissions if not principal.can(permission)]
        if missing:
            raise AccessDeniedError("permission denied")

    def _encode(self, payload: dict[str, object]) -> str:
        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signing_input = (header + "." + body).encode("ascii")
        signature = _b64(hmac.new(self.secret, signing_input, hashlib.sha256).digest())
        return header + "." + body + "." + signature

    def _decode(self, token: str) -> dict[str, object]:
        return self._decode_verified(token)[0]

    def state_binding(self, token: str, context: str) -> str:
        """Derive a state-bound value with the key that actually signed the token."""
        if not context or len(context) > 100:
            raise ValueError("signed state binding context is invalid")
        _payload, signing_secret = self._decode_verified(token)
        return _b64(
            hmac.new(
                signing_secret,
                context.encode("utf-8") + b":" + token.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )

    def _decode_verified(self, token: str) -> tuple[dict[str, object], bytes]:
        try:
            header, body, supplied = token.split(".", 2)
            signing_input = (header + "." + body).encode("ascii")
            signing_secret = next(
                (
                    secret
                    for secret in self._verification_secrets
                    if hmac.compare_digest(
                        _b64(hmac.new(secret, signing_input, hashlib.sha256).digest()), supplied
                    )
                ),
                None,
            )
            if signing_secret is None:
                raise AccessDeniedError("invalid token")
            if signing_secret != self.secret:
                metrics.inc("auth_previous_secret_verifications_total")
            if strict_json_loads(_unb64(header)) != {"alg": "HS256", "typ": "JWT"}:
                raise AccessDeniedError("invalid token")
            payload = strict_json_loads(_unb64(body))
            if not isinstance(payload, dict):
                raise AccessDeniedError("invalid token")
            expires_at = payload.get("exp")
            if not isinstance(expires_at, int) or isinstance(expires_at, bool):
                raise AccessDeniedError("invalid token")
            if expires_at <= int(time.time()):
                raise AccessDeniedError("token has expired")
            if payload.get("role") not in ROLE_PERMISSIONS:
                raise AccessDeniedError("invalid role")
            return payload, signing_secret
        except AccessDeniedError:
            raise
        except Exception as exc:
            raise AccessDeniedError("invalid token") from exc
