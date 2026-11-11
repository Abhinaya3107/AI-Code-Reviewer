import hashlib
import hmac


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Checks GitHub's X-Hub-Signature-256 header against the RAW request body (parsing it first would
    change the bytes). Uses a constant-time comparison so response timing leaks nothing about the secret."""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)
