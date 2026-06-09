"""Offline unit tests — no network, Blender, ffmpeg, Google libs, or keys.

Covers the input validation and pure helpers that guard every tool. The Google
client libraries are imported lazily inside youtube.upload (after validation),
so these run without them installed. Run with:

    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshy_youtube import meshy, turntable, video, youtube  # noqa: E402


class TestMeshyValidation(unittest.TestCase):
    def test_headers_requires_key(self):
        old = os.environ.pop("MESHY_API_KEY", None)
        try:
            with self.assertRaises(meshy.MeshyError):
                meshy._headers()
        finally:
            if old is not None:
                os.environ["MESHY_API_KEY"] = old

    def test_bad_art_style_rejected(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.create_preview_task("a robot", art_style="nope")

    def test_get_task_rejects_injection_id(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.get_task("../../evil?x=1")

    def test_download_glb_needs_model_url(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.download_glb({"status": "SUCCEEDED"}, "/tmp/nope.glb")


class TestImageInputs(unittest.TestCase):
    def test_url_passthrough(self):
        self.assertEqual(meshy.to_image_source("https://x/y.png"),
                         "https://x/y.png")

    def test_local_file_to_data_uri(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + b"realpngbody")
            path = fh.name
        try:
            self.assertTrue(meshy.to_image_source(path).startswith(
                "data:image/png;base64,"))
        finally:
            os.unlink(path)

    def test_non_image_file_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(b"SECRET=hunter2\n")  # not an image despite .png name
            path = fh.name
        try:
            with self.assertRaises(meshy.MeshyError):
                meshy.to_image_source(path)
        finally:
            os.unlink(path)

    def test_endpoint_allowlist_blocks_foreign_host(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.get_task("validtask", endpoint="https://evil.example.com/x")

    def test_missing_file_rejected(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.to_image_source("/no/such/file.png")

    def test_multi_image_count_validated(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.generate_from_images([], "/tmp/m.glb")
        with self.assertRaises(meshy.MeshyError):
            meshy.generate_from_images(["a", "b", "c", "d", "e"], "/tmp/m.glb")


class TestRetextureRig(unittest.TestCase):
    def test_retexture_requires_source_and_style(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.retexture("/tmp/m.glb", text_style_prompt="gold")  # no source
        with self.assertRaises(meshy.MeshyError):
            meshy.retexture("/tmp/m.glb", input_task_id="t")  # no style

    def test_rig_requires_source(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.rig()

    def test_animate_validates_ids(self):
        with self.assertRaises(meshy.MeshyError):
            meshy.animate("../bad", 1, "/tmp/a.glb")
        with self.assertRaises(meshy.MeshyError):
            meshy.animate("validtask", -1, "/tmp/a.glb")


class TestTurntable(unittest.TestCase):
    def test_zero_frames_rejected(self):
        with self.assertRaises(turntable.TurntableError):
            turntable.render("model.glb", "/tmp/frames", frames=0)

    def test_hd_resolution_allowed(self):
        # YouTube edition raised the ceiling to 1920.
        self.assertEqual(turntable.MAX_RESOLUTION, 1920)

    def test_numeric_sort_and_rename(self):
        d = tempfile.mkdtemp()
        for name, content in [("1.png", b"A"), ("2.png", b"B"), ("10.png", b"C")]:
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(content)
        self.assertEqual(turntable._normalize_frame_sequence(d, 3), 3)
        with open(os.path.join(d, "0000.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"A")  # "1" sorts first numerically
        with open(os.path.join(d, "0002.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"C")  # "10" sorts last, not lexically


class TestVideoBounds(unittest.TestCase):
    def test_bad_fps_rejected(self):
        with self.assertRaises(video.VideoError):
            video.frames_to_video("/tmp/frames", "/tmp/out.mp4", fps=0)


class TestYouTubeValidation(unittest.TestCase):
    """All of these raise BEFORE any Google library import."""

    def test_missing_file(self):
        with self.assertRaises(youtube.YouTubeError):
            youtube.upload("/tmp/does-not-exist.mp4", "Title")

    def test_non_video_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as fh:
            with self.assertRaises(youtube.YouTubeError):
                youtube.upload(fh.name, "Title")

    def test_empty_title(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with self.assertRaises(youtube.YouTubeError):
                youtube.upload(fh.name, "   ")

    def test_invalid_privacy(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with self.assertRaises(youtube.YouTubeError):
                youtube.upload(fh.name, "Title", privacy="semi-public")

    def test_title_too_long(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with self.assertRaises(youtube.YouTubeError):
                youtube.upload(fh.name, "x" * 101)

    def test_description_too_long(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as fh:
            with self.assertRaises(youtube.YouTubeError):
                youtube.upload(fh.name, "Title", description="x" * 5001)


class TestTokenSecurity(unittest.TestCase):
    def test_write_token_is_0600_and_atomic(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "sub", "token.json")
        youtube.write_token(path, '{"refresh_token":"secret"}')
        self.assertTrue(os.path.isfile(path))
        mode = os.stat(path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        with open(path) as fh:
            self.assertIn("secret", fh.read())
        # No leftover temp files in the directory.
        self.assertEqual(os.listdir(os.path.dirname(path)), ["token.json"])


class TestYouTubeConfig(unittest.TestCase):
    def test_default_paths(self):
        old = {k: os.environ.pop(k, None)
               for k in ("YOUTUBE_CLIENT_SECRET_FILE", "YOUTUBE_TOKEN_FILE")}
        try:
            self.assertTrue(youtube.client_secret_file().endswith(
                "meshy-youtube-mcp/client_secret.json"))
            self.assertTrue(youtube.token_file().endswith(
                "meshy-youtube-mcp/token.json"))
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_env_override_and_has_token(self):
        old = os.environ.get("YOUTUBE_TOKEN_FILE")
        os.environ["YOUTUBE_TOKEN_FILE"] = "/tmp/definitely-no-such-token.json"
        try:
            self.assertEqual(youtube.token_file(),
                             "/tmp/definitely-no-such-token.json")
            self.assertFalse(youtube.has_token())
        finally:
            if old is None:
                os.environ.pop("YOUTUBE_TOKEN_FILE", None)
            else:
                os.environ["YOUTUBE_TOKEN_FILE"] = old


class TestServer(unittest.TestCase):
    def test_tags_list(self):
        from meshy_youtube import server
        self.assertEqual(server._tags_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(server._tags_list(""), [])

    def test_preflight_missing_meshy_key(self):
        from meshy_youtube import server
        old = os.environ.pop("MESHY_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                server._preflight(need_meshy=True)
        finally:
            if old is not None:
                os.environ["MESHY_API_KEY"] = old

    def test_one_shot_success_path(self):
        from meshy_youtube import server
        with mock.patch.object(server, "_preflight"), \
                mock.patch.object(server, "_workdir", return_value="/tmp/x"), \
                mock.patch.object(meshy, "generate",
                                  return_value={"glb_path": "/tmp/x/model.glb"}), \
                mock.patch.object(turntable, "render",
                                  return_value={"frames_dir": "/tmp/x/frames",
                                                "frame_count": 180}), \
                mock.patch.object(video, "frames_to_video",
                                  return_value="/tmp/x/turntable.mp4"), \
                mock.patch.object(youtube, "upload",
                                  return_value={"video_id": "abc",
                                                "watch_url": "https://youtu.be/abc"}):
            res = server.meshy_to_youtube("a dragon", "Title", privacy="unlisted")
        self.assertTrue(res["ok"])
        self.assertEqual(res["watch_url"], "https://youtu.be/abc")

    def test_one_shot_bad_privacy_fails_before_meshy(self):
        from meshy_youtube import server
        billed = {"called": False}

        def _gen(*a, **k):
            billed["called"] = True
            return {"glb_path": "x"}

        with mock.patch.object(server, "_preflight"), \
                mock.patch.object(meshy, "generate", side_effect=_gen):
            res = server.meshy_to_youtube("a dragon", "Title", privacy="nope")
        self.assertFalse(res["ok"])
        self.assertEqual(res["failed_stage"], "validate")
        self.assertFalse(billed["called"])


if __name__ == "__main__":
    unittest.main()
