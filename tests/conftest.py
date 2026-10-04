"""Keep tests hermetic: no network, no tokenizer downloads.

Session-scoped so the guards are active before any module-scoped fixture runs an eval.
"""

import socket
from collections.abc import Iterator

import pytest

_LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}
_real_getaddrinfo = socket.getaddrinfo


def _local_only_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
    name = host.decode() if isinstance(host, bytes) else host
    if name not in _LOCAL_HOSTS:
        raise RuntimeError(f"network access is blocked in tests (lookup of {name!r})")
    return _real_getaddrinfo(host, *args, **kwargs)


async def _estimate_tokens(self: object, text: str) -> int:
    return max(1, len(text) // 4)


@pytest.fixture(autouse=True, scope="session")
def _hermetic() -> Iterator[None]:
    from inspect_ai.model._providers.mockllm import MockLLM

    with pytest.MonkeyPatch.context() as mp:
        # Every SDK resolves a hostname before connecting (sync or asyncio), so refusing
        # non-local lookups blocks outbound calls without breaking local event-loop sockets.
        mp.setattr(socket, "getaddrinfo", _local_only_getaddrinfo)
        # mockllm estimates usage with tiktoken, which downloads its encoding on first use.
        mp.setattr(MockLLM, "count_text_tokens", _estimate_tokens)
        yield
