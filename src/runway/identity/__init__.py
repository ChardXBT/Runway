"""The identity-provider seam for the hosted profile (spec 05 section 2).

`IdentityProvider.verify(request) -> ExternalIdentity` is the only interface
the API depends on. Provider specifics live in this package: `clerk` (session
JWT), `clerk_webhooks` (event payloads), `svix` (webhook signatures).
`mirror.IdentityMirror` writes identities into Runway's own tables, which stay
the system of record for workspaces, members and roles.
"""

from runway.identity.base import (
    ExternalIdentity,
    IdentityProvider,
    IdentityRejected,
    IdentityUnavailable,
)

__all__ = ["ExternalIdentity", "IdentityProvider", "IdentityRejected", "IdentityUnavailable"]
