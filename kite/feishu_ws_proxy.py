"""Feishu/Lark websocket proxy policy.

Only the public Feishu websocket ingress is controlled here. Local loopback
websockets used by kap-server keep their own explicit ``proxy=None`` behavior.

Direct port of FOCUS ``bot/feishu_ws_proxy.py`` with one upstream-drift
adaptation: lark-oapi <= 1.4.x forced ``{"proxy": None}`` through a private
``_ws_connect_kwargs`` hook in ``lark_oapi.ws.client``; lark-oapi >= 1.5
removed that hook and calls ``websockets.connect(url)`` with no proxy kwarg,
which already defers to the websockets default (``proxy=True``, i.e. honor
environment proxies). Both SDK shapes are handled below; anything else fails
closed for ``env`` mode (a proxy user must not silently lose connectivity).
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable

logger = logging.getLogger(__name__)

FEISHU_WS_PROXY_ENV = "env"
FEISHU_WS_PROXY_DISABLED = "disabled"
DEFAULT_FEISHU_WS_PROXY = FEISHU_WS_PROXY_ENV

_SUPPORTED_MODES = {FEISHU_WS_PROXY_ENV, FEISHU_WS_PROXY_DISABLED}


def normalize_feishu_ws_proxy_mode(value: object) -> str:
    mode = str(value or DEFAULT_FEISHU_WS_PROXY).strip().lower()
    if not mode:
        return DEFAULT_FEISHU_WS_PROXY
    if mode not in _SUPPORTED_MODES:
        raise ValueError("feishu_ws_proxy only supports 'env' or 'disabled'")
    return mode


def configure_feishu_ws_proxy(mode: object) -> str:
    """Apply the process-local Feishu websocket proxy policy.

    ``env`` makes the Feishu websocket honor environment proxy variables;
    ``disabled`` forces a direct connection. Returns the normalized mode.
    """

    normalized = normalize_feishu_ws_proxy_mode(mode)
    try:
        import lark_oapi.ws.client as ws_client
    except Exception:
        logger.warning("cannot import lark_oapi.ws.client; cannot apply Feishu websocket proxy policy", exc_info=True)
        if normalized == FEISHU_WS_PROXY_ENV:
            raise RuntimeError("cannot apply feishu_ws_proxy=env: lark_oapi.ws.client unavailable")
        return normalized

    if hasattr(ws_client, "_ws_connect_kwargs"):
        return _configure_via_connect_kwargs_hook(ws_client, normalized)
    return _configure_via_module_shim(ws_client, normalized)


def _configure_via_connect_kwargs_hook(ws_client: object, normalized: str) -> str:
    """lark-oapi <= 1.4.x shape: override the private connect-kwargs hook.

    The SDK disables websockets' environment proxy discovery by returning
    ``{"proxy": None}`` from its private helper. ``env`` restores normal
    websockets behavior by returning no explicit proxy argument.
    """

    if not hasattr(ws_client, "websockets"):
        message = "this lark_oapi version exposes no websockets module hook; cannot apply Feishu websocket proxy policy"
        logger.warning(message)
        if normalized == FEISHU_WS_PROXY_ENV:
            raise RuntimeError(f"cannot apply feishu_ws_proxy=env: {message}")
        return normalized

    def _env_ws_connect_kwargs() -> dict:
        return {}

    def _disabled_ws_connect_kwargs() -> dict:
        params = inspect.signature(ws_client.websockets.connect).parameters
        if "proxy" in params:
            return {"proxy": None}
        return {}

    replacement: Callable[[], dict]
    if normalized == FEISHU_WS_PROXY_ENV:
        replacement = _env_ws_connect_kwargs
    else:
        replacement = _disabled_ws_connect_kwargs
    ws_client._ws_connect_kwargs = replacement
    logger.info("Feishu websocket proxy mode: %s", normalized)
    return normalized


class _DisabledProxyWebsocketsShim:
    """Module stand-in for ``lark_oapi.ws.client.websockets``.

    Delegates every attribute to the real websockets module except
    ``connect``, which defaults ``proxy`` to ``None`` (direct connection).
    Rebinding the reference inside ``lark_oapi.ws.client`` only affects the
    Feishu websocket client; the global websockets package is untouched.
    """

    def __init__(self, real_module: object) -> None:
        self._real_module = real_module

    def __getattr__(self, name: str) -> object:
        return getattr(self._real_module, name)

    def connect(self, uri: str, **kwargs: object) -> object:
        kwargs.setdefault("proxy", None)
        return self._real_module.connect(uri, **kwargs)


def _configure_via_module_shim(ws_client: object, normalized: str) -> str:
    """lark-oapi >= 1.5 shape: the SDK passes no proxy kwarg at all.

    With no explicit proxy argument, websockets defaults to ``proxy=True``
    (honor environment), which is exactly the ``env`` contract — nothing to
    restore. ``disabled`` rebinds the module reference inside the SDK's ws
    client to a shim that forces ``proxy=None``.
    """

    if normalized == FEISHU_WS_PROXY_ENV:
        logger.info("Feishu websocket proxy mode: env (lark_oapi default already honors environment proxies)")
        return normalized

    real_websockets = getattr(ws_client, "websockets", None)
    if isinstance(real_websockets, _DisabledProxyWebsocketsShim):
        return normalized
    if real_websockets is None or not hasattr(real_websockets, "connect"):
        logger.warning(
            "this lark_oapi version exposes no websockets.connect hook; feishu_ws_proxy=disabled not applied"
        )
        return normalized
    ws_client.websockets = _DisabledProxyWebsocketsShim(real_websockets)
    logger.info("Feishu websocket proxy mode: %s", normalized)
    return normalized
