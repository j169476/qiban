"""Run and check the dedicated Ubuntu/PostgreSQL service locally."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from lab import IMAGE_TAG, ROOT, Lab, build_status_image, run  # noqa: E402


class CoreLab(Lab):
    image = f"qiban-linux-core:{IMAGE_TAG}"
    volumes = (*Lab.volumes, ("-pg", "/var/lib/qiban-pg"))

    def __init__(self, name):
        if not name.startswith("qiban-linux-core-"):
            raise ValueError("Use a qiban-linux-core- prefixed dedicated lab")
        super().__init__(name)

    def control(self, *args):
        return json.loads(self.docker("/opt/core-venv/bin/python", "/opt/qiban/core_control.py",
                                      *args, user="qiban"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="qiban-linux-core-round2")
    sub = parser.add_subparsers(dest="op", required=True)
    for op in ("build", "status", "stop", "restart", "accept", "grant", "revoke", "database", "counters"):
        sub.add_parser(op)
    sub.add_parser("up").add_argument("--port", type=int, default=22452)
    sub.add_parser("send").add_argument("mode", choices=["confirm", "hold", "lost", "interrupt"])
    for op in ("inspect", "replay", "release"):
        sub.add_parser(op).add_argument("message")
    args = parser.parse_args()
    lab = CoreLab(args.name)
    if args.op == "build":
        build_status_image()
        subprocess.run(["docker", "build", "--tag", lab.image, "--build-arg", f"STATUS_IMAGE={Lab.image}",
                        "--file", "platforms/linux/core/Dockerfile", "."], cwd=ROOT, check=True)
        return
    if args.op == "up":
        result = lab.up(args.port)
    elif args.op == "status":
        result = lab.status()
    else:
        lab.require_lab()
        if args.op == "accept":
            from core_acceptance import accept
            result = accept(lab)
        elif args.op == "counters":
            result = json.loads(lab.docker("/opt/core-venv/bin/python", "-c",
                                "import dialogue_store,json; print(json.dumps(dialogue_store.progress()))", user="qiban"))
        elif args.op in ("stop", "restart"):
            result = run("docker", args.op, lab.name).stdout.strip()
            if args.op == "restart":
                result = lab.wait_ready()
        else:
            result = lab.control(args.op, *[getattr(args, key) for key in ("message", "mode") if hasattr(args, key)])
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit((exc.stderr or str(exc)).strip())
