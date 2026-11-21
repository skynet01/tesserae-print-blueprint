"""PrusaLink GET authentication, adapted from dmellok/tesserae-widget-prusalink 0.1.3.

AGPL-3.0-only. Digest (MD5, MD5-sess, SHA-256) and password-as-key fallback
match the reference widget. Redirects never carry credentials to another host.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _digest_challenge(headers: Any) -> dict[str, str] | None:
    """The first Digest challenge in a 401's WWW-Authenticate headers."""
    values = headers.get_all("WWW-Authenticate") if headers is not None else None
    for value in values or []:
        match = re.search(r"(?i)\bdigest\s+(.*)", value)
        if not match:
            continue
        return {
            key.lower(): quoted if quoted else bare
            for key, quoted, bare in re.findall(
                r'(\w+)=(?:"([^"]*)"|([^\s,]+))', match.group(1)
            )
        }
    return None


def _digest_header(
    challenge: dict[str, str], *, method: str, uri: str, username: str, password: str
) -> str | None:
    """Build the Authorization header answering ``challenge``, or None when
    the printer asks for an algorithm this code doesn't have."""
    stated = challenge.get("algorithm") or "MD5"
    algorithm = stated.upper()
    family = algorithm.removesuffix("-SESS")
    if family == "MD5":
        hasher = hashlib.md5
    elif family == "SHA-256":
        hasher = hashlib.sha256
    else:
        return None

    def h(text: str) -> str:
        return hasher(text.encode("utf-8")).hexdigest()

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    cnonce = secrets.token_hex(8)
    nc = "00000001"
    ha1 = h(f"{username}:{realm}:{password}")
    if algorithm.endswith("-SESS"):
        ha1 = h(f"{ha1}:{nonce}:{cnonce}")
    ha2 = h(f"{method}:{uri}")

    offered = [
        q.strip().lower() for q in challenge.get("qop", "").split(",") if q.strip()
    ]
    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        # Echoed exactly as the printer wrote it: PrusaLink sends "MD5-sess"
        # and compares the answer letter for letter.
        f"algorithm={stated}",
    ]
    if "auth" in offered:
        response = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    elif offered:
        return None  # only auth-int on offer; nobody ships that for a GET API
    else:
        response = h(f"{ha1}:{nonce}:{ha2}")
    parts.append(f'response="{response}"')
    if challenge.get("opaque"):
        parts.append(f'opaque="{challenge["opaque"]}"')
    return "Digest " + ", ".join(parts)


class AuthError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, settings: dict, budget: float = 6.0):
        base = str(settings.get("prusalink_url") or "").strip().rstrip("/")
        if not base:
            raise ValueError(
                "Set the printer address in the PrusaLink widget settings."
            )
        if "://" not in base:
            base = "http://" + base
        parsed = urllib.parse.urlsplit(base)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use an HTTP or HTTPS printer address without credentials or a query."
            )
        self.base = base
        self.username = str(settings.get("prusalink_username") or "maker").strip()
        self.password = str(
            settings.get("prusalink_password_secret")
            or settings.get("prusalink_password")
            or ""
        ).strip()
        self.key = str(
            settings.get("prusalink_api_key_secret")
            or settings.get("prusalink_api_key")
            or ""
        ).strip()
        if not self.password and not self.key:
            raise ValueError(
                "Set the PrusaLink password or API key in the widget settings."
            )
        self.deadline = time.monotonic() + budget
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _try(self, path, headers, limit):
        left = self.deadline - time.monotonic()
        if left <= 0.05:
            raise TimeoutError("PrusaLink request budget exhausted")
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            with self.opener.open(request, timeout=min(6, left)) as response:
                if response.status == 204:
                    return True, b"", None
                data = bytearray()
                while True:
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("PrusaLink request budget exhausted")
                    chunk = response.read1(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError("PrusaLink response is too large")
                if response.length not in (None, 0):
                    raise ValueError("Incomplete PrusaLink response")
                return True, bytes(data), None
        except urllib.error.HTTPError as err:
            challenge = _digest_challenge(err.headers) if err.code == 401 else None
            code = err.code
            err.close()
            if code in (401, 403):
                return False, None, challenge
            raise OSError(f"PrusaLink HTTP {code}") from None

    def get(self, path: str, limit: int = 512_000) -> bytes:
        # References are local paths only, including /usb/... downloads on Buddy firmware.
        u = urllib.parse.urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or u.scheme
            or u.netloc
            or "\\" in path
        ):
            raise ValueError("PrusaLink returned a file URL outside this printer")
        path = urllib.parse.quote(path, safe="/%?&=:@!$'()*+,;~-._")
        headers = {
            "User-Agent": "tesserae/print_blueprint",
            "Accept-Encoding": "identity",
        }
        if self.key:
            ok, body, _ = self._try(path, {**headers, "X-Api-Key": self.key}, limit)
            if ok:
                return body
            if not self.password:
                raise AuthError(
                    "PrusaLink refused the API key. Check the widget settings."
                )
        ok, body, challenge = self._try(path, headers, limit)
        if ok:
            return body
        if challenge:
            auth = _digest_header(
                challenge,
                method="GET",
                uri=path,
                username=self.username,
                password=self.password,
            )
            if auth:
                ok, body, _ = self._try(path, {**headers, "Authorization": auth}, limit)
                if ok:
                    return body
        ok, body, _ = self._try(path, {**headers, "X-Api-Key": self.password}, limit)
        if ok:
            return body
        raise AuthError(
            "PrusaLink refused the sign-in. Check the username, password or API key."
        )

    def json(self, path):
        raw = self.get(path)
        value = json.loads(raw) if raw else {}
        if not isinstance(value, dict):
            raise ValueError("That address answered, but not as a PrusaLink printer.")
        return value
