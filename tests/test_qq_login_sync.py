from qiban.desktop_qq import DesktopQQ

ACCOUNT = {"uin": "10001", "uid": "u_test", "label": "测试", "can_quick_login": True}


class Worker:
    process = None

    def __init__(self, pages):
        self.pages = iter(pages)
        self.reads = 0
        self.closed = False

    def open(self):
        pass

    def request(self, command):
        if command == "login_status":
            return {"status": "authenticated", "uin": ACCOUNT["uin"]}
        if command == "login_accounts":
            self.reads += 1
            return {"accounts": next(self.pages)}
        return {}

    def close(self):
        self.closed = True


class FastCancel:
    def is_set(self):
        return False

    def wait(self, seconds):
        return False


def test_qr_success_waits_for_matching_usable_account(tmp_path):
    old = {**ACCOUNT, "uin": "20002"}
    worker = Worker([[old], [{**ACCOUNT, "can_quick_login": False}], [old, ACCOUNT]])
    qq = DesktopQQ(None, tmp_path, tmp_path, worker_factory=lambda _: worker)
    qq.cancelled = FastCancel()
    qq._run("login")
    assert worker.closed and worker.reads == 3
    assert qq.value["state"] == "available" and qq.value["authenticated_uin"] == "10001"


def test_qr_sync_timeout_never_offers_an_unrelated_cached_account(tmp_path):
    worker = Worker([[{**ACCOUNT, "uin": "20002"}]] * 41)
    qq = DesktopQQ(None, tmp_path, tmp_path, worker_factory=lambda _: worker)
    qq.cancelled = FastCancel()
    qq._run("login")
    assert worker.closed and worker.reads == 41
    assert qq.value["error"] == "qq_account_sync_pending" and qq.value["accounts"] == []
