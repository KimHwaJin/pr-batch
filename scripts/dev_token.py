"""Print a short-lived service JWT for local Swagger/curl testing.

Mirrors what a calling service sends: RS256 signed with the private key,
iss=batch-client, aud=batch-service, scope=batch:manage, sub=<user>, valid for
5 minutes (the server maximum). The default key is the local pair from
`make auth-keys`; for a deployed environment pass its --private-key and,
if it sets another AUTH_ISSUER or AUTH_AUDIENCE, --issuer/--audience.
"""

import argparse
import os
import time
from pathlib import Path

import jwt

parser = argparse.ArgumentParser(description=__doc__)
# prj_re user IDs are UUIDs; this fixed one is the local developer.
parser.add_argument("--user", default="00000000-0000-4000-8000-000000000de0")
parser.add_argument(
    "--private-key",
    type=Path,
    default=Path(
        os.environ.get(
            "PR_BATCH_AUTH_PRIVATE_KEY_FILE", ".local-keys/auth_private.pem"
        )
    ),
)
parser.add_argument(
    "--issuer",
    default=os.environ.get("PR_BATCH_AUTH_ISSUER", "batch-client"),
)
parser.add_argument(
    "--audience",
    default=os.environ.get("PR_BATCH_AUTH_AUDIENCE", "batch-service"),
)
args = parser.parse_args()
now = int(time.time())
claims = {
    "sub": args.user,
    "iss": args.issuer,
    "aud": args.audience,
    "scope": "batch:manage",
    "iat": now,
    "exp": now + 300,
}
print(jwt.encode(claims, args.private_key.read_bytes(), algorithm="RS256"))
