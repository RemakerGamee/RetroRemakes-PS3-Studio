# RetroRemakes PS3 Studio
# Copyright (c) 2026 RetroRemakes. All rights reserved.
# Licensed as described in LICENSE.txt; third-party credits are in NOTICE.md.
import argparse
import json

from .core import Studio


def main():
    ap = argparse.ArgumentParser(description="RetroRemakes PS3 Port Studio")
    ap.add_argument("--home", help="Workspace directory")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("doctor")
    sub.add_parser("setup")
    sub.add_parser("list")
    for cmd in ("add", "inspect", "build", "lift"):
        p = sub.add_parser(cmd)
        p.add_argument("source", help="Extracted game folder or decrypted PPU ELF")
        if cmd in ("build", "lift"):
            p.add_argument("--backend", help="Existing ps3recomp checkout")
    args = ap.parse_args()
    if not args.command:
        from .gui import App
        App(args.home).mainloop()
        return
    studio = Studio(args.home)
    if args.command == "doctor":
        print(json.dumps(studio.doctor(), indent=2))
    elif args.command == "setup":
        studio.setup_backend()
    elif args.command == "list":
        print(json.dumps(studio.state["projects"], indent=2))
    else:
        if getattr(args, "backend", None):
            studio.state["settings"]["backend"] = args.backend
        p = studio.add(args.source)
        if args.command == "inspect":
            print(json.dumps(studio.analyse(p), indent=2))
        elif args.command in ("build", "lift"):
            studio.build(p, stop_after="lift" if args.command == "lift" else "build")
        else:
            print(p["id"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as e:
        raise SystemExit(str(e))
