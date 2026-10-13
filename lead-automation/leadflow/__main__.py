"""CLI:  python -m leadflow run | serve | reset"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
import uuid

from .config import Config
from .llm import Triager
from .notify import Notifier
from .pipeline import Pipeline
from .server import serve
from .store import Store


def build(cfg: Config) -> Pipeline:
    store = Store(cfg)
    return Pipeline(cfg, store, Triager(cfg), Notifier(cfg), run_id=uuid.uuid4().hex[:8])


def post_lead(url: str, payload, secret: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Secret"] = secret
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def cmd_run(args, cfg: Config) -> int:
    try:
        leads = json.loads(cfg.incoming_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read incoming leads from {cfg.incoming_json}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(leads, list):
        print("incoming file must be a JSON array of submissions", file=sys.stderr)
        return 2

    pipeline = None if args.via_http else build(cfg)
    mode = pipeline.triager.mode if pipeline else f"webhook {args.via_http}"
    print(f"Processing {len(leads)} leads  [triage: {mode}]\n")
    print(f"{'ID':<7}{'OUTCOME':<11}{'TAG':<6}{'SHEET ROW':<11}DETAIL")
    results = []
    for raw in leads:
        try:
            res = pipeline.process(raw).as_dict() if pipeline else post_lead(args.via_http, raw, cfg.webhook_secret)
        except Exception as exc:  # webhook unreachable etc.
            res = {"source_id": str(raw.get("id", "?")) if isinstance(raw, dict) else "?", "outcome": "error",
                   "lead_id": "", "tag": "", "detail": f"delivery failed: {exc}", "notes": []}
        results.append(res)
        extra = f" [{'; '.join(res['notes'])}]" if res.get("notes") else ""
        print(f"{res['source_id']:<7}{res['outcome']:<11}{res.get('tag',''):<6}{res.get('lead_id',''):<11}{res['detail'][:90]}{extra}")

    counts: dict = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    if pipeline:
        n = pipeline.store.export_review_queue()
        print(f"\nSummary: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}")
        print(f"Sheet:        {pipeline.store.sheet_path}  ({len(pipeline.store.rows)} rows)")
        print(f"Review queue: {pipeline.store.review_path}  ({n} entries)")
    else:
        print(f"\nSummary: {', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}  (server keeps out/review_queue.csv current)")
    return 1 if counts.get("error") else 0


def cmd_serve(args, cfg: Config) -> int:
    serve(build(cfg), cfg.webhook_secret, args.port)
    return 0


def cmd_reset(args, cfg: Config) -> int:
    if cfg.out_dir.exists():
        shutil.rmtree(cfg.out_dir)
    print(f"Removed {cfg.out_dir}. Next run starts again from {cfg.existing_csv}.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leadflow", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="process data/incoming_leads.json")
    p_run.add_argument("--reset", action="store_true", help="wipe out/ first (clean demo run)")
    p_run.add_argument("--via-http", metavar="URL", help="POST each lead to a running `serve` instead of in-process")
    p_run.add_argument("--input", help="override incoming leads file")
    p_serve = sub.add_parser("serve", help="run the /lead webhook")
    p_serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("reset", help="delete out/ (sheet, audit log)")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if getattr(args, "input", None):
        from pathlib import Path
        cfg.incoming_json = Path(args.input)
    if args.cmd == "run" and args.reset:
        cmd_reset(args, cfg)
    return {"run": cmd_run, "serve": cmd_serve, "reset": cmd_reset}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
