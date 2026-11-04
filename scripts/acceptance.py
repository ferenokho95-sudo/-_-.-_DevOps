"""Run real deployment and failure tests; save logs without reading credentials.

Run from the project root in Ubuntu. These checks cause short app outages.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
steps = [
    ("deploy-all", ["deploy-all"]),
    ("grafana-repeat", ["configure-grafana"]),
    ("persistence", ["persistence-check"]),
    ("kuma-crash", ["chaos", "--target=kuma", "--mode=crash"]),
    ("db-crash", ["chaos", "--target=db", "--mode=crash"]),
    ("db-stop", ["chaos", "--target=db", "--mode=stop"]),
    ("final-validation", ["validate"]),
]

def main():
    if sys.argv[1:] not in ([], ["--resume"]):
        raise SystemExit("Usage: python3 scripts/acceptance.py [--resume]")
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    summary = {"started_utc": datetime.now(timezone.utc).isoformat(), "steps": []}
    if "--resume" in sys.argv and (evidence / "summary.json").exists():
        summary = json.loads((evidence / "summary.json").read_text())
        summary["steps"] = [row for row in summary["steps"] if row["exit_code"] == 0]
    for name, args in steps:
        if any(row["step"] == name for row in summary["steps"]):
            print(f"ALREADY PASSED {name}", flush=True)
            continue
        print(f"START {name}", flush=True)
        started = time.monotonic()
        command = ["dagger", "--progress=plain", "call", *args,
                   "--socket=/var/run/docker.sock", "--credentials=file:.env"]
        with (evidence / (name + ".log")).open("w") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        row = {"step": name, "exit_code": result.returncode,
               "seconds": round(time.monotonic() - started, 2)}
        plain = re.sub(r"\x1b\[[0-9;]*m", "", (evidence / (name + ".log")).read_text())
        for match in re.finditer(r"(?m)^\{", plain):
            try:
                payload, _ = json.JSONDecoder().raw_decode(plain[match.start():])
            except ValueError:
                continue
            if "evidence" in payload or "persistence" in payload:
                row["observations"] = payload
        summary["steps"].append(row)
        summary["updated_utc"] = datetime.now(timezone.utc).isoformat()
        (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(row), flush=True)
        if result.returncode:
            print(f"FAILED: see evidence/{name}.log", flush=True)
            return result.returncode
    print("ALL ACCEPTANCE CHECKS PASSED", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
