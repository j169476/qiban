"""Bounded management discovery never grants reply permission."""
import pytest

from companion_core.reply_authorization import ReplyAuthorizations, ReplyScope, ReplyTarget
from companion_service.reply_management import ReplyManagement
from tests_postgres.test_web import client, login
from tests_postgres.test_web import service as service


@pytest.fixture
def search(service, monkeypatch):
    targets = [ReplyTarget(ReplyScope("qq", "10001", "private", str(20000 + i)), f"对象 {i}", "测试QQ")
               for i in range(55)]
    targets.append(ReplyTarget(ReplyScope("wechat", "wx-account", "private", "20000"), "微信对象", "测试微信"))
    monkeypatch.setattr(ReplyManagement, "targets", lambda self: (
        {t.scope.id: t for t in targets}, {"qq": {"available": True}, "wechat": {"available": True}}))
    admin = client(service, "admin")
    headers = login(admin, service, "admin")

    def query(**args):
        return admin.post("/operate", json={"operation": "reply_authorization_list", "arguments": args}, headers=headers)

    return query, targets


def test_exact_platform_search_does_not_grant_and_empty_search_is_rejected(service, search):
    query, targets = search
    response = query(platform="qq", query=" 20000 ", view="search")
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 1 and result["items"][0]["id"] == targets[0].scope.id
    assert not result["items"][0]["enabled"]
    assert ReplyAuthorizations(service.store).records() == []
    assert query(platform="qq", query="2000", view="search").json()["items"] == []
    assert query(platform="qq", query="", view="search").status_code == 403
    assert query(query="20000", view="search").status_code == 403


def test_pages_cover_all_matches_without_returning_full_directory(search):
    query, _ = search
    ids = []
    offset = 0
    while offset is not None:
        result = query(platform="qq", offset=offset, limit=20).json()
        assert result["total"] == 55 and len(result["items"]) <= 20
        ids.extend(r["id"] for r in result["items"])
        offset = result["next_offset"]
    assert len(ids) == len(set(ids)) == 55
    assert len(query().json()["items"]) == 20


def test_authorized_view_includes_missing_targets_for_revocation(service, search):
    query, targets = search
    permissions = ReplyAuthorizations(service.store)
    for scope in (targets[0].scope, ReplyScope("qq", "10001", "group", "99999")):
        permissions.update(scope, enabled=True, expected_version=None, actor="test")
    result = query(platform="qq", view="authorized").json()
    assert result["total"] == 2
    assert all(r["enabled"] for r in result["items"])
    missing = next(r for r in result["items"] if not r["verified_target"])
    assert missing["scope"]["target_id"] == "99999"
    assert query(platform="wechat", view="authorized").json()["items"] == []


@pytest.mark.parametrize("arguments", [{"platform": "bilibili"}, {"offset": -1}, {"limit": 51},
                                      {"offset": True}, {"limit": 0}, {"query": 123}, {"surprise": True}])
def test_invalid_filters_rejected(search, arguments):
    query, _ = search
    assert query(**arguments).status_code == 403
