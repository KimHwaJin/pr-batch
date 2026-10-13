"""Print a short-lived service JWT for local Swagger/curl testing.

Mirrors what prj_re would send: HS256, iss=prj_re, aud=pr-batch,
scope=batch:manage, sub=<user>, valid for 5 minutes (the server maximum).
Never use against a shared environment.
"""

import argparse
import os
import time

import jwt

DOCKER_DEFAULT_SECRET = "local-only-insecure-secret-change-me-0123456789"

parser = argparse.ArgumentParser(description=__doc__)
# prj_re user IDs are UUIDs; this fixed one is the local developer.
parser.add_argument("--user", default="00000000-0000-4000-8000-000000000de0")
parser.add_argument(
    "--secret",
    default=os.environ.get("PR_BATCH_AUTH_SECRET", DOCKER_DEFAULT_SECRET),
)
args = parser.parse_args()
now = int(time.time())
claims = {
    "sub": args.user,
    "iss": "prj_re",
    "aud": "pr-batch",
    "scope": "batch:manage",
    "iat": now,
    "exp": now + 300,
}
print(jwt.encode(claims, args.secret, algorithm="HS256"))
