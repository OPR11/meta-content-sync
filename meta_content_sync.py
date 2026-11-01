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


def parse_date(value: Any, timezone_name: str = "Asia/Shanghai") -> date:
    raw = text(value)
    if raw.isdigit() and len(raw) >= 10:
        return datetime.fromtimestamp(
            int(raw) / 1000, tz=ZoneInfo(timezone_name)
        ).date()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(timezone_name))
        return parsed.date()
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
    missing = [
        f"source.fields.{key}"
        for key in (
            "date", "ad_id", "spend", "impressions", "reach", "clicks",
            "purchases", "purchase_value",
        )
        if not source.get(key)
    ]
    missing += [
        f"destination.fields.{key}"
        for key in (
            "ad_id", "status", "end_date", "spend", "purchases", "roas",
            "ctr", "frequency", "impressions", "clicks",
        )
        if not destination.get(key)
    ]
    return missing


def field_report(client: Feishu, label: str, spec: dict[str, Any]) -> None:
    fields = client.fields(spec["app_token"], spec["table_id"])
    print(f"\n{label} fields ({len(fields)}):")
    for item in fields:
        print(f"  - {item['field_name']!r} | id={item['field_id']} | type={item['type']}")


def aggregate(
    source_records: list[dict[str, Any]],
    fields: dict[str, str],
    target: date,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[dict[str, dict[date, dict[str, Decimal]]], set[date]]:
    data: dict[str, dict[date, dict[str, Decimal]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    dates_seen: set[date] = set()
    for record in source_records:
        row = record.get("fields", {})
        ad_id = text(row.get(fields["ad_id"]))
        if not ad_id:
            continue
        row_date = parse_date(row.get(fields["date"]), timezone_name)
        if row_date > target:
            continue
        dates_seen.add(row_date)
        parsed: dict[str, Decimal] = {}
        for metric, column in fields.items():
            if metric not in ("date", "ad_id") and column:
                parsed[metric] = number(row.get(column))
        for metric, value in parsed.items():
            if metric != "video_avg_watch_time_seconds":
                data[ad_id][row_date][metric] += value
        if "video_avg_watch_time_seconds" in parsed:
            data[ad_id][row_date]["video_watch_time_weighted"] += (
                parsed["video_avg_watch_time_seconds"]
                * parsed.get("video_plays", Decimal("0"))
            )
    return data, dates_seen


def explicit_zero_spend_days(
    ad_days: dict[date, dict[str, Decimal]], target: date, zero_days: int
) -> bool:
    days = [target - timedelta(days=offset) for offset in range(zero_days)]
    return all(
        day in ad_days and ad_days[day].get("spend", Decimal("0")) == 0
        for day in days
    )


def cumulative_totals(
    ad_days: dict[date, dict[str, Decimal]], target: date
) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for day, metrics in ad_days.items():
        if day > target:
            continue
        for key, value in metrics.items():
            totals[key] += value
    return totals


def last_delivery_date(
    ad_days: dict[date, dict[str, Decimal]], target: date
) -> date | None:
    delivered = [
        day for day, metrics in ad_days.items()
        if day <= target and (
            metrics.get("spend", Decimal("0")) > 0
            or metrics.get("impressions", Decimal("0")) > 0
        )
    ]
    return max(delivered) if delivered else None


def feishu_date_value(day: date, timezone_name: str) -> int:
    return int(datetime.combine(day, datetime.min.time(), ZoneInfo(timezone_name)).timestamp() * 1000)


def metric_update_fields(
    totals: dict[str, Decimal],
    source_fields: dict[str, str],
    destination_fields: dict[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def write(metric: str, value: Decimal) -> None:
        destination = destination_fields.get(metric)
        if destination:
            result[destination] = float(value)

    if source_fields.get("spend"):
        write("spend", totals["spend"])
    if source_fields.get("purchases"):
        write("purchases", totals["purchases"])
    if source_fields.get("impressions"):
        write("impressions", totals["impressions"])
    if source_fields.get("clicks"):
        write("clicks", totals["clicks"])
    if source_fields.get("purchase_value") and source_fields.get("spend"):
        write("roas", totals["purchase_value"] / totals["spend"] if totals["spend"] else Decimal("0"))
    if source_fields.get("clicks") and source_fields.get("impressions"):
        write("ctr", totals["clicks"] / totals["impressions"] if totals["impressions"] else Decimal("0"))
    if source_fields.get("reach") and source_fields.get("impressions"):
        write("frequency", totals["impressions"] / totals["reach"] if totals["reach"] else Decimal("0"))
    if source_fields.get("video_3s_views") and source_fields.get("impressions"):
        write("video_3s_rate", totals["video_3s_views"] / totals["impressions"] if totals["impressions"] else Decimal("0"))
    if source_fields.get("likes"):
        write("likes", totals["likes"])
    if source_fields.get("comments"):
        write("comments", totals["comments"])
    if source_fields.get("video_avg_watch_time_seconds") and source_fields.get("video_plays"):
        weighted = totals["video_watch_time_weighted"]
        write("video_avg_watch_time_seconds", weighted / totals["video_plays"] if totals["video_plays"] else Decimal("0"))
    return result


def build_update_plan(
    source_records: list[dict[str, Any]],
    destination_records: list[dict[str, Any]],
    source_fields: dict[str, str],
    destination_fields: dict[str, str],
    target: date,
    *,
    active_status: str,
    stopped_status: str,
    zero_days: int,
    timezone_name: str,
) -> dict[str, Any]:
    """Plan destination changes without performing any Feishu writes."""
    by_ad, complete_source_dates = aggregate(
        source_records, source_fields, target, timezone_name
    )
    updates = []
    skipped_not_active = 0
    skipped_missing_target = []
    inferred_zero_days = []
    for record in destination_records:
        fields = record.get("fields", {})
        ad_id = text(fields.get(destination_fields["ad_id"]))
        if text(fields.get(destination_fields["status"])) != active_status:
            skipped_not_active += 1
            continue
        history = by_ad.get(ad_id)
        if not history:
            skipped_missing_target.append(ad_id)
            continue
        ad_days = {day: dict(metrics) for day, metrics in history.items()}
        first_history_date = min(ad_days)
        inferred_dates = []
        for offset in range(zero_days - 1, -1, -1):
            day = target - timedelta(days=offset)
            if (
                day >= first_history_date
                and day in complete_source_dates
                and day not in ad_days
            ):
                ad_days[day] = {}
                inferred_dates.append(day)
        if target not in ad_days:
            skipped_missing_target.append(ad_id)
            continue
        if inferred_dates:
            inferred_zero_days.append({"ad_id": ad_id, "dates": inferred_dates})
        totals = cumulative_totals(ad_days, target)
        write_fields = metric_update_fields(totals, source_fields, destination_fields)
        if explicit_zero_spend_days(ad_days, target, zero_days):
            write_fields[destination_fields["status"]] = stopped_status
            ended = last_delivery_date(ad_days, target)
            if destination_fields.get("end_date"):
                write_fields[destination_fields["end_date"]] = (
                    feishu_date_value(ended, timezone_name) if ended else None
                )
        elif destination_fields.get("end_date"):
            write_fields[destination_fields["end_date"]] = None
        if destination_fields.get("last_synced_date"):
            write_fields[destination_fields["last_synced_date"]] = (
                feishu_date_value(target, timezone_name)
            )
        updates.append({"record_id": record["record_id"], "fields": write_fields})
    return {
        "updates": updates,
        "skipped_not_active": skipped_not_active,
        "skipped_missing_target": skipped_missing_target,
        "inferred_zero_days": inferred_zero_days,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
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
    if (
        not args.dry_run
        and state.get("last_successful_report_date") == target.isoformat()
    ):
        print(f"{target}: already completed; exiting without Feishu updates.")
        return 0

    source_rows = client.records(config["source"]["app_token"], config["source"]["table_id"])
    by_ad, complete_source_dates = aggregate(
        source_rows, config["source"]["fields"], target, tz.key
    )
    if target not in complete_source_dates:
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

    plan = build_update_plan(
        source_rows,
        destination_rows,
        config["source"]["fields"],
        dest_fields,
        target,
        active_status=config["destination"]["active_status"],
        stopped_status=config["destination"]["stopped_status"],
        zero_days=config["rules"]["stopped_after_zero_spend_days"],
        timezone_name=tz.key,
    )
    updates = plan["updates"]

    raw_only = sorted(set(by_ad) - set(destination_by_ad))
    if raw_only:
        print(f"Raw IDs with no destination record ({len(raw_only)}): {', '.join(raw_only[:20])}")
    if plan["skipped_missing_target"]:
        missing_target = plan["skipped_missing_target"]
        print(
            f"Active destination ads with no {target} Raw row "
            f"({len(missing_target)}): {', '.join(missing_target[:20])}"
        )
    if plan["inferred_zero_days"]:
        inferred = plan["inferred_zero_days"]
        preview = "; ".join(
            f"{item['ad_id']}: {', '.join(day.isoformat() for day in item['dates'])}"
            for item in inferred[:20]
        )
        print(
            "Active destination ads with inferred zero-spend Raw dates "
            f"({len(inferred)}): {preview}"
        )
    paused_count = sum(
        item["fields"].get(dest_fields["status"])
        == config["destination"]["stopped_status"]
        for item in updates
    )
    if args.dry_run:
        print(
            f"{target}: dry run complete. Would update {len(updates)} running ads; "
            f"would pause {paused_count}; "
            "no Feishu records or sync state were changed."
        )
        return 0
    if updates:
        client.batch_update(config["destination"]["app_token"], config["destination"]["table_id"], updates)
    state_path.write_text(json.dumps({"last_successful_report_date": target.isoformat(), "completed_at": datetime.now(tz).isoformat()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"{target}: success. Updated {len(updates)} running ads; "
        f"paused {paused_count}; skipped non-running {plan['skipped_not_active']}; "
        f"skipped missing target-day data {len(plan['skipped_missing_target'])}."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SyncError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
