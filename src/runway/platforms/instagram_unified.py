"""Instagram Unified Login provider (spec 08 section 3, "Unified Login adapter").

Facebook Login for Business: the consent URL is
`facebook.com/dialog/oauth?client_id=<META_APP_ID>&config_id=<CONFIG_ID>` —
permissions come from the configuration, so the URL carries no scope list.
The exchange runs on `graph.facebook.com` with the **Meta** app credentials
and yields a Business Integration System User token. Its lifetime is set by
the configuration; there is no long-lived exchange. `GET /me` answers the
system user (`client_business_id`), and
`GET /me/assigned_instagram_accounts` lists the professional accounts the
owner may connect — the stored token marks itself `login="unified"` so the
read side of `InstagramProvider` switches to the Facebook Graph host.
"""

from __future__ import annotations

import httpx

from runway.platforms.base import ProviderError, TokenSet, provider_request
from runway.platforms.instagram import FACEBOOK_GRAPH, InstagramProvider

DIALOG_URL = "https://www.facebook.com/dialog/oauth"
TOKEN_URL = f"{FACEBOOK_GRAPH}/oauth/access_token"
RUPLOAD_BASE = "https://rupload.facebook.com/ig-api-upload"

# Permissions granted by a Unified Login configuration (the Business scope
# names without `business`; `equivalent_scopes` maps them for capability
# checks). A token response naming its own `granted_scopes` wins.
UNIFIED_SCOPES = (
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
)


class InstagramUnifiedProvider(InstagramProvider):
    """The OAuth side of Unified Login; reads are inherited branches."""

    def configured(self) -> bool:
        return self.settings.instagram_unified_oauth_configured

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
        scopes: list[str] | tuple[str, ...] | None = None,
    ) -> str:
        params = {
            "client_id": self.settings.meta_app_id or "",
            "config_id": self.settings.instagram_unified_config_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{DIALOG_URL}?{httpx.QueryParams(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
        requested_scopes: list[str] | tuple[str, ...] | None = None,
    ) -> TokenSet:
        response = provider_request(
            self.client,
            "Instagram",
            "GET",
            TOKEN_URL,
            params={
                "client_id": self.settings.meta_app_id or "",
                "client_secret": self.settings.meta_app_secret or "",
                "redirect_uri": redirect_uri,
                "code": code[:-2] if code.endswith("#_") else code,
            },
        )
        data = self._json_or_raise(response)
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("Unified Login exchange returned no access token")
        granted = data.get("granted_scopes")
        scopes = (
            [str(scope) for scope in granted]
            if isinstance(granted, list) and granted
            else list(UNIFIED_SCOPES)
        )
        extra: dict[str, str] = {
            "login": "unified",
            "config_id": self.settings.instagram_unified_config_id or "",
        }
        expires_in = data.get("expires_in")
        if expires_in is not None:
            extra["expires_in"] = str(expires_in)
        return TokenSet(access_token=access_token, scopes=scopes, extra=extra)

    def attach_business_id(self, token: TokenSet) -> TokenSet:
        """`GET /me?fields=id,client_business_id` — the user and their portfolio.

        `id` is the app-scoped Meta user id: Meta's deauthorize and
        data-deletion signed_requests name it, so the token carries it to
        the account row (metadata_json `meta_user_id`), where the compliance
        sweep can find the account.
        """
        data = self._graph_get(token, "me", {"fields": "id,client_business_id"})
        business_id = data.get("client_business_id")
        if not business_id:
            raise ProviderError("Unified Login /me returned no client_business_id")
        extra = dict(token.extra)
        extra["client_business_id"] = str(business_id)
        meta_user_id = data.get("id")
        if meta_user_id:
            extra["meta_user_id"] = str(meta_user_id)
        return token.model_copy(update={"extra": extra})

    def assigned_accounts(self, token: TokenSet) -> list[dict[str, str]]:
        """`GET /me/assigned_instagram_accounts` — the accounts the owner picks from."""
        data = self._graph_get(token, "me/assigned_instagram_accounts", {"fields": "id,username"})
        entries = data.get("data")
        accounts: list[dict[str, str]] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            accounts.append(
                {
                    "id": str(entry["id"]),
                    "username": str(entry.get("username") or ""),
                }
            )
        return accounts


def unified_token_mark(token: TokenSet) -> bool:
    """True when the stored token came through Unified Login."""
    return token.extra.get("login") == "unified"


__all__ = [
    "DIALOG_URL",
    "TOKEN_URL",
    "RUPLOAD_BASE",
    "UNIFIED_SCOPES",
    "InstagramUnifiedProvider",
    "unified_token_mark",
]
