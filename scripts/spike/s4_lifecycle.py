#!/usr/bin/env python3
"""S4 — managed subprocess full lifecycle.

Checks:
  4.1 port+1 retry when 58627 is occupied
  4.2 token file existence + permission bits (0600 file / 0700 dir)
  4.3 `kimi web rotate-token` hot reload (old rejected, new accepted)
  4.4 SIGTERM graceful shutdown (exit code, instance deregistration)
  4.5 kill -9 leftovers: stale instance entry, sweep on next launch, fresh launch works
  4.6 POST /api/v1/shutdown on loopback

No model calls required.
"""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kap


def main() -> None:
    kap.log("=== S4: managed subprocess lifecycle ===")
    results: list[str] = []

    # ------------------------------------------------------------------ 4.1
    kap.log("--- 4.1 port+1 retry ---")
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", kap.DEFAULT_PORT))
    blocker.listen(1)
    srv = None
    try:
        srv = kap.Server(port_requested=kap.DEFAULT_PORT).launch()
        home1 = srv.home
        kap.obs(
            "4.1 port occupied -> +1 retry",
            f"requested {kap.DEFAULT_PORT} (occupied), binds {kap.DEFAULT_PORT + 1}",
            f"bound port {srv.port}; instance registry: {srv.instance_files()}",
            "PASS" if srv.port == kap.DEFAULT_PORT + 1 else "NEEDS-DESIGN-ADJUSTMENT",
        )
        results.append(f"4.1 {'PASS' if srv.port == kap.DEFAULT_PORT + 1 else 'FAIL'}")

        # -------------------------------------------------------------- 4.2
        kap.log("--- 4.2 token file + permission bits ---")
        token_path = os.path.join(home1, "server.token")
        st = os.stat(token_path)
        fmode = stat.S_IMODE(st.st_mode)
        dmode = stat.S_IMODE(os.stat(home1).st_mode)
        tok_len = len(open(token_path).read().strip())
        kap.obs(
            "4.2 token file",
            "server.token exists, 43-char base64url, file 0600, home dir 0700",
            f"exists=True len={tok_len} file_mode={oct(fmode)} home_mode={oct(dmode)}",
            "PASS" if (fmode == 0o600 and tok_len == 43) else "CHECK",
        )
        results.append(f"4.2 {'PASS' if fmode == 0o600 and tok_len == 43 else 'FAIL'}")

        # -------------------------------------------------------------- 4.3
        kap.log("--- 4.3 rotate-token hot reload ---")
        old_token = srv.token
        rest = srv.rest()
        before = rest.get("/meta")
        env = kap.child_env(home1)
        rot = subprocess.run([kap.KIMI_BIN, "web", "rotate-token"], env=env,
                             capture_output=True, text=True, timeout=30)
        new_token = open(token_path).read().strip()
        time.sleep(0.5)  # let the mtime/inode check trip on next auth
        old_rej = rest.get("/meta", token=old_token)
        new_ok = rest.get("/meta", token=new_token)
        kap.obs(
            "4.3 rotate-token",
            "rotate rewrites file; running server rejects old token, accepts new without restart",
            f"rotate rc={rot.returncode}; token changed={old_token != new_token}; "
            f"old_token /meta code={old_rej.get('code')} http={old_rej.get('http')}; "
            f"new_token /meta code={new_ok.get('code')}",
            "PASS" if (old_token != new_token and old_rej.get("code") != 0
                       and new_ok.get("code") == 0) else "CHECK",
        )
        results.append("4.3 PASS" if old_token != new_token and new_ok.get("code") == 0
                       and old_rej.get("code") != 0 else "4.3 FAIL")
        srv.token = new_token

        # -------------------------------------------------------------- 4.4
        kap.log("--- 4.4 SIGTERM graceful shutdown ---")
        rc = srv.terminate(grace=15)
        leftovers = srv.instance_files()
        kap.obs(
            "4.4 SIGTERM",
            "graceful exit, instance registry entry removed",
            f"returncode={rc} (negative means signal: {-rc if rc and rc < 0 else 'no'}), "
            f"instance files after exit: {leftovers}",
            "PASS" if leftovers == [] else "CHECK",
        )
        results.append("4.4 PASS" if leftovers == [] else "4.4 FAIL")
        srv.proc = None
    finally:
        blocker.close()
        if srv is not None and srv.proc is not None:
            srv.stop()

    # ------------------------------------------------------------------ 4.5
    kap.log("--- 4.5 kill -9 leftovers ---")
    srv2 = kap.Server().launch()  # default port now free
    home2 = srv2.home
    try:
        kap.log(f"second server on port {srv2.port}, home {home2}")
        dead_pid = srv2.proc.pid
        srv2.kill9()
        stale = srv2.read_instances()
        kap.obs(
            "4.5a kill -9 leftover",
            "instance entry remains on disk after SIGKILL (no deregistration possible)",
            f"entries after kill -9: {[(i['server_id'][:8], i['pid'], i['port']) for i in stale]}",
            "PASS" if len(stale) == 1 else "CHECK",
        )
        # Fresh launch against the SAME home: stale sweep happens on register.
        srv3 = kap.Server(home=home2).launch()
        try:
            after = srv3.read_instances()
            stale_gone = all(i["pid"] != dead_pid for i in after)
            meta = srv3.rest().get("/meta")
            kap.obs(
                "4.5b fresh launch with stale entry present",
                "launch succeeds; stale dead-pid entry swept lazily on register; registry shows only live instances",
                f"new port={srv3.port}; entries={[(i['server_id'][:8], i['pid'], i['port']) for i in after]}; "
                f"stale_pid_swept={stale_gone}; /meta code={meta.get('code')}",
                "PASS" if (stale_gone and meta.get("code") == 0) else "CHECK",
            )
            results.append("4.5 PASS" if stale_gone and meta.get("code") == 0 else "4.5 FAIL")

            # ---------------------------------------------------------- 4.6
            kap.log("--- 4.6 POST /api/v1/shutdown ---")
            r = srv3.rest().post("/shutdown")
            try:
                rc = srv3.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                rc = "TIMEOUT"
                srv3.kill9()
            leftovers = srv3.instance_files()
            kap.obs(
                "4.6 shutdown route",
                "loopback POST /api/v1/shutdown -> envelope {ok:true}, process exits, deregisters",
                f"response code={r.get('code')} data={r.get('data')} http={r.get('http')}; "
                f"proc rc={rc}; instance files: {leftovers}",
                "PASS" if (r.get("code") == 0 and rc == 0 and leftovers == []) else "CHECK",
            )
            results.append("4.6 PASS" if r.get("code") == 0 and rc == 0 else "4.6 FAIL")
            srv3.proc = None
        finally:
            if srv3.proc is not None:
                srv3.stop()
    finally:
        srv2.stop()

    kap.log("=== S4 summary: " + ", ".join(results) + " ===")


if __name__ == "__main__":
    main()
