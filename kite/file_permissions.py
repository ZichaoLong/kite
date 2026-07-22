"""
Sensitive file permission helpers.

This repo stores multiple local files that may contain credentials or local
control tokens. On Unix-like systems we tighten those files to ``0600``.
Windows does not offer POSIX mode semantics here, so we fall back to the
current user's profile isolation and NTFS ACLs, with an explicit warning.
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading

from kite.platform_paths import is_windows

_WINDOWS_PERMISSION_WARNING = (
    "警告: Windows 不承诺 POSIX 0600 语义；KITE 的敏感配置/令牌文件将依赖当前用户目录"
    "与 NTFS ACL 保护。请确保 KITE_CONFIG_ROOT / KITE_DATA_ROOT 位于当前用户私有路径下，而不是共享目录。"
)
_warn_lock = threading.Lock()
_warned_windows_acl_fallback = False


def ensure_private_file_permissions(path: pathlib.Path | str) -> None:
    resolved = pathlib.Path(path)
    if is_windows():
        _warn_windows_acl_fallback_once()
        return
    os.chmod(resolved, 0o600)


def _warn_windows_acl_fallback_once() -> None:
    global _warned_windows_acl_fallback
    with _warn_lock:
        if _warned_windows_acl_fallback:
            return
        print(_WINDOWS_PERMISSION_WARNING, file=sys.stderr)
        _warned_windows_acl_fallback = True
