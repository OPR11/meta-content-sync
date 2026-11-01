# Meta Content Sync

Daily Feishu synchronization for the Meta content hub. It reads **US-META 26 Ad Daily Raw**, matches records by unique Ad ID, and updates the configured cumulative performance columns for records whose material status is **正在投放** in **META内容中台源表**.

## Business rules implemented

- Runs at **05:00, 10:00, 15:00, 20:00 Asia/Shanghai**.
- Every run targets the previous calendar day; current-day raw data is never included.
- Once a report date succeeds, `state/sync_state.json` prevents the remaining runs that day from writing again.
- Only destination records whose status is exactly `正在投放` are processed. Blank, paused, or other statuses are never updated.
- The target date must exist in the Raw table before any ad is processed; otherwise the run exits and retries later.
- For an active destination ad that already has Raw history, a missing row on or after its first Raw date is inferred as an explicit zero-delivery day when that date is present elsewhere in the Raw table. Earlier dates, dates absent from the whole Raw table, and ads with no Raw history remain unknown.
- An active ad becomes `已暂停` only when both of the most recent two natural days are confirmed complete and each day's spend is explicitly or safely inferred as `0`.
- The stopping run first writes the latest cumulative performance, then changes the status to `已暂停` and sets the end date to the latest day with spend or impressions.
- `已暂停` is terminal for this program. To restart syncing, a person must change the record back to `正在投放`.
- Raw rows are grouped by `Ad ID + Date` before they are accumulated. This safely handles daily breakdown rows.
- Raw IDs with no content-hub record are reported but never create a new content record.
- Feishu millisecond timestamps are interpreted in `Asia/Shanghai` by default.

## Initial setup

1. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret | Value |
   | --- | --- |
   | `FEISHU_APP_ID` | Feishu self-built application App ID |
   | `FEISHU_APP_SECRET` | Feishu self-built application App Secret |
   | `FEISHU_SOURCE_APP_TOKEN` | Optional source Base token; skips source Wiki resolution |
   | `FEISHU_DESTINATION_APP_TOKEN` | Optional destination Base token; skips destination Wiki resolution |

2. Grant the app read access to Raw and read/write access to the destination Base. When both optional Base token secrets are set, the script accesses the Bases directly and does not call Wiki. Without a token, it resolves that Base from its `wiki_url`, which also requires Wiki node read scope and access to that node.
3. Run **Actions → Meta content sync → Run workflow** once. The repository starts in `diagnostic` mode: it prints each table's field names, IDs, and types and changes no records.
4. Copy the verified Raw field names into `config.yml`. Destination metric fields must be numeric fields; status is a single-select field and end date is a date field.
5. Change `mode: diagnostic` to `mode: sync` and commit the configuration.
6. Open **Actions → Meta content sync → Run workflow**, keep `dry_run` checked, and inspect the preview counts. This reads both Bases but changes neither Feishu nor sync state.
7. Run the workflow again with `dry_run` unchecked for the first live run.

## Required field mapping

Configure the exact Feishu field names in `config.yml`:

- Raw required: `date`, `ad_id`, `spend`, `impressions`, `reach`, `clicks`, `purchases`, `purchase_value`.
- Raw optional: `video_3s_views`, `video_plays`, `video_avg_watch_time_seconds`, `likes`, `comments`.
- Content hub required: `ad_id`, `status`, `end_date`, `spend`, `purchases`, `roas`, `ctr`, `frequency`, `impressions`, `clicks`.
- Content hub optional: `video_avg_watch_time_seconds`, `video_3s_rate`, `likes`, `comments`, `last_synced_date`.

Calculated cumulative values use these formulas:

- `ROAS = purchase_value / spend`
- `CTR = clicks / impressions`
- `Frequency = impressions / reach`
- `3-second rate = video_3s_views / impressions`
- Average watch time is weighted by video plays.

`source.wiki_url`, `destination.wiki_url`, and both table IDs are prefilled from the supplied Feishu links. No Base token secrets are required. Confirm both tables with the diagnostic output before live sync.

## Manual recovery

- To re-enable an intentionally restarted ad, manually change its status in Feishu to `正在投放`. The next successful run can update it again.
- To reprocess a report date, edit `state/sync_state.json` and clear or replace `last_successful_report_date`, then run the workflow manually.
- The workflow commits only changes to `state/sync_state.json`; credentials are never stored in the repository.
