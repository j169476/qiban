import pytest

from companion_wechat.db_triggers import message
from tests_wechat.test_db_source import Harness

SELF = "wxid_synthetic_bot"


@pytest.fixture
def lab(tmp_path):
    value = Harness(tmp_path)
    try:
        yield value
    finally:
        value.close()


def parse(kind, text, source=None, verified=False):
    return message(kind, text, source, SELF, own_reference=lambda _identifier: verified)


def test_only_metadata_mentions_wake_and_entity_xml_is_rejected():
    metadata = f"<msgsource><atuserlist><![CDATA[wxid_other,{SELF}]]></atuserlist></msgsource>"
    assert parse(1, "hello", metadata) == ("hello", "mention")
    assert parse(1, metadata)[1] == "none"
    assert parse(1, "@小龙娘") == ("@小龙娘", "none")
    assert parse(1, "hello", metadata.replace(SELF, "notify@all"))[1] == "none"
    assert parse(1, "hello", '<!DOCTYPE x [<!ENTITY e "x">]>' + metadata)[1] == "none"


def test_quote_requires_structured_type_and_verified_own_message_in_same_chat():
    body = f"<msg><appmsg><type>57</type><title>follow up</title><refermsg><fromusr>{SELF}</fromusr><svrid>1234</svrid></refermsg></appmsg></msg>"
    assert parse((57 << 32) | 49, body, verified=True) == ("follow up", "reply")
    assert parse(49, body, verified=False) == ("follow up", "none")
    assert parse(49, body.replace(SELF, "wxid_other"), verified=True)[1] == "none"
    assert parse(1, body, verified=True)[1] == "none"
    assert parse(49, body.replace("<type>57", "<type>5"), verified=True) == (None, "none")


def test_real_source_selects_metadata_and_verifies_quoted_sender(lab):
    from tests_wechat.test_db_source import GROUP, OWNER, SELF, message_table

    connection = lab.shards["message_0.db"]
    for target in (GROUP, OWNER):
        connection.execute(f'ALTER TABLE "{message_table(target)}" ADD COLUMN source TEXT')
    table = message_table(GROUP)
    connection.execute(f'INSERT INTO "{table}" VALUES(1,1234,1700000001,1,2,?,9,NULL)', ("own message",))
    with lab.session() as session:
        initial = session.snapshot()
        connection.execute(f'INSERT INTO "{table}" VALUES(2,1235,1700000002,1,3,?,9,?)',
            ("@shown hello", f"<msgsource><atuserlist>{SELF}</atuserlist></msgsource>"))
        quote = f"<msg><appmsg><type>57</type><title>question</title><refermsg><fromusr>{SELF}</fromusr><svrid>1234</svrid></refermsg></appmsg></msg>"
        connection.execute(f'INSERT INTO "{table}" VALUES(3,1236,1700000003,?,3,?,9,NULL)', ((57 << 32) | 49, quote))
        result = session.read(initial["checkpoint"])
        assert [r["trigger"] for r in result["rows"]] == ["mention", "reply"]
