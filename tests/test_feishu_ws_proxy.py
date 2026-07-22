import inspect
import unittest

import lark_oapi.ws.client as lark_ws_client

from kite.feishu_ws_proxy import (
    _DisabledProxyWebsocketsShim,
    configure_feishu_ws_proxy,
    normalize_feishu_ws_proxy_mode,
)


class NormalizeFeishuWsProxyModeTests(unittest.TestCase):
    def test_defaults_to_env(self) -> None:
        self.assertEqual(normalize_feishu_ws_proxy_mode(None), "env")
        self.assertEqual(normalize_feishu_ws_proxy_mode(""), "env")
        self.assertEqual(normalize_feishu_ws_proxy_mode("  "), "env")

    def test_supported_modes_case_insensitive(self) -> None:
        self.assertEqual(normalize_feishu_ws_proxy_mode("ENV"), "env")
        self.assertEqual(normalize_feishu_ws_proxy_mode("Disabled"), "disabled")

    def test_unsupported_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            normalize_feishu_ws_proxy_mode("bogus")


class ConfigureFeishuWsProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._had_hook = hasattr(lark_ws_client, "_ws_connect_kwargs")
        self._original_hook = getattr(lark_ws_client, "_ws_connect_kwargs", None)
        self._original_websockets = lark_ws_client.websockets
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._had_hook:
            lark_ws_client._ws_connect_kwargs = self._original_hook
        elif hasattr(lark_ws_client, "_ws_connect_kwargs"):
            del lark_ws_client._ws_connect_kwargs
        lark_ws_client.websockets = self._original_websockets

    def _install_legacy_hook(self) -> None:
        # lark-oapi <= 1.4.x shape: private hook forcing {"proxy": None}.
        lark_ws_client._ws_connect_kwargs = lambda: {"proxy": None}

    def test_env_mode_on_installed_sdk_is_noop_success(self) -> None:
        # lark-oapi >= 1.5 passes no proxy kwarg, so websockets already honors
        # environment proxies by default; env must not fail closed here.
        self.assertEqual(configure_feishu_ws_proxy("env"), "env")

    def test_disabled_mode_installs_scoped_shim(self) -> None:
        self.assertEqual(configure_feishu_ws_proxy("disabled"), "disabled")
        shim = lark_ws_client.websockets
        self.assertIsInstance(shim, _DisabledProxyWebsocketsShim)
        # Non-connect attributes still delegate to the real module.
        self.assertIs(shim.serve, self._original_websockets.serve)

        captured: dict = {}

        class _FakeModule:
            def connect(self, uri: str, **kwargs: object) -> str:
                captured.update(kwargs)
                return "conn"

        shim._real_module = _FakeModule()
        self.assertEqual(shim.connect("wss://example.invalid/ws"), "conn")
        self.assertIsNone(captured["proxy"])

    def test_disabled_mode_shim_is_idempotent(self) -> None:
        configure_feishu_ws_proxy("disabled")
        first = lark_ws_client.websockets
        configure_feishu_ws_proxy("disabled")
        self.assertIs(lark_ws_client.websockets, first)

    def test_legacy_hook_env_restores_empty_kwargs(self) -> None:
        self._install_legacy_hook()

        self.assertEqual(configure_feishu_ws_proxy("env"), "env")

        self.assertEqual(lark_ws_client._ws_connect_kwargs(), {})

    def test_legacy_hook_disabled_forces_proxy_none(self) -> None:
        self._install_legacy_hook()

        self.assertEqual(configure_feishu_ws_proxy("disabled"), "disabled")

        kwargs = lark_ws_client._ws_connect_kwargs()
        if "proxy" in inspect.signature(lark_ws_client.websockets.connect).parameters:
            self.assertEqual(kwargs, {"proxy": None})
        else:
            self.assertEqual(kwargs, {})

    def test_legacy_hook_without_websockets_module_fails_closed_for_env(self) -> None:
        self._install_legacy_hook()
        saved = lark_ws_client.websockets
        del lark_ws_client.websockets
        try:
            with self.assertRaises(RuntimeError):
                configure_feishu_ws_proxy("env")
            # disabled only warns (FOCUS's asymmetry: env is strict).
            self.assertEqual(configure_feishu_ws_proxy("disabled"), "disabled")
        finally:
            lark_ws_client.websockets = saved


if __name__ == "__main__":
    unittest.main()
