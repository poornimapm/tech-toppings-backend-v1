"""Print a new VAPID key pair for Web Push (ADR-0018), as lines for ``.env``.

Usage: ``poe vapid-keys``. Paste both lines from the same run (startup checks they fit). Keep the
private key secret (an env var, never the repository). Changing the keys later drops every browser
subscribed with the old ones: each turns notifications on again.
"""

from __future__ import annotations

from app.platform.push.webpush import new_key_pair


def main() -> int:
    public, private = new_key_pair()
    print(f"PUSH_VAPID_PUBLIC_KEY={public}")
    print(f"PUSH_VAPID_PRIVATE_KEY={private}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
