"""Export CIs and relationships for offline CMDB health analysis.

Usage (against a personal developer instance):
    cp .env.example .env && edit .env        # never commit .env
    set -a; source .env; set +a
    python -m snow_toolkit.export_cmdb --class cmdb_ci_server --out reports/
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .client import Config, ServiceNowClient

CI_FIELDS = ["sys_id", "name", "sys_class_name", "serial_number", "ip_address", "os", "owned_by",
             "support_group", "last_discovered", "install_status"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--class", dest="ci_class", default="cmdb_ci_server")
    p.add_argument("--query", default="install_status!=7")
    p.add_argument("--limit", type=int)
    p.add_argument("--out", type=Path, default=Path("reports"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = ServiceNowClient(Config.from_env())
    cis = list(client.iter_records(args.ci_class, args.query, CI_FIELDS, args.limit))
    ids = {c["sys_id"] for c in cis}
    rels = [r for r in client.iter_records("cmdb_rel_ci", "", ["parent", "child", "type"])
            if r.get("parent") in ids or r.get("child") in ids]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "cis.json").write_text(json.dumps({"result": cis}, indent=2))
    (args.out / "rels.json").write_text(json.dumps({"result": rels}, indent=2))
    logging.info("Exported %d CIs and %d relationships to %s", len(cis), len(rels), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
