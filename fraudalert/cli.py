import argparse
import csv
import logging
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fraudalert", description="Credit card fraud alert pipeline")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create tables and seed the default rule")
    sub.add_parser("sync", help="fetch new bank emails over IMAP once")
    w = sub.add_parser("watch", help="sync on an interval (the long-running ingestion worker)")
    w.add_argument("--interval", type=int, default=300, help="seconds between syncs (default 300)")
    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--sync-interval", type=int, default=0, help="also sync the inbox every N seconds")
    i = sub.add_parser("import-eml", help="ingest saved .eml files")
    i.add_argument("paths", nargs="+", type=Path)
    r = sub.add_parser("reevaluate", help="re-score all transactions and re-apply current rules")
    r.add_argument("--reparse", action="store_true", help="also retry emails that failed to parse")
    r.add_argument("--reparse-all", action="store_true",
                   help="re-parse every stored email (after a parser update); keeps fraud/legit labels")
    sub.add_parser("train", help="train the Isolation Forest anomaly model now and re-score everything")
    rp = sub.add_parser("report", help="email the daily report now (covers everything since the last one)")
    rp.add_argument("--test", action="store_true", help="send a [TEST] copy without affecting the daily schedule")
    e = sub.add_parser("export-features", help="write feature vectors + labels to CSV for model training")
    e.add_argument("out", type=Path)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from fraudalert import pipeline
    from fraudalert.db import init_db

    init_db()

    if args.cmd == "init-db":
        print("database ready")
    elif args.cmd == "sync":
        result = pipeline.sync_inbox()
        print(result)
        return 1 if result.errors else 0
    elif args.cmd == "watch":
        while True:
            print(pipeline.sync_inbox(), flush=True)
            try:
                if pipeline.maybe_retrain():
                    print("retrained the anomaly model", flush=True)
            except Exception:  # noqa: BLE001 - a training problem must not stop the worker
                logging.exception("anomaly model retraining failed")
            try:
                from fraudalert.report import maybe_send_daily_report

                if maybe_send_daily_report():
                    print("sent the daily report", flush=True)
            except Exception:  # noqa: BLE001 - a mail problem must not stop the worker (retried next loop)
                logging.exception("daily report failed")
            time.sleep(args.interval)
    elif args.cmd == "report":
        from fraudalert.report import send_report

        r = send_report(test=args.test)
        print(f"sent: {r.transactions} transaction(s), {r.unacknowledged} unacknowledged alarm(s)")
    elif args.cmd == "train":
        from fraudalert.anomaly.training import NotEnoughData

        try:
            info = pipeline.retrain_anomaly_model()
        except NotEnoughData as exc:
            print(exc, file=sys.stderr)
            return 1
        m = info["metrics"]
        print(f"trained on {info['n_samples']} transactions; "
              f"AUC (fraud vs legit) iforest={m['auc_iforest']} baseline={m['auc_baseline']}; "
              f"{m['alarms_30d_at_threshold']} of {m['transactions_30d']} recent transactions score >= {m['alarm_threshold']}")
    elif args.cmd == "serve":
        import threading

        import uvicorn

        from fraudalert.config import get_settings
        from fraudalert.web.app import create_app

        problem = exposure_problem(get_settings(), args.host, in_container=Path("/.dockerenv").exists())
        if problem:
            print(problem, file=sys.stderr)
            return 2

        if args.sync_interval:
            def loop() -> None:
                while True:
                    pipeline.sync_inbox()
                    time.sleep(args.sync_interval)

            threading.Thread(target=loop, daemon=True).start()
        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.cmd == "import-eml":
        files = [p for path in args.paths for p in (sorted(path.glob("*.eml")) if path.is_dir() else [path])]
        print(pipeline.import_eml_files(files))
    elif args.cmd == "reevaluate":
        mode = "all" if args.reparse_all else "failed" if args.reparse else "none"
        print(pipeline.reevaluate_all(reparse=mode))
    elif args.cmd == "export-features":
        export_features(args.out)
    return 0


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def exposure_problem(settings, host: str, in_container: bool) -> str | None:
    """Refuse to serve transactions to the network without a password.

    In a container the server always listens on 0.0.0.0 and docker decides exposure from
    FRAUDALERT_WEB_BIND; outside one, --host decides.
    """
    exposed = settings.web_bind not in LOOPBACK if in_container else host not in LOOPBACK
    if exposed and not settings.auth_enabled:
        return (
            "Refusing to start: the web UI would be reachable from other machines without a password.\n"
            "Set FRAUDALERT_WEB_USERNAME and FRAUDALERT_WEB_PASSWORD in .env, "
            "or keep FRAUDALERT_WEB_BIND=127.0.0.1 / --host 127.0.0.1."
        )
    return None


def export_features(out: Path) -> None:
    from sqlalchemy import select

    from fraudalert.anomaly.features import FEATURE_NAMES, to_vector
    from fraudalert.db import session_scope
    from fraudalert.models import Transaction

    with session_scope() as s, out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "occurred_at", *FEATURE_NAMES, "anomaly_score", "flagged", "label_fraud"])
        n = 0
        for t in s.scalars(select(Transaction).where(Transaction.features.is_not(None)).order_by(Transaction.occurred_at)):
            label = "" if t.label_fraud is None else int(t.label_fraud)
            writer.writerow([t.id, t.occurred_at.isoformat(), *to_vector(t.features), t.anomaly_score, int(t.flagged), label])
            n += 1
    print(f"wrote {n} rows to {out}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
