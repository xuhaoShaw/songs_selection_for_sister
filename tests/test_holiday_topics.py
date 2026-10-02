import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import daily_topic_analysis as app
from holiday_calendar import get_holiday_context


def context(date="2026-10-01"):
    return get_holiday_context(datetime.date.fromisoformat(date))


def song(name, themed=False, reason="表达家国祝福，旋律适合女声清唱"):
    return {"song": name, "artist": "歌手", "reason": "旋律舒缓，适合女声清唱",
            "title": f"在楼道里唱{name}", "tags": ["#清唱"],
            "is_theme": themed, "theme_reason": reason if themed else ""}


def output(songs, lead=None):
    return json.dumps({"songs": songs, "action": "练习舒适音区并试录", "lead": lead}, ensure_ascii=False)


class CalendarTests(unittest.TestCase):
    def test_national_window_and_preview(self):
        before = context("2026-09-30")
        self.assertEqual(before["min_theme_songs"], 0)
        self.assertIn("明日主题：国庆", before["tomorrow_preview"])
        self.assertEqual(context()["min_theme_songs"], 2)
        for day in range(2, 8):
            c = context(f"2026-10-{day:02}")
            self.assertEqual((c["theme"], c["phase"], c["min_theme_songs"]), ("国庆", "延续", 1))
        self.assertIsNone(context("2026-10-08")["theme"])

    def test_spring_festival_crosses_solar_month(self):
        self.assertEqual(context("2025-01-29")["theme"], "春节")
        self.assertEqual(context("2025-01-29")["min_theme_songs"], 2)
        for date in ("2025-01-30", "2025-02-01", "2025-02-04"):
            self.assertEqual((context(date)["theme"], context(date)["min_theme_songs"]), ("春节", 1))
        self.assertIsNone(context("2025-02-05")["theme"])

    def test_lunar_festivals(self):
        for date, theme in [("2025-02-12", "元宵"), ("2025-05-31", "端午"),
                            ("2025-08-29", "七夕"), ("2025-10-06", "中秋")]:
            self.assertEqual(context(date)["theme"], theme)

    def test_weekday_and_fixed_festivals(self):
        for date, theme in [("2026-05-10", "母亲节"), ("2026-06-21", "父亲节"),
                            ("2026-01-01", "元旦"), ("2026-02-14", "情人节"),
                            ("2026-09-10", "教师节"), ("2026-12-25", "圣诞节")]:
            self.assertEqual(context(date)["theme"], theme)
        self.assertNotEqual(context("2026-05-03")["theme"], "母亲节")
        self.assertNotEqual(context("2026-06-14")["theme"], "父亲节")

    def test_same_day_precedes_continuation(self):
        c = context("2025-10-06")
        self.assertEqual((c["theme"], c["phase"], c["min_theme_songs"]), ("中秋", "当天", 2))
        self.assertIn("国庆", c["secondary_themes"])

    def test_same_day_priority_and_no_leap_month_duplicate(self):
        with patch("holiday_calendar._events_on", side_effect=lambda d: ["中秋", "国庆"] if d == datetime.date(2026, 10, 1) else []):
            self.assertEqual(context()["theme"], "国庆")
        # 2025 年闰六月，不应把闰月日期误认作另一传统节日。
        with patch("holiday_calendar.Solar") as solar:
            lunar = solar.fromYmd.return_value.getLunar.return_value
            lunar.getMonth.return_value = -7
            lunar.getDay.return_value = 7
            self.assertIsNone(context("2026-03-01")["theme"])


class HolidaySelectionTests(unittest.TestCase):
    def test_october_first_acceptance_and_lead(self):
        songs = [song("日常一"), song("主题一", True), song("主题二", True), song("日常二"), song("日常三")]
        lead = {"song": "主题二", "artist": "歌手", "reason": "国庆唱家国祝福，女声轻声进入副歌"}
        with patch.object(app, "call_llm", return_value=output(songs, lead)) as llm:
            data = app.gen_topics({}, context())
        self.assertEqual(data["theme_count"], 2)
        self.assertEqual(data["songs"][0]["song"], "主题二")
        self.assertEqual(data["lead"], lead)
        self.assertFalse(data["quota_warning"])
        self.assertEqual(llm.call_count, 1)

    def test_full_daily_list_still_requests_missing_theme_and_refreshes_lead(self):
        first = output([song(f"日常{i}") for i in range(5)], {"song": "日常0", "artist": "歌手", "reason": "旧说明"})
        second = output([song("主题一", True), song("主题二", True)],
                        {"song": "主题二", "artist": "歌手", "reason": "国庆主题适合女声清唱的新说明"})
        with patch.object(app, "call_llm", side_effect=[first, second]) as llm:
            data = app.gen_topics({}, context())
        self.assertEqual(llm.call_count, 2)
        self.assertIn("补选 2 首", llm.call_args_list[1].args[0])
        self.assertEqual(data["theme_count"], 2)
        self.assertNotIn("旧说明", data["lead"]["reason"])
        self.assertEqual(len(data["songs"]), 5)

    def test_invalid_theme_reason_bool_and_repeated_songs_do_not_count(self):
        invalid = song("空理由", True, " ")
        bad_bool = dict(song("字符串标记", True), is_theme="true")
        first = [song("旧歌", True), invalid, bad_bool, song("日常一"), song("日常二"), song("日常三")]
        with patch.object(app, "call_llm", side_effect=[output(first), output([song("新主题一", True), song("新主题二", True)])]):
            data = app.gen_topics({app.song_key("旧歌"): "旧歌"}, context())
        self.assertEqual(data["theme_count"], 2)
        self.assertNotIn("旧歌", [s["song"] for s in data["songs"]])

    def test_quota_shortage_keeps_five_and_warns_before_lead(self):
        with patch.object(app, "call_llm", return_value=output([song(f"日常{i}") for i in range(5)])) as llm:
            data = app.gen_topics({}, context())
        self.assertEqual(llm.call_count, 3)
        self.assertEqual(data["theme_count"], 0)
        self.assertIn("目标至少 2 首，实际 0 首", data["quota_warning"])
        for rendered in (app.build_summary(data, ""), app.render_html(data)):
            self.assertLess(rendered.index("主题配额不足"), rendered.index("今日首推"))

    def test_one_theme_is_preferred_even_if_quota_not_met(self):
        with patch.object(app, "call_llm", return_value=output([song("主题", True)] + [song(f"日常{i}") for i in range(4)])):
            data = app.gen_topics({}, context())
        self.assertEqual(data["songs"][0]["song"], "主题")
        self.assertEqual(data["theme_count"], 1)
        self.assertIn("实际 1 首", data["quota_warning"])

    def test_wrong_or_stale_lead_falls_back_without_extra_call(self):
        for lead in [None, {"song": "不存在", "artist": "歌手", "reason": "错误说明"},
                     {"song": "日常0", "artist": "歌手", "reason": "不应首推日常"}]:
            with patch.object(app, "call_llm", return_value=output([song("主题一", True), song("主题二", True)] + [song(f"日常{i}") for i in range(3)], lead)) as llm:
                data = app.gen_topics({}, context())
            self.assertEqual(llm.call_count, 1)
            self.assertEqual(data["lead"]["song"], "主题一")
            self.assertIn("女声清唱", data["lead"]["reason"])

    def test_short_total_still_errors(self):
        with patch.object(app, "call_llm", return_value=output([song("主题", True)])) as llm:
            with self.assertRaises(RuntimeError):
                app.gen_topics({}, context())
            self.assertEqual(llm.call_count, 3)

    def test_daily_theme_flags_are_ignored_and_preview_is_shared(self):
        c = context("2026-09-30")
        with patch.object(app, "call_llm", return_value=output([song(str(i), True) for i in range(5)])):
            data = app.gen_topics({}, c)
        self.assertEqual(data["theme_count"], 0)
        for rendered in (app.build_summary(data, ""), app.render_html(data)):
            self.assertIn("日常选题", rendered)
            self.assertIn("明日主题：国庆", rendered)

    def test_html_escapes_model_text(self):
        songs = [song(str(i)) for i in range(5)]
        lead = {"song": "0", "artist": "歌手", "reason": "<script>alert(1)</script>"}
        with patch.object(app, "call_llm", return_value=output(songs, lead)):
            data = app.gen_topics({}, context("2026-03-01"))
        data["action"] = '<img src="x" onerror="bad">'
        rendered = app.render_html(data)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn('<img src="x"', rendered)

    def test_calendar_failure_prevents_generation_and_push(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "REPORT_DIR", tmp), \
                patch.object(app, "get_holiday_context", side_effect=ValueError("日历错误")), \
                patch.object(app, "call_llm") as llm, patch.object(app, "send_feishu") as send:
            result = app.main()
            self.assertFalse(result["ok"])
            self.assertIn("日历错误", result["report"])
            llm.assert_not_called()
            send.assert_not_called()
            self.assertTrue(os.path.exists(os.path.join(tmp, "push-failed.txt")))

    def test_invalid_config_stops_before_model(self):
        with patch.object(app, "call_llm") as llm:
            with self.assertRaises(ValueError):
                app.gen_topics({}, dict(context(), min_theme_songs=-1))
            llm.assert_not_called()

    def test_only_final_five_are_saved_after_successful_push(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(app, "REPORT_DIR", tmp), \
                patch.object(app, "HISTORY_FILE", os.path.join(tmp, "history.json")), \
                patch.object(app, "today_date", return_value=datetime.date(2026, 10, 1)), \
                patch.object(app, "call_llm", side_effect=[
                    output([song(f"日常{i}") for i in range(5)]),
                    output([song("主题一", True), song("主题二", True)]),
                ]), patch.object(app, "send_feishu") as send:
            result = app.main()
            self.assertTrue(result["ok"])
            self.assertEqual(result["theme_count"], 2)
            sent = send.call_args.args[0]
            history = app.load_history()
            self.assertEqual(len(history["entries"][0]["songs"]), 5)
            for saved in history["entries"][0]["songs"]:
                self.assertIn(f'《{saved["song"]}》', sent)
                self.assertEqual(set(saved), {"song", "artist"})
            self.assertNotIn("日常4", sent)
