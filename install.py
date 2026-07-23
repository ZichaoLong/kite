#!/usr/bin/env python3
"""
Bootstrap installer for local KITE development checkouts.

The script prepares the managed virtualenv, installs the `kite` package into
it, writes the user-bin wrapper(s), and registers the OS service definition
WITHOUT starting it (docs/architecture/kite-design.md §9). Starting the
daemon and enabling autostart are explicit later steps (`kitectl service`).
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import venv

_DEFAULT_PIP_EXTRA_INDEX_URL = "https://pypi.org/simple"


def _ensure_supported_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或更高版本。")


def _venv_python_path(venv_dir: pathlib.Path) -> pathlib.Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_cfg_path(venv_dir: pathlib.Path) -> pathlib.Path:
    return venv_dir / "pyvenv.cfg"


def _venv_is_complete(venv_dir: pathlib.Path) -> bool:
    return _venv_cfg_path(venv_dir).exists() and _venv_python_path(venv_dir).exists()


def _recreate_venv(venv_dir: pathlib.Path) -> None:
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True).create(venv_dir)


def _run_checked(command: list[str], **kwargs) -> None:
    subprocess.run(command, check=True, **kwargs)


def _run_pip_install(venv_python: pathlib.Path, *args: str) -> None:
    command = [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", *args]
    try:
        _run_checked(command)
        return
    except subprocess.CalledProcessError:
        if os.environ.get("PIP_EXTRA_INDEX_URL"):
            raise
        fallback_command = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--extra-index-url",
            _DEFAULT_PIP_EXTRA_INDEX_URL,
            *args,
        ]
        print(
            "pip install 失败，正在使用官方 PyPI 额外源重试一次："
            f" {_DEFAULT_PIP_EXTRA_INDEX_URL}",
            file=sys.stderr,
        )
        _run_checked(fallback_command)


def _venv_has_pip(venv_python: pathlib.Path) -> bool:
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "--version"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _ensure_venv_pip(venv_python: pathlib.Path) -> None:
    if _venv_has_pip(venv_python):
        return
    try:
        _run_checked([str(venv_python), "-m", "ensurepip", "--upgrade"])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "当前 Python 无法在受管 .venv 中引导 pip；"
            "请确认已安装该 Python 对应的 venv/ensurepip 组件，"
            "或删除受管 .venv 后重试。"
        ) from exc
    if not _venv_has_pip(venv_python):
        raise SystemExit("已尝试使用 ensurepip 修复受管 .venv，但其中仍然缺少 pip。")


def _verify_installed_package(venv_python: pathlib.Path, venv_dir: pathlib.Path) -> None:
    # Run from the venv directory so `import kite` resolves the installed
    # package, not the repository checkout next to this script.
    _run_checked([str(venv_python), "-c", "import kite"], cwd=str(venv_dir))


def _write_wrappers(venv_dir: pathlib.Path, bin_dir: pathlib.Path) -> list[pathlib.Path]:
    """User-bin wrapper scripts for the admin surface (POSIX only; ~FOCUS)."""
    if os.name == "nt":
        return []
    bin_dir.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    for name in ("kitectl",):
        target = bin_dir / name
        target.write_text(
            f'#!/bin/sh\nexec "{venv_dir}/bin/{name}" "$@"\n', encoding="utf-8"
        )
        target.chmod(0o755)
        written.append(target)
    return written


def _register_service(venv_dir: pathlib.Path) -> bool:
    """Write the OS service definition via kitectl (never started here; §9).

    A platform without a supported service manager must not fail the whole
    install: warn loudly and let the caller continue with the venv + wrapper.
    """
    kitectl = venv_dir / ("Scripts/kitectl.exe" if os.name == "nt" else "bin/kitectl")
    result = subprocess.run(
        [str(kitectl), "service", "install"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(
            "WARNING: service definition was NOT written "
            f"(kitectl service install exited {result.returncode}): {detail}",
            file=sys.stderr,
        )
        return False
    return True


def _print_next_steps(
    venv_dir: pathlib.Path,
    venv_python: pathlib.Path,
    wrappers: list[pathlib.Path],
    service_written: bool,
) -> None:
    print()
    print("KITE install complete.")
    print(f"  managed venv : {venv_dir}")
    print(f"  interpreter  : {venv_python}")
    print("  verified     : `import kite` succeeds inside the managed venv")
    wrapper_text = " ".join(str(path) for path in wrappers) if wrappers else "(none)"
    print(f"  wrappers     : {wrapper_text}")
    if service_written:
        print("  service      : definition written, NOT started")
        print("                 (docs/architecture/kite-design.md §9)")
    else:
        print("  service      : NOT written (see warning above)")
    print()
    print("Next steps:")
    print("  - Fill in ~/.config/kite/system.yaml (see config/system.yaml.example).")
    print("  - Provider credentials for the service environment go into the")
    print("    env file (default ~/.config/kite/env, KITE_ENV_FILE to override).")
    print("  - Start the daemon:   kitectl service start")
    print("  - Inspect:            kitectl service status / log")
    print("  - Start on login:     kitectl service autostart enable")


def main(argv: list[str] | None = None) -> None:
    args = [] if argv is None else list(argv)
    if args:
        raise SystemExit(f"install.py 不接受任何参数：{' '.join(args)}")
    _ensure_supported_python()
    install_dir = pathlib.Path(__file__).resolve().parent
    from kite.platform_paths import default_user_bin_dir
    from kite.service_manager import managed_venv_dir

    venv_dir = managed_venv_dir()
    if not _venv_is_complete(venv_dir):
        _recreate_venv(venv_dir)
    venv_python = _venv_python_path(venv_dir)
    if not venv_python.exists():
        raise SystemExit(f"受管 .venv 不完整，缺少解释器：{venv_python}")
    _ensure_venv_pip(venv_python)
    _run_pip_install(venv_python, "setuptools>=68", "wheel")
    _run_pip_install(venv_python, "--no-build-isolation", str(install_dir))
    _verify_installed_package(venv_python, venv_dir)
    wrappers = _write_wrappers(venv_dir, default_user_bin_dir())
    service_written = _register_service(venv_dir)
    _print_next_steps(venv_dir, venv_python, wrappers, service_written)


if __name__ == "__main__":
    main(sys.argv[1:])
