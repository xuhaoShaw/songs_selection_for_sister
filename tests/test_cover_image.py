import base64
import datetime
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

import cover_image as cover
import daily_topic_analysis as app


TODAY = datetime.date(2026, 10, 4)


def selected():
    songs = [{"song": f"歌曲{i}", "artist": "歌手", "reason": "温柔思念",
              "title": "听见温柔", "tags": ["#翻唱"], "is_theme": i == 0,
              "theme_reason": "节日祝福" if i == 0 else ""} for i in range(5)]
    return {"songs": songs, "lead": {"song": "歌曲0", "artist": "歌手", "reason": "思念与祝福"},
            "holiday_context": {"theme": "国庆", "phase": "延续"},
            "action": "试录主推", "theme_count": 1, "quota_warning": ""}


def png(size=(1024, 1024)):
    buffer = BytesIO()
    Image.new("RGB", size, "orange").save(buffer, format="PNG")
    return buffer.getvalue()


class CoverGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def generate(self):
        return cover.generate_cover(selected(), self.temp.name, TODAY,
                                    "test-key", "https://api.modelverse.cn/v1/")

    def test_prompt_uses_final_main_song_without_fixed_scene_or_hype(self):
        data = selected()
        data["songs"][0]["song"] = "最终歌曲"
        prompt = cover.build_cover_prompt(data)
        self.assertIn('"歌名": "最终歌曲"', prompt)
        self.assertNotIn('"歌名": "歌曲1"', prompt)
        self.assertIn("不使用楼道作为固定背景", prompt)
        self.assertIn("不保证涨粉", prompt)
        self.assertIn('"当天主题": "国庆"', prompt)
        data["songs"][0]["is_theme"] = False
        self.assertIn('"当天主题": null', cover.build_cover_prompt(data))
        data["lead_details"] = {"cover_headline": "有些思念，唱出来就懂了"}
        self.assertIn('"封面短句": "有些思念，唱出来就懂了"', cover.build_cover_prompt(data))

    def test_headline_validation_and_mismatched_lead(self):
        current = selected()["songs"][0]
        for headline in [None, ["标题"], "楼道清唱", "两行\n标题", "长" * 17]:
            self.assertEqual(app._make_lead_details(current, {"cover_headline": headline})["cover_headline"], "")
        self.assertEqual(app._make_lead_details(current, {"cover_headline": "有些思念，唱出来就懂了"})["cover_headline"], "有些思念，唱出来就懂了")
        data = selected()
        data["lead"] = {"song": "不是最终歌曲", "artist": "歌手", "reason": "旧理由", "cover_headline": "旧歌封面"}
        with patch.object(app, "call_llm", return_value=json.dumps(data, ensure_ascii=False)):
            result = app.gen_topics({}, {"min_theme_songs": 0, "theme": None})
        self.assertEqual(result["lead_details"]["cover_headline"], "")

    def test_base64_png_and_request_payload(self):
        raw = png()
        response = Mock()
        response.json.return_value = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
        with patch.object(cover.requests, "post", return_value=response) as post:
            result = self.generate()
        self.assertEqual(Path(result["path"]).read_bytes(), raw)
        self.assertEqual(base64.b64decode(result["b64"]), raw)
        self.assertEqual(result["song"], "歌曲0")
        self.assertEqual(post.call_args.args[0], "https://api.modelverse.cn/v1/images/generations")
        args = post.call_args.kwargs
        self.assertEqual(args["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(args["json"]["model"], "gpt-image-2")
        self.assertEqual(args["json"]["size"], "1024x1024")
        self.assertEqual(args["json"]["output_format"], "png")
        self.assertEqual(args["timeout"], 300)

    def test_https_url_download_does_not_send_api_key(self):
        response = Mock()
        response.json.return_value = {"data": [{"url": "https://example.test/cover.png"}]}
        download = Mock()
        download.__enter__ = Mock(return_value=download)
        download.__exit__ = Mock(return_value=False)
        download.iter_content.return_value = [png()]
        with patch.object(cover.requests, "post", return_value=response), \
                patch.object(cover.requests, "get", return_value=download) as get:
            self.generate()
        self.assertNotIn("headers", get.call_args.kwargs)

    def test_invalid_response_or_images_never_saved(self):
        for payload in [{}, {"data": []}, {"data": [None]}, {"data": [{}]},
                        {"data": [{"url": "http://example.test/cover"}]},
                        {"data": [{"b64_json": "not-base64"}]},
                        {"data": [{"b64_json": base64.b64encode(b"not png").decode()}]},
                        {"data": [{"b64_json": base64.b64encode(png((20, 20))).decode()}]}]:
            with self.subTest(payload=str(payload)[:60]):
                response = Mock()
                response.json.return_value = payload
                with patch.object(cover.requests, "post", return_value=response):
                    with self.assertRaises(Exception):
                        self.generate()
                self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_http_failure_and_size_limit(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError()
        with patch.object(cover.requests, "post", return_value=response):
            with self.assertRaises(requests.HTTPError):
                self.generate()
        response.raise_for_status.side_effect = None
        response.json.return_value = {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        with patch.object(cover.requests, "post", return_value=response), \
                patch.object(cover, "MAX_IMAGE_BYTES", 20):
            with self.assertRaises(ValueError):
                self.generate()


class CoverDeliveryTests(unittest.TestCase):
    def test_cover_between_main_and_secondary_for_both_channels(self):
        data = selected()
        data["cover"] = {"image_key": "img_test", "b64": base64.b64encode(png()).decode()}
        summary = app.build_summary(data, "")
        card = app.build_feishu_card(summary, "img_test")
        first, image, last = card["elements"]
        self.assertIn("今日主推", first["content"])
        self.assertNotIn("副推 1", first["content"])
        self.assertEqual(image["img_key"], "img_test")
        self.assertEqual(image["tag"], "img")
        self.assertIn("副推 1", last["content"])
        self.assertIn("副推 4", last["content"])
        html = app.render_html(data)
        self.assertLess(html.index("data:image/png;base64,"), html.index("副推 1"))
        self.assertEqual(html.count("data:image/png;base64,"), 1)
        resp = Mock()
        resp.json.return_value = {"code": 0}
        with patch.object(app.requests, "post", return_value=resp) as post, \
                patch.object(app, "FEISHU_WEBHOOK", "https://example.test/hook"):
            app.send_feishu(summary, "img_test")
        self.assertEqual(post.call_args.kwargs["json"]["card"], card)
        with patch.object(app.requests, "post", return_value=resp) as post, \
                patch.object(app, "FEISHU_WEBHOOK", ""), \
                patch.object(app, "_tenant_token", return_value="test-token"):
            app.send_feishu(summary, "img_test")
        self.assertEqual(json.loads(post.call_args.kwargs["json"]["content"]), card)

    def test_upload_image_uses_multipart_and_checks_response(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cover.png"
            path.write_bytes(png())
            response = Mock()
            response.json.return_value = {"code": 0, "data": {"image_key": "img_test"}}
            with patch.object(app, "_tenant_token", return_value="test-token"), \
                    patch.object(app.requests, "post", return_value=response) as post:
                self.assertEqual(app.upload_feishu_image(str(path)), "img_test")
                self.assertEqual(post.call_args.kwargs["data"], {"image_type": "message"})
                self.assertEqual(post.call_args.kwargs["files"]["image"][2], "image/png")
                for body in [{"code": 999}, {"code": 0, "data": {}}]:
                    response.json.return_value = body
                    with self.assertRaises(RuntimeError):
                        app.upload_feishu_image(str(path))

    def test_prepare_disabled_failure_missing_config_and_upload_failure(self):
        with patch.object(app, "COVER_ENABLED", False), patch.object(app, "generate_cover") as gen:
            app.prepare_cover(selected(), TODAY)
            gen.assert_not_called()
        data = selected()
        with patch.object(app, "COVER_ENABLED", True), \
                patch.object(app, "generate_cover", side_effect=RuntimeError("sensitive")):
            app.prepare_cover(data, TODAY)
        self.assertIn("封面生成失败", data["cover_warning"])
        self.assertNotIn("sensitive", data["cover_warning"])
        for configured in [False, True]:
            data = selected()
            with patch.object(app, "COVER_ENABLED", True), \
                    patch.object(app, "generate_cover", return_value={"path": "image.png", "b64": "png"}), \
                    patch.object(app, "FEISHU_APP_ID", "id" if configured else ""), \
                    patch.object(app, "FEISHU_APP_SECRET", "secret" if configured else ""), \
                    patch.object(app, "upload_feishu_image", side_effect=RuntimeError("sensitive")) as upload:
                app.prepare_cover(data, TODAY)
            self.assertEqual(upload.call_count, int(configured))
            self.assertIn("封面已生成", data["cover_warning"])
            self.assertIn("封面提醒", app.build_summary(data, ""))

    def test_prepare_success_generates_and_uploads_exactly_once(self):
        data = selected()
        with patch.object(app, "COVER_ENABLED", True), \
                patch.object(app, "generate_cover", return_value={"path": "image.png", "b64": "png"}) as gen, \
                patch.object(app, "FEISHU_APP_ID", "id"), \
                patch.object(app, "FEISHU_APP_SECRET", "secret"), \
                patch.object(app, "upload_feishu_image", return_value="img_test") as upload:
            app.prepare_cover(data, TODAY)
        gen.assert_called_once()
        upload.assert_called_once_with("image.png")
        self.assertEqual(data["cover"]["image_key"], "img_test")
        self.assertNotIn("cover_warning", data)

    def run_main(self, send_error=None):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        def attach(data, date):
            data["cover"] = {"path": "cover.png", "b64": base64.b64encode(png()).decode(), "image_key": "img_test"}
        with patch.object(app, "REPORT_DIR", self.temp.name), \
                patch.object(app, "HISTORY_FILE", str(Path(self.temp.name) / "history.json")), \
                patch.object(app, "today_date", return_value=TODAY), \
                patch.object(app, "gen_topics", return_value=selected()), \
                patch.object(app, "prepare_cover", side_effect=attach), \
                patch.object(app, "send_feishu", side_effect=send_error) as send:
            result = app.main()
        return result, send

    def test_success_saves_final_songs_only(self):
        result, send = self.run_main()
        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], "img_test")
        history = json.loads((Path(self.temp.name) / "history.json").read_text())
        self.assertEqual(len(history["entries"][0]["songs"]), 5)
        self.assertEqual(set(history["entries"][0]["songs"][0]), {"song", "artist"})

    def test_explicit_image_card_rejection_falls_back_once(self):
        result, send = self.run_main([app.FeishuMessageRejected("rejected"), None])
        self.assertTrue(result["ok"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len(send.call_args_list[1].args), 1)
        self.assertIn("未接受图片卡片", send.call_args_list[1].args[0])
        self.assertIn("未接受图片卡片", Path(result["report"]).read_text())

    def test_uncertain_timeout_is_not_retried_or_saved(self):
        result, send = self.run_main(requests.Timeout())
        self.assertFalse(result["ok"])
        self.assertEqual(send.call_count, 1)
        self.assertFalse((Path(self.temp.name) / "history.json").exists())

    def test_generation_failure_still_pushes_and_saves(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(app, "REPORT_DIR", folder), \
                patch.object(app, "HISTORY_FILE", str(Path(folder) / "history.json")), \
                patch.object(app, "gen_topics", return_value=selected()), \
                patch.object(app, "COVER_ENABLED", True), \
                patch.object(app, "generate_cover", side_effect=RuntimeError("failure")), \
                patch.object(app, "send_feishu") as send:
            result = app.main()
            self.assertTrue(result["ok"])
            self.assertIn("封面生成失败", send.call_args.args[0])
            self.assertTrue((Path(folder) / "history.json").exists())


if __name__ == "__main__":
    unittest.main()
