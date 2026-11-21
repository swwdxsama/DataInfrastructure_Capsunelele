"""Exporta batch-uri brute din DynamoDB (RawBatches) ca fisiere JSON.

  pip install boto3
  python tools/export_batches.py --device ios-01 --label masa --from 2026-11-21T10:00:00Z --to 2026-11-21T10:01:00Z
  python tools/export_batches.py --device ios-01 --label masa --limit 5      # ultimele 5
  python tools/export_batches.py --clean exports/raw exports/clean            # task 4: ruleaza preprocess offline

Scrie exports/raw/<label>/<device>_<t0>.json (exact payload-ul trimis de telefon, cu received_at_ms separat).
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src", "backend"))


def _ms(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def export(args):
    import boto3
    from boto3.dynamodb.conditions import Key
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    cond = Key("device_id").eq(args.device)
    if args.from_ and args.to:
        cond = cond & Key("t0").between(_ms(args.from_), _ms(args.to))
    q = dict(KeyConditionExpression=cond, ScanIndexForward=not (args.limit and not args.from_))
    items = []
    while True:
        res = table.query(**q)
        items += res["Items"]
        if args.limit and len(items) >= args.limit or "LastEvaluatedKey" not in res:
            break
        q["ExclusiveStartKey"] = res["LastEvaluatedKey"]
    if args.limit:
        items = items[: args.limit]
    out_dir = os.path.join(args.out, args.label)
    os.makedirs(out_dir, exist_ok=True)
    for it in items:
        doc = {"label": args.label, "received_at_ms": int(it["received_at"]), "payload": json.loads(it["raw"])}
        path = os.path.join(out_dir, "%s_%d.json" % (it["device_id"], int(it["t0"])))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
    print("wrote %d batches to %s" % (len(items), out_dir))


def clean(src, dst):
    from preprocess import preprocess
    n = 0
    for root, _, files in os.walk(src):
        for name in files:
            if not name.endswith(".json"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as f:
                doc = json.load(f)
            res = preprocess(doc["payload"], doc["received_at_ms"], {"MAX_BATCH_AGE_SEC": float("inf")})
            rel = os.path.relpath(root, src)
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            with open(os.path.join(dst, rel, name), "w", encoding="utf-8") as f:
                json.dump(res, f, indent=1)
            print(os.path.join(rel, name), res["status"], res.get("reason") or res.get("warnings"))
            n += 1
    print("processed %d files" % n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", nargs=2, metavar=("SRC", "DST"))
    ap.add_argument("--device")
    ap.add_argument("--label", default="unlabeled")
    ap.add_argument("--from", dest="from_")
    ap.add_argument("--to")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--table", default="RawBatches")
    ap.add_argument("--region", default="eu-central-1")
    ap.add_argument("--out", default="exports/raw")
    a = ap.parse_args()
    if a.clean:
        clean(*a.clean)
    elif a.device:
        export(a)
    else:
        ap.error("--device or --clean required")
