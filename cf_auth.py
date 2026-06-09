"""cf_auth.py — Cloudflare Access JWT verification.

Copied verbatim from the CDL Registers (ARMR) project so WeaselOMeter uses the
exact same edge-auth pattern.

Configuration (environment variables):
  CF_TEAM_DOMAIN  — your Cloudflare Access team domain
                    e.g. "yourteam.cloudflareaccess.com"
  CF_AUD          — the Application Audience (AUD) tag from the CF Access
                    application settings page (optional but strongly recommended
                    to prevent token reuse across applications)

When CF_TEAM_DOMAIN is unset, is_enabled() returns False and verification is
skipped — handy for local development.
"""
import os
import time
import requests
import jwt

CF_TEAM_DOMAIN: str = os.environ.get("CF_TEAM_DOMAIN", "").strip().rstrip("/")
CF_AUD:         str = os.environ.get("CF_AUD", "").strip()

_CERTS_URL = (
    f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs" if CF_TEAM_DOMAIN else ""
)
_CACHE: dict = {"keys": None, "at": 0.0}
_CACHE_TTL  = 3600  # refresh JWKS at most once per hour


def is_enabled() -> bool:
    """Return True when Cloudflare Access integration is configured."""
    return bool(CF_TEAM_DOMAIN)


def _jwks() -> list:
    """Fetch (and in-process cache) Cloudflare's public JWKS."""
    now = time.monotonic()
    if _CACHE["keys"] is not None and (now - _CACHE["at"]) < _CACHE_TTL:
        return _CACHE["keys"]
    try:
        resp = requests.get(_CERTS_URL, timeout=5)
        resp.raise_for_status()
        keys = resp.json().get("keys", [])
        _CACHE["keys"] = keys
        _CACHE["at"]   = now
        return keys
    except Exception:
        return _CACHE["keys"] or []


def _rsa_public_key_from_jwk(jwk_dict: dict):
    """Build an RSA public key from a JWK dict using the cryptography library directly."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.hazmat.backends import default_backend

    def _b64_to_int(s: str) -> int:
        s += "=" * (-len(s) % 4)
        return int.from_bytes(base64.urlsafe_b64decode(s), "big")

    n = _b64_to_int(jwk_dict["n"])
    e = _b64_to_int(jwk_dict["e"])
    return RSAPublicNumbers(e, n).public_key(default_backend())


def verify_cf_jwt(token: str) -> str | None:
    """Verify a Cloudflare Access JWT and return the authenticated email, or None."""
    if not token or not CF_TEAM_DOMAIN:
        return None

    for key_data in _jwks():
        try:
            public_key = _rsa_public_key_from_jwk(key_data)
            payload    = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=CF_AUD if CF_AUD else None,
                options={"verify_aud": bool(CF_AUD)},
            )
            return payload.get("email")
        except jwt.ExpiredSignatureError:
            return None
        except Exception:
            continue

    return None
