import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import daily_topic_analysis as app


TODAY = datetime.date(2026, 9, 30)


def song(name, artist="歌手"):
    return {"song": name, "artist": artist, "reason": "适合女声", "title": f"听{name}", "tags": ["#清唱"]}


def response(*names):
    return json.dumps({"action": "录一首", "songs": [song(name) for name in names]}, ensure_ascii=False)


class SelectionTests(unittest.TestCase):
    def test_window_includes_six_days_ago_and_same_day(self):
        history = {"entries": [
            {"date": "2026-09-23", "songs": [song("七天前") ]},
            {"date": "2026-09-24", "songs": [song("六天前") ]},
            {"date": "2026-09-30", "songs": [song("当天") ]},
            {"date": "2026-10-01", "songs": [song("未来") ]},
        ]}
        self.assertEqual(set(app.recent_songs(history, TODAY).values()), {"六天前", "当天"})

    def test_normalization(self):
        for name in ["《ＡＢＣ》", "a b c", "ABC（Live）", "ABC【清唱版】", "ABC (现场版)"]:
            self.assertEqual(app.song_key(name), "abc")
        self.assertNotEqual(app.song_key("后来"), app.song_key("后来我们"))

    def test_repeated_songs_are_filtered_and_only_missing_slots_requested(self):
        with patch.object(app, "call_llm", side_effect=[
            response("《旧歌》（Live）", "新歌一", "新歌一", "新歌二", "新歌三"),
            response("新歌二", "新歌四", "新歌五"),
        ]) as llm:
            result = app.gen_topics({app.song_key("旧歌"): "旧歌"})
        self.assertEqual([s["song"] for s in result["songs"]], [f"新歌{x}" for x in "一二三四五"])
        self.assertEqual(llm.call_count, 2)
        self.assertIn("今日 2 首", llm.call_args_list[1].args[0])
        self.assertIn("旧歌", llm.call_args_list[0].args[0])
        self.assertIn("新歌一", llm.call_args_list[1].args[0])

    def test_invalid_output_is_retried(self):
        with patch.object(app, "call_llm", side_effect=[
            "not json", '{"songs": [null, {"song": "坏数据"}]}',
            response("一", "二", "三", "四", "五"),
        ]):
            self.assertEqual(len(app.gen_topics()["songs"]), 5)

    def test_no_repeated_fallback_when_attempts_exhausted(self):
        with patch.object(app, "call_llm", return_value=response("旧歌")) as llm:
            with self.assertRaisesRegex(RuntimeError, "一周内不重复"):
                app.gen_topics({app.song_key("旧歌"): "旧歌"})
            self.assertEqual(llm.call_count, 3)

    def test_summary_keeps_all_five_songs(self):
        songs = [dict(song(f"歌{i}"), title="长标题" * 50) for i in range(5)]
        summary = app.build_summary({"songs": songs, "action": "行动结尾"}, "")
        self.assertIn("歌4", summary)
        self.assertTrue(summary.endswith("行动：行动结尾"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.history_file = os.path.join(self.tmp.name, "history.json")
        self.patches = [
            patch.object(app, "HISTORY_FILE", self.history_file),
            patch.object(app, "REPORT_DIR", os.path.join(self.tmp.name, "reports")),
            patch.object(app, "today_date", return_value=TODAY),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_missing_history_and_retention(self):
        history = app.load_history()
        history["entries"] = [
            {"date": "2026-08-31", "songs": [song("过期") ]},
            {"date": "2026-09-01", "songs": [song("保留") ]},
        ]
        app.save_history(history, [song("新歌")], TODAY)
        loaded = app.load_history()
        self.assertEqual([e["date"] for e in loaded["entries"]], ["2026-09-01", "2026-09-30"])
        self.assertEqual(os.listdir(self.tmp.name), ["history.json"])

    def test_success_is_saved_and_same_day_second_run_excludes_first(self):
        with patch.object(app, "call_llm", side_effect=[response("一", "二", "三", "四", "五"),
                                                      response("六", "七", "八", "九", "十")]) as llm, \
                patch.object(app, "send_feishu") as send:
            self.assertTrue(app.main()["ok"])
            self.assertTrue(app.main()["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertIn('"一"', llm.call_args_list[1].args[0])
        self.assertEqual(len(app.recent_songs(app.load_history(), TODAY)), 10)

    def test_push_failure_does_not_update_history(self):
        with patch.object(app, "call_llm", return_value=response("一", "二", "三", "四", "五")), \
                patch.object(app, "send_feishu", side_effect=RuntimeError("发送失败")):
            self.assertFalse(app.main()["ok"])
        self.assertFalse(os.path.exists(self.history_file))

    def test_corrupt_history_stops_before_generation_or_push(self):
        for content in ["bad json", '{"version":1,"entries":[{"date":"bad","songs":[]}]}',
                        '{"version":1,"entries":[{"date":"2026-09-29","songs":[{}]}]}']:
            with open(self.history_file, "w") as f:
                f.write(content)
            with patch.object(app, "call_llm") as llm, patch.object(app, "send_feishu") as send:
                self.assertFalse(app.main()["ok"])
                llm.assert_not_called()
                send.assert_not_called()

    def test_exhaustion_does_not_push_or_save(self):
        app.save_history({"entries": []}, [song("旧歌")], TODAY)
        with patch.object(app, "call_llm", return_value=response("旧歌")), patch.object(app, "send_feishu") as send:
            self.assertFalse(app.main()["ok"])
            send.assert_not_called()
        self.assertEqual(len(app.load_history()["entries"]), 1)

    def test_history_save_failure_is_visible_after_successful_push(self):
        with patch.object(app, "call_llm", return_value=response("一", "二", "三", "四", "五")), \
                patch.object(app, "send_feishu") as send, \
                patch.object(app, "save_history", side_effect=OSError("磁盘错误")):
            result = app.main()
        send.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertEqual(result["push"], "ok")
        self.assertIn("保存历史失败", result["history"])


if __name__ == "__main__":
    unittest.main()
