"""Small, dependency-free HTTP helpers for radar source fetchers."""

import http.client
import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional, Tuple


class SourceError(RuntimeError):
    """A source could not be reached or returned an unusable response."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_GITHUB_TOKEN_LOADED = False
_GITHUB_TOKEN = ""


def github_token() -> str:
    """Return the current environment token or a cached gh token safely."""

    global _GITHUB_TOKEN_LOADED, _GITHUB_TOKEN
    environment_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if environment_token:
        return environment_token

    if _GITHUB_TOKEN_LOADED:
        return _GITHUB_TOKEN

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        _GITHUB_TOKEN_LOADED = True
        return _GITHUB_TOKEN

    if result.returncode != 0:
        _GITHUB_TOKEN_LOADED = True
        return _GITHUB_TOKEN
    _GITHUB_TOKEN = result.stdout.strip()
    _GITHUB_TOKEN_LOADED = True
    return _GITHUB_TOKEN


def _request_headers(
    headers: Optional[Dict[str, str]],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    if headers is None:
        return {}, {}
    if not isinstance(headers, Mapping):
        raise SourceError("HTTP request failed")

    regular: Dict[str, str] = {}
    unredirected: Dict[str, str] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise SourceError("HTTP request failed")
        if name.lower() == "authorization":
            unredirected[name] = value
        else:
            regular[name] = value
    return regular, unredirected


def get_text(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
) -> str:
    """Fetch UTF text and normalize transport failures to ``SourceError``."""

    try:
        regular_headers, unredirected_headers = _request_headers(headers)
        request = urllib.request.Request(url, headers=regular_headers)
        for name, value in unredirected_headers.items():
            request.add_unredirected_header(name, value)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except SourceError:
        raise
    except urllib.error.HTTPError as exc:
        raise SourceError(
            f"HTTP request failed with status {exc.code}", status_code=exc.code
        ) from None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        http.client.HTTPException,
    ):
        raise SourceError("HTTP request failed") from None

    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        raise SourceError("HTTP response was not valid text") from None


def get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20,
) -> Any:
    """Fetch JSON and normalize invalid payloads to ``SourceError``."""

    text = get_text(url, headers=headers, timeout=timeout)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise SourceError("HTTP response was not valid JSON") from None
