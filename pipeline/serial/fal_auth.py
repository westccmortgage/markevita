"""Shared fal credential handling. Diagnostics never include the secret."""
import re


def normalize_key(value):
    # Clipboard whitespace is not part of a fal API credential. Do not silently
    # remove quotes, header prefixes or internal characters from a bad value.
    return (value or '').strip()


def key_problem(value):
    key = normalize_key(value)
    if not key:
        return 'FAL_KEY is missing.'
    if key.lower().startswith(('key ', 'bearer ', 'authorization:', 'fal_key=')):
        return 'FAL_KEY contains a header or variable prefix. Paste only the full key_id:key_secret value.'
    if any(c in key for c in ('"', "'", '*', '…', '•')):
        return 'FAL_KEY contains quotes or masking characters. Paste the full value copied when the key was created.'
    if any(ord(c) <= 32 or ord(c) >= 127 for c in key):
        return 'FAL_KEY contains whitespace or non-ASCII characters. Copy the complete key again.'
    parts = key.split(':')
    if len(parts) != 2 or not all(parts):
        return 'FAL_KEY must contain both key_id and key_secret separated by a colon. The dashboard ID alone is not a key.'
    return None


def key_id_prefix(value):
    key = normalize_key(value)
    if key_problem(key):
        return None
    identifier = key.split(':', 1)[0]
    # Only display the non-secret, UUID-shaped identifier used by the dashboard.
    if re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', identifier):
        return identifier[:8].lower()
    return None
