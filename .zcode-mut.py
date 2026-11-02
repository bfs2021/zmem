"""Critic-round boundary mutants, transport-safe (v2: robust suite parse)."""
import subprocess
import sys
from pathlib import Path

BS = chr(92)
sh = Path("scripts/fleet/serve-status-classify.sh")
ps1 = Path("scripts/fleet/serve-status-lib.ps1")


def run_suite():
    r = subprocess.run([sys.executable, "-X", "utf8",
                        "tests/test_fleet_installer_443.py"],
                       capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    if "CHECK-FAIL" in out:
        return "FAILED"
    if "CHECK-PASS" in out:
        return "OK"
    return "NO-OUTPUT rc=" + str(r.returncode)


def restore(path):
    subprocess.run(["git", "checkout", "--", str(path)], check=True)


src = sh.read_text(encoding="utf-8")
old = "([^0-9]|"
assert src.count(old) == 1, "sh boundary anchor"
sh.write_text(src.replace(old, "(|"), encoding="utf-8", newline="\n")
print("MC2 sh-boundary-drop:", run_suite())
restore(sh)

src = ps1.read_text(encoding="utf-8")
old2 = "(" + BS + "D|$)"
assert src.count(old2) == 1, "ps1 boundary anchor"
ps1.write_text(src.replace(old2, "(d" + BS + "$)"), encoding="utf-8", newline="\n")
print("MC3 ps1-boundary-invert:", run_suite())
restore(ps1)

st = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
print("post-probe dirty entries:", len(st.stdout.splitlines()))
