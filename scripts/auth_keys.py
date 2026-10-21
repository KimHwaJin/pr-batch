"""Create the RSA key pair for service tokens, unless it already exists.

auth_private.pem goes to every calling service (they sign with it);
auth_public.pem stays with pr-batch (AUTH_PUBLIC_KEY_FILE). With no
argument the pair lands in .local-keys/ for the Docker stack and
`make dev-token`; never use that pair outside your machine.

    python scripts/auth_keys.py [OUT_DIR]
"""

import argparse
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PRIVATE, PUBLIC = "auth_private.pem", "auth_public.pem"

parser = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
)
parser.add_argument("out", nargs="?", type=Path, default=Path(".local-keys"))
args = parser.parse_args()

private_path, public_path = args.out / PRIVATE, args.out / PUBLIC
if private_path.exists() or public_path.exists():
    print(f"Keeping the existing key pair in {args.out}/.")
    raise SystemExit(0)

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
args.out.mkdir(parents=True, exist_ok=True)
private_path.write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
private_path.chmod(0o600)
public_path.write_bytes(
    key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
)
print(f"Wrote {private_path} (callers) and {public_path} (pr-batch).")
