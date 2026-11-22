from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from runway.auth.principal import (
    ALL_SCOPES,
    POSTS_RELEASE,
    Principal,
    get_principal,
    local_owner,
    require,
    scopes_for_role,
)
from runway.auth.problems import Problem, problem_handler
from runway.config import Settings


def test_roles_expand_to_the_specified_scopes() -> None:
    viewer = scopes_for_role("viewer")
    assert viewer == {
        "accounts:read",
        "content:read",
        "inbox:read",
        "posts:read",
    }
    editor = scopes_for_role("editor")
    assert editor == viewer | {"inbox:draft", "media:write", "posts:write", "posts:schedule"}
    approver = scopes_for_role("approver")
    assert approver == editor | {"posts:release", "inbox:reply"}
    assert scopes_for_role("owner") == ALL_SCOPES
    assert "workspace:admin" not in approver
    assert "accounts:manage" not in approver


def test_local_owner_is_the_workspace_1_owner_with_every_scope() -> None:
    owner = local_owner()
    assert owner.kind == "user"
    assert owner.id == "local-owner"
    assert owner.workspace_id == 1
    assert owner.role == "owner"
    assert owner.scopes == ALL_SCOPES


def test_require_raises_a_scope_missing_problem() -> None:
    viewer = Principal(
        kind="agent",
        id="key-1",
        workspace_id=1,
        role=None,
        scopes=scopes_for_role("viewer"),
        display="Muse",
    )
    require(viewer, "posts:read")
    with pytest.raises(Problem) as raised:
        require(viewer, POSTS_RELEASE)
    assert raised.value.status_code == 403
    assert raised.value.code == "scope_missing"
    with pytest.raises(ValueError, match="unknown scope"):
        require(local_owner(), "posts:delete-everything")


def _app(settings: Settings) -> FastAPI:
    application = FastAPI()
    application.state.settings = settings
    application.add_exception_handler(Problem, problem_handler)

    @application.get("/whoami")
    def whoami(principal: Principal = Depends(get_principal)) -> dict[str, object]:
        return {"id": principal.id, "workspace_id": principal.workspace_id}

    @application.post("/release")
    def release(principal: Principal = Depends(get_principal)) -> dict[str, str]:
        require(principal.model_copy(update={"scopes": frozenset()}), POSTS_RELEASE)
        return {"released": "yes"}

    return application


def test_get_principal_returns_the_local_owner_without_any_login(tmp_path: Path) -> None:
    client = TestClient(_app(Settings(data_dir=tmp_path / "data")))
    assert client.get("/whoami").json() == {"id": "local-owner", "workspace_id": 1}


def test_scope_missing_is_rendered_as_problem_json(tmp_path: Path) -> None:
    client = TestClient(_app(Settings(data_dir=tmp_path / "data")))
    response = client.post("/release")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "scope_missing"
    assert body["status"] == 403


def test_hosted_profile_without_an_identity_provider_returns_401(tmp_path: Path) -> None:
    # model_copy skips validation: hosted Settings cannot be built yet (no
    # identity provider, SQLite only), but the dependency must still fail closed.
    hosted = Settings(data_dir=tmp_path / "data").model_copy(update={"profile": "hosted"})
    response = TestClient(_app(hosted)).get("/whoami")
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == "Bearer"


def test_an_api_key_is_never_mapped_to_the_local_owner(tmp_path: Path) -> None:
    client = TestClient(_app(Settings(data_dir=tmp_path / "data")))
    response = client.get("/whoami", headers={"Authorization": "Bearer rwy_local_abcdefgh_x"})
    assert response.status_code == 401
    assert response.json()["code"] == "api_key_unsupported"
    # Other Authorization headers are not API keys and keep today's behaviour.
    assert client.get("/whoami", headers={"Authorization": "Basic Zm9vOmJhcg=="}).status_code == 200


def test_an_app_without_settings_fails_closed() -> None:
    application = FastAPI()

    @application.get("/whoami")
    def whoami(principal: Principal = Depends(get_principal)) -> str:
        return principal.id

    with pytest.raises(RuntimeError, match="no settings"):
        TestClient(application).get("/whoami")


# ------------------------------------------------------------------ profiles


def test_profile_defaults_to_local(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.profile == "local"
    assert settings.identity_provider == ""
    assert settings.public_dict()["profile"] == "local"


def test_local_profile_still_refuses_a_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(data_dir=tmp_path / "data", host="0.0.0.0")


def test_hosted_profile_refuses_sqlite_and_a_missing_identity_provider(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(data_dir=tmp_path / "data", profile="hosted")
    message = str(raised.value)
    assert "SQLite is local-only" in message
    assert "identity provider" in message


def test_hosted_profile_with_an_identity_provider_still_refuses_sqlite(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings(data_dir=tmp_path / "data", profile="hosted", identity_provider="clerk")
    message = str(raised.value)
    assert "SQLite is local-only" in message
    assert "identity provider" not in message


def test_hosted_profile_refuses_the_browser_publisher(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="authorized_browser"):
        Settings(
            data_dir=tmp_path / "data",
            profile="hosted",
            identity_provider="clerk",
            host="0.0.0.0",
            publishing_mode="authorized_browser",
            publishing_enabled=True,
            youtube_automation_authorized=True,
        )


def test_profile_is_read_from_runway_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNWAY_PROFILE", "hosted")
    with pytest.raises(ValidationError, match="RUNWAY_PROFILE=hosted requires"):
        Settings(data_dir=tmp_path / "data")
