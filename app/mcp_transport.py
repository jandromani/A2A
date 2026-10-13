import base64
import re

CURRENT_PROTOCOL_VERSION = "2026-07-28"
BASE64_PREFIX = "=?base64?"
BASE64_SUFFIX = "?="


def encode_header_value(value: str) -> str:
    encoded = value.encode("utf-8")
    safe = all((character == "\t" or " " <= character <= "~") for character in value)
    sentinel = value.startswith(BASE64_PREFIX) and value.endswith(BASE64_SUFFIX)
    if safe and value == value.strip(" \t") and not sentinel:
        return value
    return f"{BASE64_PREFIX}{base64.b64encode(encoded).decode('ascii')}{BASE64_SUFFIX}"


def decode_header_value(value: str) -> str:
    if value.startswith(BASE64_PREFIX) and value.endswith(BASE64_SUFFIX):
        payload = value[len(BASE64_PREFIX) : -len(BASE64_SUFFIX)]
        try:
            decoded = base64.b64decode(payload, validate=True)
            return decoded.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Malformed MCP base64 header value.") from exc
    if not re.fullmatch(r"[\x20-\x7e\t]*", value):
        raise ValueError("MCP header value must be visible ASCII or base64 encoded.")
    if value != value.strip(" \t"):
        raise ValueError("MCP header value with edge whitespace must be base64 encoded.")
    return value
