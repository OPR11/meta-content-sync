# Meta Content Sync

Daily Feishu synchronization for the Meta content hub. It reads **US-META 26 Ad Daily Raw**, matches records by unique Ad ID, and updates only the configured lifecycle and cumulative-performance fields in **META内容中台源表**.

## Business rules implemented

- Runs at **05:00, 10:00, 15:00, 20:00 Asia/Shanghai**.
- Every run targets the previous calendar day; current-day raw data is never included.
- Once a report date succeeds, `state/sync_state.json` prevents the remaining runs that day from writing again.
- An active ad whose most recent two natural days each have spend `0` (a missing day counts as `0`) becomes `已停投`.
- `已停投` is a terminal program state: it is never restored to `投放中`, and no lifecycle or performance data is updated afterwards.
- When an ad becomes `已停投`, that run changes its status only; its accumulated performance is frozen at the prior successful active run.
- Raw rows are grouped by `Ad ID + Date` before they are accumulated. This safely handles daily breakdown rows.
- Raw IDs with no content-hub record are reported but never create a new content record.

## Initial setup

1. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret | Value |
   | --- | --- |
   | `FEISHU_APP_ID` | Feishu self-built application App ID |
   | `FEISHU_APP_SECRET` | Feishu self-built application App Secret |
   | `FEISHU_SOURCE_APP_TOKEN` | Base token of META 数据看板 |
   | `FEISHU_DESTINATION_APP_TOKEN` | Base token of Meta内容中台 |

2. Add the Feishu app as a collaborator of both Bases, with read access to Raw and read/write access to the destination table.
3. Run **Actions → Meta content sync → Run workflow** once. The repository starts in `diagnostic` mode: it prints each table's field names, IDs, and types and changes no records.
4. Copy the verified field names into `config/config.yml`. Keep lifecycle and cumulative-performance destination fields as text fields.
5. Change `mode: diagnostic` to `mode: sync`, commit the configuration, and use **Run workflow** for the first live run.

## Required field mapping

Configure the exact Feishu field names in `config/config.yml`:

- Raw: `date`, `ad_id`, `spend`, `impressions`; optional `reach`, `clicks`, `purchases`, `purchase_value`.
- Content hub: `ad_id`, `status`, `lifecycle`, `cumulative_performance`; optional `last_synced_date`.

`source.table_id` is prefilled for **US-META 26 Ad Daily Raw** and `destination.table_id` for **META内容中台源表**. Confirm both with the diagnostic output before live sync.

## Manual recovery

- To re-enable an intentionally restarted ad, manually change its status in Feishu to `投放中`. The next successful run can update it again.
- To reprocess a report date, edit `state/sync_state.json` and clear or replace `last_successful_report_date`, then run the workflow manually.
- The workflow commits only changes to `state/sync_state.json`; credentials are never stored in the repository.
