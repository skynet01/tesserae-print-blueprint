"""kie.ai client for the optional AI engraving: upload, createTask, poll recordInfo, download."""
from __future__ import annotations

from typing import Callable

import base64
import json
import time
import urllib.error
import urllib.request

UPLOAD_URLS = ("https://kieai.redpandaai.co/api/file-base64-upload", "https://api.kie.ai/api/file-base64-upload")
CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
RECORD_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
CREDIT_URL = "https://api.kie.ai/api/v1/chat/credit"
MODEL = "google/nano-banana-edit"

PROMPT = (
    "Redraw this exact 3D-printed object as a vintage engineering technical illustration: "
    "black pen-and-ink line art with fine cross-hatching shading, like a 1950s patent drawing "
    "or exploded-view service manual plate. Keep the exact same shape, parts, proportions, "
    "pose and camera angle. Pure white background, no text, no labels, no numbers, "
    "no border, no shadow on the ground, no colour."
)


class KieError(Exception):
    """kie.ai refused or failed; the message is the API's own."""


class KieTaskFailed(KieError):
    """The provider explicitly reported state=fail (no charge). Safe to retry later."""


class UrllibHttp:
    """`request(method, url, headers, body, timeout) -> (status, bytes)`; never raises on an HTTP status."""

    def request(self, method: str, url: str, headers: dict | None = None, body: bytes | None = None,
                timeout: float = 60) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError as e:
            raise KieError(f"network error: {e}") from e


def _message(answer) -> str:
    if isinstance(answer, dict):
        for k in ("msg", "message", "error"):
            v = answer.get(k)
            if isinstance(v, dict):
                v = v.get("message")
            if v:
                return str(v)[:200]
    return ""


class KieClient:
    def __init__(self, key: str, http=None, sleep=time.sleep, clock=time.monotonic, poll_s: float = 4.0):
        self.key, self.http = key, http or UrllibHttp()
        self._sleep, self._clock, self.poll_s = sleep, clock, poll_s

    def _json(self, method: str, url: str, body: dict | None = None, timeout: float = 60) -> dict:
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        status, raw = self.http.request(method, url, headers, json.dumps(body).encode() if body is not None else None, timeout)
        try:
            answer = json.loads(raw)
        except ValueError:
            raise KieError(f"kie.ai answered HTTP {status} with no JSON") from None
        code = answer.get("code") if isinstance(answer, dict) else None
        if status >= 400 or (code is not None and code != 200):
            raise KieError(_message(answer) or f"kie.ai answered HTTP {status}")
        return answer

    def _upload(self, png: bytes) -> str:
        body = {"base64Data": "data:image/png;base64," + base64.b64encode(png).decode(),
                "uploadPath": "tesserae-print-blueprint", "fileName": "source.png"}
        err = None
        for url in UPLOAD_URLS:
            try:
                url_out = (self._json("POST", url, body).get("data") or {}).get("downloadUrl")
            except KieError as e:
                err = e
                continue
            if url_out:
                return url_out
        raise err or KieError("upload returned no URL")

    def repaint(self, png: bytes, prompt: str = PROMPT, timeout_s: float = 240,
                on_task: "Callable[[str], None] | None" = None) -> bytes:
        """Upload, start the paid task and wait for its picture. `on_task(task_id)` runs as soon as
        the task exists, so a caller can keep the id and `collect()` it later if this times out."""
        src = self._upload(png)
        task = self._json("POST", CREATE_URL, {"model": MODEL, "input": {
            "prompt": prompt, "image_urls": [src], "output_format": "png", "aspect_ratio": "1:1"}})
        tid = (task.get("data") or {}).get("taskId")
        if not tid:
            raise KieError(_message(task) or "kie.ai did not return a task id")
        if on_task is not None:
            on_task(str(tid))
        return self.collect(str(tid), timeout_s)

    def collect(self, tid: str, timeout_s: float = 240) -> bytes:
        """The picture of a task already paid for (polls until it is done or `timeout_s`)."""
        t0 = self._clock()
        last_err: KieError | None = None
        while True:
            if self._clock() - t0 > timeout_s:
                why = f" (last poll: {last_err})" if last_err else ""
                raise KieError(f"timed out after {timeout_s:.0f} s{why}")
            self._sleep(self.poll_s)
            try:  # the task is paid for already: a failed poll is retried until the timeout
                rec = self._json("GET", f"{RECORD_URL}?taskId={tid}", timeout=30).get("data") or {}
            except KieError as e:
                last_err = e
                continue
            state = rec.get("state")
            if state == "fail":
                raise KieTaskFailed(str(rec.get("failMsg") or "generation failed"))
            if state == "success":
                break
        result = rec.get("resultJson")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {}
        urls = (result or {}).get("resultUrls") or []
        if not urls:
            raise KieError("finished without a picture URL")
        status, raw = self.http.request("GET", urls[0], None, None, 60)
        if status >= 400 or not raw:
            raise KieError(f"download failed (HTTP {status})")
        return raw

    def credits(self) -> float:
        v = self._json("GET", CREDIT_URL, timeout=15).get("data")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise KieError("kie.ai did not report credits")
        return float(v)
