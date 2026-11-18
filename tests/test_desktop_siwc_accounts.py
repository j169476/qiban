"""Account isolation and manual selection, with synthetic loopback authorization."""

import copy
import json
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest
from test_desktop_siwc import Store, token, wait
from test_desktop_siwc import signing as signing

from companion_core.errors import AssistantError
from qiban.desktop_siwc import ISSUER, TOKEN
from qiban.desktop_siwc_accounts import DesktopSiwcAccounts, catalog, registration_id, selected


def account(client="client-a", subject="subject-a", email="same@example.invalid"):
    return {
        "host": "stable-host",
        "client_id": client,
        "subject": subject,
        "email": email,
        "session": {
            "access_token": "access-" + client,
            "refresh_token": "refresh-" + client,
            "scopes": ["chatgpt.tokens.use.direct"],
            "expires_at": 10000,
        },
    }


def fixture(tmp_path):
    store = Store()
    first, second = account(), account("client-b", "subject-b")
    a, b = registration_id(first), registration_id(second)
    store.value = {
        "format": 2,
        "revision": 3,
        "host": "stable-host",
        "active": a,
        "accounts": {a: {**first, "number": 1}, b: {**second, "number": 2}},
    }

    def no_network(*args, **kwargs):
        raise AssertionError("No network request expected")

    return DesktopSiwcAccounts(tmp_path, store, transport=no_network, clock=lambda: 1000), store, a, b


def choose(broker, identifier, expected=None):
    broker.begin(
        "select",
        account_id=identifier,
        expected=broker.status()["accounts_revision"] if expected is None else expected,
    )
    return wait(broker)


def test_selection_is_explicit_durable_and_does_not_log_out_others(tmp_path):
    broker, store, a, b = fixture(tmp_path)
    before = copy.deepcopy(store.value["accounts"])
    assert choose(broker, b)["active_account"] == b
    assert store.value["accounts"] == before
    restarted = DesktopSiwcAccounts(tmp_path, store)
    assert restarted.status()["active_account"] == b
    assert choose(broker, a)["active_account"] == a
    assert "refresh-client" not in json.dumps(broker.status())
    labels = [row["label"] for row in broker.status()["accounts"]]
    assert labels[0] != labels[1] and all("same@example.invalid" in label for label in labels)


def test_stale_or_unknown_selection_does_not_change_account(tmp_path):
    broker, store, a, b = fixture(tmp_path)
    assert choose(broker, b, expected=2)["code"] == "model_account_changed"
    assert store.value["active"] == a
    assert choose(broker, "0" * 32)["code"] == "model_account_changed"
    assert store.value["active"] == a
    with pytest.raises(AssistantError):
        broker.begin("select", account_id="../account", expected=3)


def test_legacy_account_is_read_without_rewriting_then_preserved_on_mutation(tmp_path):
    store = Store()
    store.value = account()
    original = copy.deepcopy(store.value)
    broker = DesktopSiwcAccounts(tmp_path, store, clock=lambda: 1000)
    assert store.value == original and len(broker.status()["accounts"]) == 1
    broker.begin("refresh")
    assert wait(broker)["state"] == "authenticated"
    assert store.value == original  # Not near expiry; no need to write or migrate.
    choose(broker, broker.status()["active_account"])
    assert store.value["format"] == 2
    assert selected(store.value)["session"] == original["session"]
    assert store.value["host"] == original["host"]


def test_renewal_and_logout_affect_only_selected_registration(tmp_path):
    broker, store, a, b = fixture(tmp_path)
    store.value["accounts"][a]["session"]["expires_at"] = 1001
    untouched = copy.deepcopy(store.value["accounts"][b])
    sent = []

    def transport(url, form=None, **kwargs):
        assert url == TOKEN and form["client_id"] == "client-a"
        sent.append(form["refresh_token"])
        return {
            "access_token": "new-a",
            "refresh_token": "new-refresh-a",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    broker.transport = transport
    broker.begin("refresh")
    assert wait(broker)["renewal_status"] == "complete"
    assert sent == ["refresh-client-a"] and store.value["accounts"][b] == untouched
    revoked = []
    broker._revoke = lambda client, token: revoked.append((client, token))
    broker.begin("logout")
    assert wait(broker)["state"] == "signed_out"
    assert revoked == [("client-a", "new-refresh-a")]
    assert store.value["active"] == a and "session" not in store.value["accounts"][a]
    assert store.value["accounts"][b] == untouched


def test_invalid_selected_session_never_falls_back_to_other_account(tmp_path):
    broker, store, a, b = fixture(tmp_path)
    store.value["accounts"][a]["renewal"] = {"status": "uncertain"}
    broker.begin("refresh")
    assert wait(broker)["renewal_status"] == "uncertain" and store.value["active"] == a


def test_cancel_adding_keeps_original_active_account(tmp_path):
    broker, store, a, b = fixture(tmp_path)
    before = copy.deepcopy(store.value)
    broker.begin("add")
    wait(broker, "waiting")
    query = parse_qs(urlsplit(broker.open_login()["url"]).query)
    assert query["client_id"] == ["dynamic_agent_client"] and query["ext_agent_host_id"] == ["stable-host"]
    with pytest.raises(AssistantError):
        broker.begin("select", account_id=b, expected=3)
    broker.cancel()
    assert wait(broker)["state"] == "cancelled"
    assert store.value == before


@pytest.mark.parametrize('handoff_failed', [False, True])
def test_new_identity_activates_only_after_real_signed_callback(tmp_path, signing, handoff_failed):
    broker, store, a, b = fixture(tmp_path)
    before = copy.deepcopy(store.value["accounts"])
    parameters = {}
    cutovers, revoked = [], []
    def change(commit, *, ready):
        assert ready
        cutovers.append(True)
        commit()
        if handoff_failed:
            raise AssistantError('model_account_handoff_pending', 'Synthetic post-commit failure')
    broker.account_change = change
    broker._revoke = lambda *args: revoked.append(True)

    def transport(url, form=None, **kwargs):
        if url == TOKEN:
            assert store.value["active"] == a and store.value["accounts"] == before
            assert form["client_id"] == "oaiapp_synthetic"
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid offline_access chatgpt.tokens.use.direct",
                "id_token": token(signing, nonce=parameters["nonce"][0]),
            }
        if url.endswith("openid-configuration"):
            return {
                "issuer": ISSUER,
                "id_token_signing_alg_values_supported": ["RS256"],
                "jwks_uri": ISSUER + "/keys",
            }
        return signing[1]

    broker.transport = transport
    broker.begin("add")
    wait(broker, "waiting")
    parameters.update(parse_qs(urlsplit(broker.open_login()["url"]).query))
    with urlopen(
        parameters["redirect_uri"][0]
        + "?"
        + urlencode({"state": parameters["state"][0], "code": "synthetic", "client_id": "oaiapp_synthetic"}),
        timeout=2,
    ) as response:
        assert response.status == 200
    result = wait(broker)
    assert result["state"] == ('error' if handoff_failed else 'authenticated') and result["active_account"] not in (a, b)
    assert cutovers == [True] and not revoked
    assert len(store.value["accounts"]) == 3
    assert all(store.value["accounts"][key] == value for key, value in before.items())
    assert store.value["host"] == "stable-host"
    assert "new-refresh" not in json.dumps(result)


def test_unknown_catalog_format_is_not_silently_reset():
    with pytest.raises(ValueError):
        catalog({"format": 99, "accounts": {}})


def test_platform_refresh_rotates_only_selected_account_before_admission_cutoff(tmp_path):
    from test_desktop_siwc_sessions import fresh
    broker, store, a, b = fixture(tmp_path)
    store.value['accounts'][a]['session']['expires_at'] = broker.clock() + 480
    untouched = copy.deepcopy(store.value['accounts'][b])
    sent = []
    broker.transport = lambda *args: sent.append(args) or fresh()
    assert broker.ready_for_model() and broker.platform_refresh_due()
    assert broker.refresh_for_platform()
    assert not broker.refresh_for_platform()
    assert len(sent) == 1 and sent[0][0] == TOKEN
    assert broker.model_access().access_token == 'new-access'
    assert store.value['accounts'][b] == untouched
    assert broker.status()['automatic_renewal_enabled']


def test_unattended_refresh_waits_for_retry_after_and_never_replays_uncertain_rotation(tmp_path):
    broker, store, a, _ = fixture(tmp_path)
    value = store.value['accounts'][a]
    value['session']['expires_at'] = 999
    value['renewal'] = {'status':'temporary_failure', 'retry_at':1100}
    assert broker.waiting_for_renewal() and not broker.platform_refresh_due()
    broker.clock = lambda:1100
    assert broker.platform_refresh_due()
    value['renewal'] = {'status':'uncertain'}
    assert not broker.waiting_for_renewal() and not broker.refresh_for_platform()
