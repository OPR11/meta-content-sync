import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from meta_content_sync import build_update_plan, parse_date


SOURCE_FIELDS = {
    "date": "Date",
    "ad_id": "Ad ID",
    "spend": "Spend",
    "impressions": "Impressions",
    "reach": "Reach",
    "clicks": "Clicks",
    "purchases": "Purchases",
    "purchase_value": "Purchase value",
    "video_3s_views": "3-second views",
    "video_plays": "Video plays",
    "video_avg_watch_time_seconds": "Average watch time",
    "likes": "Likes",
    "comments": "Comments",
}

DESTINATION_FIELDS = {
    "ad_id": "广告ID",
    "status": "素材状态",
    "end_date": "结束投放时间",
    "spend": "Spend",
    "purchases": "Purchase",
    "roas": "ROAS",
    "ctr": "CTR",
    "frequency": "Frequency",
    "impressions": "曝光量（展示次数）",
    "clicks": "链接点击量",
    "video_avg_watch_time_seconds": "视频平均播放时间（秒）",
    "video_3s_rate": "3秒完播率",
    "likes": "点赞数",
    "comments": "评论数",
}


def raw(ad_id, day, *, spend=10, impressions=100, reach=80, clicks=5,
        purchases=1, purchase_value=20, video_3s_views=30, video_plays=10,
        video_avg_watch_time_seconds=4, likes=2, comments=1):
    return {"fields": {
        "Date": day.isoformat(),
        "Ad ID": ad_id,
        "Spend": spend,
        "Impressions": impressions,
        "Reach": reach,
        "Clicks": clicks,
        "Purchases": purchases,
        "Purchase value": purchase_value,
        "3-second views": video_3s_views,
        "Video plays": video_plays,
        "Average watch time": video_avg_watch_time_seconds,
        "Likes": likes,
        "Comments": comments,
    }}


def destination(record_id, ad_id, status):
    return {"record_id": record_id, "fields": {"广告ID": ad_id, "素材状态": status}}


class SyncRuleTests(unittest.TestCase):
    def test_repository_stays_in_diagnostic_mode_until_raw_fields_are_verified(self):
        config = yaml.safe_load(Path("config.yml").read_text(encoding="utf-8"))

        self.assertEqual(config["mode"], "diagnostic")
        self.assertEqual(config["destination"]["fields"]["status"], "素材状态")
        self.assertEqual(config["destination"]["fields"]["spend"], "Spend")

    def test_feishu_timestamp_is_parsed_in_shanghai_timezone(self):
        shanghai_midnight = datetime(
            2026, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai")
        )
        milliseconds = int(shanghai_midnight.timestamp() * 1000)

        self.assertEqual(
            parse_date(milliseconds, "Asia/Shanghai"),
            date(2026, 10, 31),
        )

    def test_only_currently_running_ads_are_planned_for_update(self):
        target = date(2026, 10, 31)
        source = [raw("active-ad", target), raw("paused-ad", target)]
        destination_rows = [
            destination("rec-active", "active-ad", "正在投放"),
            destination("rec-paused", "paused-ad", "已暂停"),
        ]

        plan = build_update_plan(
            source,
            destination_rows,
            SOURCE_FIELDS,
            DESTINATION_FIELDS,
            target,
            active_status="正在投放",
            stopped_status="已暂停",
            zero_days=2,
            timezone_name="Asia/Shanghai",
        )

        self.assertEqual([item["record_id"] for item in plan["updates"]], ["rec-active"])
        self.assertEqual(plan["skipped_not_active"], 1)
        self.assertIsNone(plan["updates"][0]["fields"]["结束投放时间"])

    def test_each_ad_requires_its_own_previous_day_row(self):
        target = date(2026, 10, 31)
        source = [
            raw("ready-ad", target),
            raw("late-ad", date(2026, 10, 30)),
        ]
        destination_rows = [
            destination("rec-ready", "ready-ad", "正在投放"),
            destination("rec-late", "late-ad", "正在投放"),
        ]

        plan = build_update_plan(
            source, destination_rows, SOURCE_FIELDS, DESTINATION_FIELDS, target,
            active_status="正在投放", stopped_status="已暂停", zero_days=2,
            timezone_name="Asia/Shanghai",
        )

        self.assertEqual([item["record_id"] for item in plan["updates"]], ["rec-ready"])
        self.assertEqual(plan["skipped_missing_target"], ["late-ad"])

    def test_two_explicit_zero_spend_days_pause_after_writing_final_totals(self):
        target = date(2026, 10, 31)
        source = [
            raw("ad-1", date(2026, 10, 29), spend=10, impressions=100),
            raw("ad-1", date(2026, 10, 30), spend=0, impressions=20,
                purchases=0, purchase_value=0),
            raw("ad-1", target, spend=0, impressions=0,
                purchases=0, purchase_value=0),
        ]

        plan = build_update_plan(
            source, [destination("rec-1", "ad-1", "正在投放")],
            SOURCE_FIELDS, DESTINATION_FIELDS, target,
            active_status="正在投放", stopped_status="已暂停", zero_days=2,
            timezone_name="Asia/Shanghai",
        )

        fields = plan["updates"][0]["fields"]
        expected_end = int(datetime(
            2026, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
        ).timestamp() * 1000)
        self.assertEqual(fields["素材状态"], "已暂停")
        self.assertEqual(fields["结束投放时间"], expected_end)
        self.assertEqual(fields["Spend"], 10.0)
        self.assertEqual(fields["曝光量（展示次数）"], 120.0)

    def test_missing_prior_day_is_unknown_and_does_not_pause(self):
        target = date(2026, 10, 31)
        source = [raw("ad-1", target, spend=0, impressions=0)]

        plan = build_update_plan(
            source, [destination("rec-1", "ad-1", "正在投放")],
            SOURCE_FIELDS, DESTINATION_FIELDS, target,
            active_status="正在投放", stopped_status="已暂停", zero_days=2,
            timezone_name="Asia/Shanghai",
        )

        self.assertNotIn("素材状态", plan["updates"][0]["fields"])
        self.assertIsNone(plan["updates"][0]["fields"]["结束投放时间"])

    def test_cumulative_metrics_are_written_to_individual_columns(self):
        target = date(2026, 10, 31)
        source = [
            raw("ad-1", date(2026, 10, 30), spend=10, impressions=100,
                reach=80, clicks=5, purchases=1, purchase_value=20,
                video_3s_views=30, video_plays=10,
                video_avg_watch_time_seconds=4, likes=2, comments=1),
            raw("ad-1", target, spend=20, impressions=200,
                reach=100, clicks=10, purchases=2, purchase_value=40,
                video_3s_views=60, video_plays=30,
                video_avg_watch_time_seconds=8, likes=3, comments=2),
        ]

        plan = build_update_plan(
            source, [destination("rec-1", "ad-1", "正在投放")],
            SOURCE_FIELDS, DESTINATION_FIELDS, target,
            active_status="正在投放", stopped_status="已暂停", zero_days=2,
            timezone_name="Asia/Shanghai",
        )

        fields = plan["updates"][0]["fields"]
        self.assertEqual(fields["Spend"], 30.0)
        self.assertEqual(fields["Purchase"], 3.0)
        self.assertEqual(fields["ROAS"], 2.0)
        self.assertEqual(fields["CTR"], 0.05)
        self.assertAlmostEqual(fields["Frequency"], 300 / 180)
        self.assertEqual(fields["曝光量（展示次数）"], 300.0)
        self.assertEqual(fields["链接点击量"], 15.0)
        self.assertEqual(fields["视频平均播放时间（秒）"], 7.0)
        self.assertEqual(fields["3秒完播率"], 0.3)
        self.assertEqual(fields["点赞数"], 5.0)
        self.assertEqual(fields["评论数"], 3.0)


if __name__ == "__main__":
    unittest.main()
