"""Per-radio PTT lock: a second user's PTT request is rejected outright
while one user holds it — a lock, not a queue (no waiting/retry-for-you)."""


class PttDenied(Exception):
    def __init__(self, holder):
        self.holder = holder
        super().__init__(f"заето от {holder}")


class PttArbiter:
    def __init__(self):
        self.holder = None

    def acquire(self, username):
        if self.holder is not None and self.holder != username:
            raise PttDenied(self.holder)
        self.holder = username

    def release(self, username):
        if self.holder == username:
            self.holder = None


if __name__ == "__main__":
    a = PttArbiter()
    a.acquire("ivan")
    assert a.holder == "ivan"
    try:
        a.acquire("georgi")
        assert False, "expected PttDenied"
    except PttDenied as e:
        assert e.holder == "ivan"
    a.acquire("ivan")  # re-acquiring while already holding is a no-op
    a.release("georgi")  # releasing when you're not the holder is a no-op
    assert a.holder == "ivan"
    a.release("ivan")
    assert a.holder is None
    a.acquire("georgi")  # free again -> second user can take it
    assert a.holder == "georgi"
    print("ptt_arbiter.py: ok")
