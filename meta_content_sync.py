#!/usr/bin/env python3
"""Sync prior-day Meta raw data into the Meta content hub on Feishu."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import requests
import yaml

API = "https://open.feishu.cn/open-apis"


class SyncError(RuntimeError):
    pass


def resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {key: resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_env(item) for item in value]
    return value


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        return resolve_env(yaml.safe_load(file))


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(text(item) for item in value)
    if isinstance(value, dict):
        return text(value.get("text") or value.get("name") or value.get("value"))
    return str(value).strip()


def number(value: Any) -> Decimal:
    raw = text(value).replace(",", "").replace("$", "")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise SyncError(f"Cannot parse numeric value: {value!r}") from error


def parse_date(value: Any) -> date:
    raw = text(value)
    if raw.isdigit() and len(raw) >= 10:
        return datetime.fromtimestamp(int(raw) / 1000, tz=ZoneInfo("UTC")).date()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError as error:
            raise SyncError(f"Cannot parse date value: {value!r}") from error


class Feishu:
    def __init__(self, app_id: str, app_secret: str) -> None:
        if not app_id or not app_secret:
            raise SyncError("FEISHU_APP_ID and FEISHU_APP_SECRET must be configured as GitHub Secrets.")
        response = requests.post(
            f"{API}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=30,
        )
        payload = response.json()
        if not response.ok or payload.get("code") not in (0, None):
            raise SyncError(f"Feishu authentication failed: {payload}")
        self.headers = {"Authorization": f"Bearer {payload['tenant_access_token']}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = requests.request(method, f"{API}{path}", headers=self.headers, timeout=45, **kwargs)
        payload = response.json()
        if not response.ok or payload.get("code") not in (0, None):
            raise SyncError(f"Feishu API request failed ({method} {path}): {payload}")
        return payload

    def wiki_app_token(self, wiki_url: str, table_id: str) -> str:
        parsed = urlsplit(wiki_url)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".feishu.cn"):
            raise SyncError(f"Invalid Feishu Wiki URL: {wiki_url}")
        match = re.fullmatch(r"/wiki/([A-Za-z0-9]+)", parsed.path)
        if not match:
            raise SyncError(f"Expected a Feishu Wiki node URL: {wiki_url}")
        url_table = parse_qs(parsed.query).get("table", [""])[0]
        if url_table != table_id:
            raise SyncError(f"Wiki link table ID {url_table!r} differs from configured {table_id!r}.")
        node = self._request("GET", "/wiki/v2/spaces/get_node", params={"token": match.group(1)}).get("data", {}).get("node", {})
        if node.get("obj_type") != "bitable" or not node.get("obj_token"):
            raise SyncError(f"Wiki node does not resolve to a Base (obj_type={node.get('obj_type')!r}).")
        return node["obj_token"]

    def fields(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields").get("data", {}).get("items", [])

    def records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        items, page_token = [], None
        while True:
            params: dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records", params=params).get("data", {})
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            page_token = data.get("page_token")

    def batch_update(self, app_token: str, table_id: str, records: list[dict[str, Any]]) -> None:
        for start in range(0, len(records), 500):
            self._request("POST", f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update", json={"records": records[start:start + 500]})


def required_mappings(config: dict[str, Any]) -> list[str]:
    source = config["source"]["fields"]
    destination = config["destination"]["fields"]
    missing = [f"source.fields.{key}" for key in ("date", "ad_id", "spend", "impressions") if not source.get(key)]
    missing += [f"destination.fields.{key}" for key in ("ad_id", "status", "lifecycle", "cumulative_performance") if not destination.get(key)]
    return missing


def field_report(client: Feishu, label: str, spec: dict[str, Any]) -> None:
    fields = client.fields(spec["app_token"], spec["table_id"])
    print(f"\n{label} fields ({len(fields)}):")
    for item in fields:
        print(f"  - {item['field_name']!r} | id={item['field_id']} | type={item['type']}")


def aggregate(source_records: list[dict[str, Any]], fields: dict[str, str], target: date) -> tuple[dict[str, dict[date, dict[str, Decimal]]], bool]:
    data: dict[str, dict[date, dict[str, Decimal]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    target_seen = False
    for record in source_records:
        row = record.get("fields", {})
        ad_id = text(row.get(fields["ad_id"]))
        if not ad_id:
            continue
        row_date = parse_date(row.get(fields["date"]))
        if row_date > target:
            continue
        if row_date == target:
            target_seen = True
        for metric, column in fields.items():
            if metric not in ("date", "ad_id") and column:
                data[ad_id][row_date][metric] += number(row.get(column))
    return data, target_seen


def lifecycle(ad_days: dict[date, dict[str, Decimal]], target: date, zero_days: int) -> tuple[bool, dict[str, Any]]:
    spends = {day: metrics.get("spend", Decimal("0")) for day, metrics in ad_days.items()}
    paid_days = sorted(day for day, spend in spends.items() if spend > 0)
    recent = [spends.get(target - timedelta(days=offset), Decimal("0")) for offset in range(zero_days)]
    stopped = all(spend == 0 for spend in recent)
    return stopped, {
        "first_date": paid_days[0].isoformat() if paid_days else "",
        "last_date": paid_days[-1].isoformat() if paid_days else "",
        "paid_days": len(paid_days),
        "lifecycle_days": ((target - paid_days[0]).days + 1) if paid_days else 0,
        "stopped_days": (target - paid_days[-1]).days if stopped and paid_days else 0,
    }


def display_decimal(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):,}" if value % 1 else f"{int(value):,}"


def summaries(ad_days: dict[date, dict[str, Decimal]], target: date, life: dict[str, Any]) -> tuple[str, str]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for metrics in ad_days.values():
        for key, value in metrics.items():
            totals[key] += value
    impressions, clicks, spend = totals["impressions"], totals["clicks"], totals["spend"]
    ctr = clicks / impressions * 100 if impressions else Decimal("0")
    cpm = spend / impressions * 1000 if impressions else Decimal("0")
    cpc = spend / clicks if clicks else Decimal("0")
    lifecycle_text = f"首次投放 {life['first_date'] or '—'}｜最近投放 {life['last_date'] or '—'}｜投放 {life['paid_days']} 天｜生命周期 {life['lifecycle_days']} 天"
    performance_text = f"截至 {target.isoformat()}｜花费 ${display_decimal(spend)}｜曝光 {display_decimal(impressions)}｜触达 {display_decimal(totals['reach'])}｜点击 {display_decimal(clicks)}｜CTR {ctr:.2f}%｜CPM ${cpm:.2f}｜CPC ${cpc:.2f}｜购买 {display_decimal(totals['purchases'])}｜销售额 ${display_decimal(totals['purchase_value'])}"
    return lifecycle_text, performance_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config.yml")
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    config = load_config(Path(args.config))
    client = Feishu(config["feishu"]["app_id"], config["feishu"]["app_secret"])
    for label in ("source", "destination"):
        spec = config[label]
        direct_token = os.environ.get(f"FEISHU_{label.upper()}_APP_TOKEN", "").strip()
        spec["app_token"] = direct_token or client.wiki_app_token(spec["wiki_url"], spec["table_id"])
        print(f"{label.upper()}: using {'configured Base token' if direct_token else 'Wiki resolution'}.")

    if args.diagnose or config.get("mode") == "diagnostic":
        field_report(client, "SOURCE", config["source"])
        field_report(client, "DESTINATION", config["destination"])
        print("\nDiagnostic complete. No records were changed.")
        return 0

    missing = required_mappings(config)
    if missing:
        raise SyncError("Missing field mappings: " + ", ".join(missing))
    tz = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    target = datetime.now(tz).date() - timedelta(days=1)
    state_path = Path("state/sync_state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("last_successful_report_date") == target.isoformat():
        print(f"{target}: already completed; exiting without Feishu updates.")
        return 0

    source_rows = client.records(config["source"]["app_token"], config["source"]["table_id"])
    by_ad, target_seen = aggregate(source_rows, config["source"]["fields"], target)
    if not target_seen:
        print(f"{target}: Raw table has no records for the target date; will retry later.")
        return 0
    destination_rows = client.records(config["destination"]["app_token"], config["destination"]["table_id"])
    dest_fields = config["destination"]["fields"]
    destination_by_ad: dict[str, dict[str, Any]] = {}
    for record in destination_rows:
        ad_id = text(record.get("fields", {}).get(dest_fields["ad_id"]))
        if not ad_id:
            continue
        if ad_id in destination_by_ad:
            raise SyncError(f"Destination Ad ID is not unique: {ad_id}")
        destination_by_ad[ad_id] = record

    updates, unmatched = [], []
    stopped, active = config["destination"]["stopped_status"], config["destination"]["active_status"]
    for ad_id, record in destination_by_ad.items():
        fields = record.get("fields", {})
        current_status = text(fields.get(dest_fields["status"]))
        if current_status == stopped:
            continue  # Terminal lock: never re-open and never update its data.
        if current_status not in ("", active):
            continue
        if current_status == "" and not config["rules"]["allow_empty_status_initialization"]:
            continue
        ad_days = by_ad.get(ad_id)
        if not ad_days:
            continue
        is_stopped, life = lifecycle(ad_days, target, config["rules"]["stopped_after_zero_spend_days"])
        if is_stopped:
            updates.append({"record_id": record["record_id"], "fields": {dest_fields["status"]: stopped}})
            continue  # Do not change performance values on the stopping run.
        lifecycle_text, performance_text = summaries(ad_days, target, life)
        write_fields = {dest_fields["status"]: active, dest_fields["lifecycle"]: lifecycle_text, dest_fields["cumulative_performance"]: performance_text}
        if dest_fields.get("last_synced_date"):
            write_fields[dest_fields["last_synced_date"]] = target.isoformat()
        updates.append({"record_id": record["record_id"], "fields": write_fields})

    raw_only = sorted(set(by_ad) - set(destination_by_ad))
    if raw_only:
        print(f"Raw IDs with no destination record ({len(raw_only)}): {', '.join(raw_only[:20])}")
    if updates:
        client.batch_update(config["destination"]["app_token"], config["destination"]["table_id"], updates)
    state_path.write_text(json.dumps({"last_successful_report_date": target.isoformat(), "completed_at": datetime.now(tz).isoformat()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{target}: success. Updated {len(updates)} destination records; skipped terminal stopped records.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
