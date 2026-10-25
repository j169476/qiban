import pytest

from companion_core.errors import AssistantError
from companion_core.governance import AdminService
from companion_core.sessions import SessionService


def setup(store):
    admin = AdminService(store)
    service = SessionService(admin.repository, admin.authority)
    ticket = service.setup_ticket()
    assert service.configure(ticket, "synthetic-password-2026")["role_started"] is False
    return service, ticket


def test_owner_setup_and_independent_sessions(pg_store):
    service, ticket = setup(pg_store)
    with pytest.raises(AssistantError, match="初始化"):
        service.configure(ticket, "synthetic-password-2026")
    admin = service.login("synthetic-password-2026", "admin")
    role = service.login("synthetic-password-2026", "role")
    assert service.request(admin, "admin").administrator
    assert not service.request(role, "role").administrator
    for token, kind in ((admin, "role"), (role, "admin")):
        with pytest.raises(AssistantError):
            service.request(token, kind)
    service.require_csrf(role, "role", service.csrf(role, "role"))
    with pytest.raises(AssistantError):
        service.require_csrf(role, "role", service.csrf(admin, "admin"))
    request = service.request(role, "role")
    service.logout(role)
    with pytest.raises(AssistantError):
        service.authority.require(request)
    assert pg_store.state("core-run", {}).get("status") != "active"


def test_rate_limit_survives_errors_and_restore_invalidates_sessions(pg_store):
    service, _ = setup(pg_store)
    token = service.login("synthetic-password-2026", "role")
    for _ in range(5):
        with pytest.raises(AssistantError):
            service.login("wrong-password-long", "admin")
    with pytest.raises(AssistantError, match="尝试次数"):
        service.login("synthetic-password-2026", "admin")
    assert pg_store.state("web-login-attempts:admin")["count"] == 5
    service.repository.reset_after_restore()
    with pytest.raises(AssistantError):
        service.request(token, "role")
