"""Guards on the callback-prefix registry."""
from __future__ import annotations

from bot import callback_prefixes


def test_callback_prefixes_unique():
    """Two constants sharing one string would route a tap to the wrong handler."""
    values = {name: v for name, v in vars(callback_prefixes).items()
              if name.isupper() and isinstance(v, str)}
    assert values, "registry unexpectedly empty"
    dupes = sorted(n for n, v in values.items()
                   if list(values.values()).count(v) > 1)
    assert not dupes, f"constants sharing a prefix string: {dupes}"


def test_hub_prefixes_registered():
    assert callback_prefixes.HUB_OPEN == "hubo"
    assert callback_prefixes.HUB_RETRY == "hubry"
    assert callback_prefixes.ACCOUNT_LINK == "acclk"
    assert callback_prefixes.ACCOUNT_UNLINK == "accul"
