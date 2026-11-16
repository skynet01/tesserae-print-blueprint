import json

import pytest

from blueprint.kie import KieClient, KieError, KieTaskFailed


class Http:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def request(self, method, url, headers=None, body=None, timeout=60):
        self.calls.append((method, url, json.loads(body) if body else None))
        status, data = self.replies.pop(0)
        return status, data if isinstance(data, bytes) else json.dumps(data).encode()


def client(replies):
    http = Http(replies)
    return KieClient("KEY", http, sleep=lambda s: None), http


def test_kie_flow_mocked():
    c, http = client([
        (200, {"code": 200, "data": {"downloadUrl": "https://u/x.png"}}),
        (200, {"code": 200, "data": {"taskId": "T1"}}),
        (200, {"code": 200, "data": {"state": "waiting"}}),
        (200, {"code": 200, "data": {"state": "success", "resultJson": json.dumps({"resultUrls": ["https://r/y.png"]})}}),
        (200, b"PNGDATA"),
    ])
    assert c.repaint(b"src", "draw it") == b"PNGDATA"
    urls = [u for _, u, _ in http.calls]
    assert "file-base64-upload" in urls[0] and urls[1].endswith("/createTask")
    assert urls[2].endswith("taskId=T1") and urls[4] == "https://r/y.png"
    body = http.calls[1][2]
    assert body["model"] == "google/nano-banana-edit"
    assert body["input"]["image_urls"] == ["https://u/x.png"] and body["input"]["prompt"] == "draw it"


def test_kie_error_surfaces_message():
    c, _ = client([(200, {"code": 200, "data": {"downloadUrl": "u"}}), (200, {"code": 402, "msg": "Insufficient credits"})])
    with pytest.raises(KieError, match="Insufficient credits"):
        c.repaint(b"x")


def test_kie_task_failure_message():
    c, _ = client([(200, {"code": 200, "data": {"downloadUrl": "u"}}), (200, {"code": 200, "data": {"taskId": "T"}}),
                   (200, {"code": 200, "data": {"state": "fail", "failMsg": "content policy"}})])
    with pytest.raises(KieTaskFailed, match="content policy"):
        c.repaint(b"x")


def test_kie_upload_falls_back_to_second_host():
    c, http = client([(500, {"msg": "down"}), (200, {"code": 200, "data": {"downloadUrl": "u"}}),
                      (200, {"code": 200, "data": {"taskId": "T"}}),
                      (200, {"code": 200, "data": {"state": "success", "resultJson": {"resultUrls": ["r"]}}}), (200, b"P")])
    assert c.repaint(b"x") == b"P"
    assert http.calls[1][1].startswith("https://api.kie.ai/api/file-base64-upload")


def test_kie_times_out():
    t = iter(range(0, 10000, 100))
    http = Http([(200, {"code": 200, "data": {"downloadUrl": "u"}}), (200, {"code": 200, "data": {"taskId": "T"}})]
                + [(200, {"code": 200, "data": {"state": "waiting"}})] * 20)
    c = KieClient("K", http, sleep=lambda s: None, clock=lambda: next(t))
    with pytest.raises(KieError, match="timed out"):
        c.repaint(b"x", timeout_s=240)


def test_credits():
    c, _ = client([(200, {"code": 200, "data": 123.5})])
    assert c.credits() == 123.5


def test_kie_poll_survives_transient_errors():
    """The task is paid for once created: a 502 or a network blip while polling is retried."""
    class Flaky(Http):
        def request(self, method, url, headers=None, body=None, timeout=60):
            if "recordInfo" in url and self.blips:
                self.blips -= 1
                self.calls.append((method, url, None))
                raise KieError("network error: connection reset")
            return super().request(method, url, headers, body, timeout)

    http = Flaky([(200, {"code": 200, "data": {"downloadUrl": "u"}}), (200, {"code": 200, "data": {"taskId": "T"}}),
                  (502, b"<html>bad gateway</html>"),
                  (200, {"code": 200, "data": {"state": "success", "resultJson": {"resultUrls": ["r"]}}}),
                  (200, b"P")])
    http.blips = 1
    c = KieClient("K", http, sleep=lambda s: None)
    assert c.repaint(b"x") == b"P"
    assert sum("recordInfo" in u for _, u, _ in http.calls) == 3


def test_kie_poll_errors_until_timeout_name_the_last_error():
    t = iter(range(0, 10000, 100))
    http = Http([(200, {"code": 200, "data": {"downloadUrl": "u"}}), (200, {"code": 200, "data": {"taskId": "T"}})]
                + [(503, {"msg": "busy"})] * 20)
    c = KieClient("K", http, sleep=lambda s: None, clock=lambda: next(t))
    with pytest.raises(KieError, match="timed out.*busy"):
        c.repaint(b"x", timeout_s=240)
