"""Readiness probe for scripts/start.sh (design.md §6 Scripts).

A port that accepts connections is not proof that OUR server is up: a leftover server from
another run or another config may hold it. The probe asks GET /info and accepts only the
pid, id and n that start.sh expects.

usage: python -m kvstore.probe HOST PORT PID ID N
exit 0: ready; 1: nothing listening yet (keep waiting); 2: a different process answers.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


class NotListening(Exception):
    """Nothing accepts connections on the port yet."""


def check(host: str, port: int, pid: int, server_id: int, n: int, timeout: float = 0.5) -> str | None:
    """None if the expected server answers, else a message describing what answered."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/info", timeout=timeout) as r:
            info = json.load(r)
    except urllib.error.HTTPError as exc:
        # Something speaks HTTP but has no /info: an older kvserver or not a kvserver at all.
        return f"{host}:{port} answers but has no /info (HTTP {exc.code}): not an identifiable kvserver"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            raise NotListening from exc
        return f"{host}:{port} did not answer /info: {exc.reason}"
    except (ConnectionRefusedError, ConnectionResetError) as exc:
        raise NotListening from exc
    except (ValueError, OSError) as exc:
        return f"{host}:{port} gave an unreadable /info: {exc}"
    got = (info.get("pid"), info.get("id"), info.get("n"))
    if got != (pid, server_id, n):
        return (f"{host}:{port} is served by pid {got[0]} (id={got[1]} n={got[2]}), "
                f"expected pid {pid} (id={server_id} n={n})")
    return None


def main(argv: list[str]) -> int:
    host, port, pid, server_id, n = argv[0], int(argv[1]), int(argv[2]), int(argv[3]), int(argv[4])
    try:
        msg = check(host, port, pid, server_id, n)
    except NotListening:
        return 1
    if msg:
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
