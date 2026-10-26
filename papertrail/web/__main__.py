"""Run the local PaperTrail web interface."""
import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .app import make_server


def main(argv=None):
    parser = argparse.ArgumentParser(description="PaperTrail local web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--db", default=None)
    parser.add_argument("--demo", action="store_true", help="Try sample letters offline with recorded responses")
    args = parser.parse_args(argv)
    temporary = TemporaryDirectory(prefix="papertrail-demo-") if args.demo and args.db is None else None
    db_path = Path(args.db) if args.db else (Path(temporary.name) / "cases.sqlite3" if temporary else Path(os.environ.get("DATA_DIR", "./data")) / "cases.sqlite3")
    try:
        server = make_server(args.host, args.port, db_path=db_path, demo=args.demo,
                             data_dir=Path(temporary.name) if temporary else os.environ.get("DATA_DIR", str(Path(args.db).parent) if args.db else "./data"))
    except BaseException:
        if temporary:
            temporary.cleanup()
        raise
    print(f"PaperTrail: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if temporary:
            temporary.cleanup()


if __name__ == "__main__":
    main()
