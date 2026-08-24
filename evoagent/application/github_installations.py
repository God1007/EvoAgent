"""Tenant-safe GitHub App installation binding."""

from __future__ import annotations

from ..auth import AuthManager, Principal
from ..errors import ClientInputError
from ..github import GITHUB_INSTALLATION_ID_MAX, GitHubInstallationOAuthClient
from ..ports import ApplicationStorePort


class GitHubInstallationUseCases:
    def __init__(
        self,
        store: ApplicationStorePort,
        auth: AuthManager,
        oauth: GitHubInstallationOAuthClient,
        app_slug: str,
    ):
        self.store = store
        self.auth = auth
        self.oauth = oauth
        self.app_slug = app_slug

    def begin(self, principal: Principal) -> str:
        self._configured()
        self.auth.require(principal, ("manage",))
        state = self.auth.issue_state(principal, "github-install")
        return self.oauth.installation_url(self.app_slug, state)

    def authorize(self, state: str, installation_id: str) -> str:
        self._configured()
        self._state(state)
        if (
            not isinstance(installation_id, str)
            or not installation_id.isascii()
            or not installation_id.isdigit()
            or installation_id.startswith("0")
        ):
            raise ClientInputError("invalid GitHub installation id")
        parsed_id = int(installation_id)
        if not 1 <= parsed_id <= GITHUB_INSTALLATION_ID_MAX:
            raise ClientInputError("invalid GitHub installation id")
        principal, _claims = self.auth.authenticate_state(state, "github-install", consume=True)
        self.auth.require(principal, ("manage",))
        oauth_state = self.auth.issue_state(
            principal,
            "github-oauth",
            {"installation_id": parsed_id},
        )
        return self.oauth.authorization_url(oauth_state, self._verifier(oauth_state))

    def complete(self, state: str, code: str) -> dict[str, object]:
        self._configured()
        self._state(state)
        if not GitHubInstallationOAuthClient.valid_authorization_code(code):
            raise ClientInputError("invalid GitHub authorization code")
        principal, claims = self.auth.authenticate_state(state, "github-oauth", consume=True)
        self.auth.require(principal, ("manage",))
        installation_id = claims.get("installation_id")
        if (
            not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or not 1 <= installation_id <= GITHUB_INSTALLATION_ID_MAX
        ):
            raise ClientInputError("invalid GitHub installation state")
        account = self.oauth.verify_installation(
            code,
            self._verifier(state),
            installation_id,
        )
        self.store.bind_installation(
            installation_id,
            account[:256],
            principal.tenant_id,
            principal.username,
        )
        return {"installation_id": installation_id, "account": account[:256]}

    def _configured(self) -> None:
        if not (
            self.app_slug
            and self.oauth.client_id
            and self.oauth.client_secret
            and self.oauth.callback_url
        ):
            raise ClientInputError("GitHub installation OAuth is not configured")

    @staticmethod
    def _state(state: str) -> None:
        if not isinstance(state, str) or not state or len(state) > 4096:
            raise ClientInputError("invalid GitHub installation state")

    def _verifier(self, state: str) -> str:
        return self.auth.state_binding(state, "github-install-pkce")
