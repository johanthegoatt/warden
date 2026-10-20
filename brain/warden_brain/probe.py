"""The brain's own probe: Python's urllib and socket, a third stack that shares
no code with either sensor path. Used for verification and for confirming a fix."""

from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .store import Target


@dataclass
class Result:
    ok: bool
    status: Optional[int] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return self.error
        if self.status is not None:
            return f"{self.status} in {self.latency_ms:.0f} ms"
        return f"reachable in {self.latency_ms:.0f} ms"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def check(target: Target, timeout: float = 8.0) -> Result:
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    if target.kind == "http":
        expected = target.expected_status or 200
        req = urllib.request.Request(target.endpoint, headers={"User-Agent": "warden-brain/0.1"})
        try:
            with _opener.open(req, timeout=timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception as e:  # urllib raises many unrelated types for network trouble
            return Result(False, None, elapsed(), f"{type(e).__name__}: {e}")
        if status == expected:
            return Result(True, status, elapsed())
        return Result(False, status, elapsed(), f"status {status}, expected {expected}")

    if target.kind == "tcp":
        host, _, port = target.endpoint.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return Result(True, None, elapsed())
        except OSError as e:
            return Result(False, None, elapsed(), f"connect {target.endpoint}: {e}")

    if target.kind == "heartbeat":
        try:
            age = time.time() - os.stat(target.endpoint).st_mtime
        except FileNotFoundError:
            return Result(False, None, elapsed(), f"{target.endpoint} does not exist")
        except OSError as e:
            return Result(False, None, elapsed(), f"cannot stat {target.endpoint}: {e}")
        return Result(True, None, elapsed()) if age <= (target.max_age_secs or 300) else Result(
            False, None, elapsed(), f"last heartbeat {age:.0f}s ago"
        )

    return Result(False, None, None, f"unknown target kind {target.kind!r}")

