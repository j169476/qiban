import sqlite3

import pytest

from companion_wechat.db_setup import directory_page, directory_snapshot


def test_snapshot_keeps_nickname_and_alias_for_local_search():
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room,remark,nick_name,alias)')
        db.executemany('INSERT INTO contact VALUES(?,?,?,?,?,?,?,?,?)', [
            ('wxid_a',1,1,0,0,0,'同名','昵称甲','human_alias'),
            ('wxid_b',1,1,0,0,0,'同名','昵称乙','second_alias'),
            ('group@chatroom',2,0,0,0,1,'','测试群',''),
            ('filehelper',1,1,0,0,0,'助手','',''),
            ('gone',1,1,1,0,0,'删除','',''),
            ('own',1,1,0,0,0,'自己','','')])
        items = directory_snapshot(db, 'own')['items']
        assert len(items) == 3
        assert {row['kind'] for row in items} == {'private', 'group'}
        a = next(row for row in items if row['id'] == 'wxid_a')
        assert (a['name'], a['nickname'], a['alias']) == ('同名','昵称甲','human_alias')


def test_search_pages_verified_contacts_and_excludes_helpers():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE contact(username,local_type,flag,delete_flag,verify_flag,is_in_chat_room,remark,nick_name)")
        db.executemany("INSERT INTO contact VALUES(?,1,1,0,0,0,?,?)",
                       [(f"wxid_{i:03}", f"备注{i}", "同名") for i in range(24)]
                       + [("filehelper", "文件助手", ""), ("myself", "自己", "")])
        db.execute("INSERT INTO contact VALUES('deleted',1,1,1,0,0,'已删除','')")
        first = directory_page(db, "myself", "private", "", 1)
        assert (first["total"], first["pages"], len(first["items"])) == (24, 3, 10)
        last = directory_page(db, "myself", "private", "", 99)
        assert last["page"] == 3 and len(last["items"]) == 4
        assert directory_page(db, "myself", "private", "备注12", 1)["items"][0]["username"] == "wxid_012"
        assert directory_page(db, "myself", "private", "WXID_023", 1)["total"] == 1
        assert directory_page(db, "myself", "private", "同名", 1)["total"] == 24
        assert directory_page(db, "myself", "private", "' OR 1=1", 1)["total"] == 0
        assert directory_page(db, "myself", "group", "", 1)["total"] == 0
        for page in (0, True, -1, 10001):
            with pytest.raises(Exception, match="configuration_invalid"):
                directory_page(db, "myself", "private", "", page)
