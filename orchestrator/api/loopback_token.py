"""The one source of the loopback token, shared by the server launcher and the MCP adapter.

Both read ``CCTV_PIPELINE_TOKEN`` through ``read_token`` so they cannot disagree about it.
No message raised or defined here ever contains the token itself.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

TOKEN_ENV = "CCTV_PIPELINE_TOKEN"

#: Deliberately free of the token's value, so it is safe to emit anywhere.
INVALID_TOKEN_MESSAGE = (
    f"${TOKEN_ENV} must contain only visible ASCII (0x21-0x7E): no spaces, line breaks or "
    "non-ASCII characters"
)


class TokenError(Exception):
    """The token is missing, malformed, or exposed on the command line.

    Its message never contains the token.
    """


def is_valid_token(token: str) -> bool:
    # http.client rejects a header value holding CR/LF with a ValueError that quotes the
    # whole value, so an unchecked token with a trailing newline would leak into the error
    # sent back to the client. Refusing it up front means that error cannot arise.
    return bool(token) and all("\x21" <= char <= "\x7e" for char in token)


def redact(text: str, token: str) -> str:
    """``text`` with every occurrence of ``token`` replaced, for diagnostics quoting user input.

    A settings or path error quotes what the user wrote, and a token pasted into the wrong
    place in settings.toml would otherwise be printed back.
    """
    return text.replace(token, "<redacted token>") if token else text


def read_token(argv: Sequence[str]) -> str:
    """Return the token from the environment, refusing one that is missing, malformed or in ``argv``."""
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise TokenError(f"{TOKEN_ENV} is not set; export the orchestrator's loopback token.")
    if not is_valid_token(token):
        raise TokenError(INVALID_TOKEN_MESSAGE)
    # Checked by callers before parsing, because a parse or settings error would quote the argument.
    if any(token in argument for argument in argv):
        raise TokenError(f"the token must not be passed on the command line; set ${TOKEN_ENV} instead.")
    return token
