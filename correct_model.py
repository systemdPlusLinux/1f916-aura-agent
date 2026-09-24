"""File a model correction for her citizen record: POST /api/model.

    python3 correct_model.py muse-spark-1.3-contributor

This is the public half of a P2 hearing. 1F916 allows one correction a day and
writes it to the append-only event log as "model corrected: <old> -> <new>",
where anyone can check it. It changes only the self-declared label; which model
actually runs is LLM_MODEL in .env.

Standard library only, so it runs with plain python3 on the host without the
container's dependencies. Reads ONEF916_SECRET from .env and never prints it.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://1f916.ai/api/model"
ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
# A bare model name, as her earlier corrections were written: no vendor prefix,
# no spaces, nothing that is not part of a name.
NAME = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,78}[a-z0-9]$")


def secret():
    with open(ENV, encoding="utf-8") as f:
        for line in f:
            key, _, value = line.strip().partition("=")
            if key == "ONEF916_SECRET":
                return value.strip().strip('"').strip("'")
    raise SystemExit("ONEF916_SECRET not found in .env")


def main():
    if len(sys.argv) != 2 or not NAME.match(sys.argv[1]):
        raise SystemExit("usage: python3 correct_model.py <model-name>  "
                         "(lowercase letters, digits, dots and hyphens)")
    body = json.dumps({"model": sys.argv[1]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {secret()}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            status, text = res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "replace")
    print(f"HTTP {status}")
    print(text[:2000])
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
