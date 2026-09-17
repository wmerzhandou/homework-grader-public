from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.pipeline import media
from app.pipeline.asr_client import AsrClient
from app.pipeline.images import detect_kind
from app.models import MaterialKind


class TestDetectKind:
    @pytest.mark.parametrize(
        "filename,kind",
        [
            ("a.jpg", MaterialKind.image),
            ("b.PNG", MaterialKind.image),
            ("c.mp3", MaterialKind.audio),
            ("d.m4a", MaterialKind.audio),
            ("e.mp4", MaterialKind.video),
            ("f.MOV", MaterialKind.video),
            ("g.pdf", MaterialKind.document),
            ("h.docx", MaterialKind.document),
            ("noext", MaterialKind.document),
        ],
    )
    def test_kinds(self, filename, kind):
        assert detect_kind(filename) == kind


class TestMedia:
    async def test_to_f32_pcm_invokes_ffmpeg(self, settings, monkeypatch, tmp_path):
        calls = []

        async def fake_run(cmd):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"\x00" * 16)

        monkeypatch.setattr(media, "_run", fake_run)
        out = await media.to_f32_pcm(tmp_path / "in.m4a", tmp_path / "out.f32", settings)
        assert out.read_bytes() == b"\x00" * 16
        cmd = calls[0]
        joined = " ".join(cmd)
        assert "-f f32le" in joined
        assert "-ar 16000" in joined
        assert "-ac 1" in joined

    async def test_extract_keyframes_three_offsets(self, settings, monkeypatch, tmp_path):
        calls = []

        async def fake_run(cmd):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"jpg")

        monkeypatch.setattr(media, "_run", fake_run)
        monkeypatch.setattr(media, "probe_duration_s", lambda *a, **k: _async(10.0))
        frames = await media.extract_keyframes(tmp_path / "v.mp4", tmp_path / "kf", "m1", settings)
        assert [f.name for f in frames] == ["m1_0.jpg", "m1_1.jpg", "m1_2.jpg"]
        offsets = [c[c.index("-ss") + 1] for c in calls]
        assert offsets == ["0.000", "5.000", "9.900"]

    async def test_extract_keyframes_without_duration(self, settings, monkeypatch, tmp_path):
        async def fake_run(cmd):
            Path(cmd[-1]).write_bytes(b"jpg")

        monkeypatch.setattr(media, "_run", fake_run)
        monkeypatch.setattr(media, "probe_duration_s", lambda *a, **k: _async(None))
        frames = await media.extract_keyframes(tmp_path / "v.mp4", tmp_path / "kf", "m1", settings)
        assert [f.name for f in frames] == ["m1_0.jpg"]


async def _async(value):
    return value


class TestAsrClient:
    async def test_three_step_flow(self, settings, tmp_path):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/start":
                return httpx.Response(200, json={"session_id": "sid-1"})
            if request.url.path == "/api/chunk":
                return httpx.Response(200, json={"language": "zh", "text": "累计文本"})
            if request.url.path == "/api/finish":
                return httpx.Response(200, json={"language": "zh", "text": "最终文本"})
            return httpx.Response(404)

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://asr.test"
        )
        pcm = tmp_path / "a.f32"
        pcm.write_bytes(b"\x00" * 64)

        async with AsrClient(settings, http=http) as client:
            language, text = await client.transcribe_f32(pcm)

        assert (language, text) == ("zh", "最终文本")
        assert [r.url.path for r in requests] == ["/api/start", "/api/chunk", "/api/finish"]
        chunk_req = requests[1]
        assert chunk_req.url.params["session_id"] == "sid-1"
        assert chunk_req.headers["Content-Type"] == "application/octet-stream"
        assert chunk_req.content == b"\x00" * 64

    async def test_start_without_session_id_raises(self, settings, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://asr.test"
        )
        pcm = tmp_path / "a.f32"
        pcm.write_bytes(b"\x00")
        async with AsrClient(settings, http=http) as client:
            with pytest.raises(Exception):
                await client.transcribe_f32(pcm)
