import sqlite3

import pytest

from companion_wechat.db_setup import directory_page


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
