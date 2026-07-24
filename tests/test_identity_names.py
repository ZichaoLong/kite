"""IdentityNames display-name cache contract tests."""

from __future__ import annotations

import unittest

from kite.identity_names import IdentityNames


class IdentityNamesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1000.0]
        self.calls: list[str] = []

    def _resolver(self, name: str | None = "张三", *, fail: bool = False) -> IdentityNames:
        def fetch(open_id: str) -> str | None:
            self.calls.append(open_id)
            if fail:
                raise RuntimeError("contact api down")
            return name

        return IdentityNames(fetch, ttl_seconds=60, negative_ttl_seconds=10, clock=lambda: self.now[0])

    def test_resolves_and_caches(self) -> None:
        names = self._resolver()
        self.assertEqual(names.name_of("ou_a"), "张三")
        self.assertEqual(names.name_of("ou_a"), "张三")
        self.assertEqual(self.calls, ["ou_a"])  # one upstream call, then cache

    def test_ttl_expiry_refetches(self) -> None:
        names = self._resolver()
        names.name_of("ou_a")
        self.now[0] += 61
        names.name_of("ou_a")
        self.assertEqual(self.calls, ["ou_a", "ou_a"])

    def test_failure_falls_back_and_negative_caches(self) -> None:
        names = self._resolver(fail=True)
        first = names.name_of("ou_abcdefgh12345")
        self.assertTrue(first.startswith("ou_abcdefgh"))
        self.assertIn("…", first)
        names.name_of("ou_abcdefgh12345")
        self.assertEqual(self.calls, ["ou_abcdefgh12345"])  # negative-cached

    def test_none_result_falls_back(self) -> None:
        names = self._resolver(name=None)
        self.assertNotEqual(names.name_of("ou_a"), "None")

    def test_empty_open_id(self) -> None:
        names = self._resolver()
        self.assertEqual(names.name_of(""), "未知用户")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
