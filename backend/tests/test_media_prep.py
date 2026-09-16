"""产出物预览副本：图片缩放、视频转码（H.264）。"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from sqlmodel import Session

from app import media_prep
from app.db import get_engine
from app.models import Artifact, Thread, User


def _make_thread(user_id: str = "u1", thread_id: str = "t1") -> None:
    with Session(get_engine()) as session:
        if session.get(User, user_id) is None:
            session.add(User(id=user_id, username="alice", password_hash="x"))
            session.commit()
        session.add(Thread(id=thread_id, user_id=user_id))
        session.commit()


async def test_image_preview_is_downscaled(settings, engine, monkeypatch):
    from PIL import Image

    monkeypatch.setenv("ARTIFACT_IMAGE_MAX_DIM", "800")
    from app.config import get_settings

    get_settings.cache_clear()
    _make_thread()
    ws = settings.data_dir / "users" / "u1" / "threads" / "t1" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    original = ws / "大图.png"
    Image.new("RGB", (3000, 1500), "white").save(original)

    with Session(get_engine()) as session:
        artifact = Artifact(
            thread_id="t1",
            filename="大图.png",
            stored_path=str(original),
            kind="image",
            size=original.stat().st_size,
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    await media_prep.prepare_artifact(artifact_id)

    with Session(get_engine()) as session:
        updated = session.get(Artifact, artifact_id)
    assert updated is not None and updated.preview_path
    preview = __import__("pathlib").Path(updated.preview_path)
    assert preview.is_file()
    with Image.open(preview) as img:
        assert max(img.size) <= 800
    # 原文件必须保留
    assert original.is_file()
    get_settings.cache_clear()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
async def test_video_preview_is_transcoded_to_h264(settings, engine):
    from app.config import get_settings

    get_settings.cache_clear()
    _make_thread("u2", "t2")
    ws = settings.data_dir / "users" / "u2" / "threads" / "t2" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    source = ws / "片段.mp4"
    # 用 mpeg4 编码生成一个"浏览器不友好"的视频
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
            "-c:v", "mpeg4", str(source),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with Session(get_engine()) as session:
        artifact = Artifact(
            thread_id="t2",
            filename="片段.mp4",
            stored_path=str(source),
            kind="video",
            size=source.stat().st_size,
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    await media_prep.prepare_artifact(artifact_id)

    with Session(get_engine()) as session:
        updated = session.get(Artifact, artifact_id)
    assert updated is not None and updated.preview_path, "应生成转码后的预览副本"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "csv=p=0", updated.preview_path],
        check=True, capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "h264"
    assert source.is_file()
    get_settings.cache_clear()
def test_parse_probe_csv_handles_both_field_orders():
    """ffprobe 的字段顺序不固定（实测输出 h264,video），解析必须与顺序无关。"""
    assert media_prep._parse_probe_csv("h264,video\naac,audio\n") == ("h264", "aac")
    assert media_prep._parse_probe_csv("video,h264\naudio,aac\n") == ("h264", "aac")
    assert media_prep._parse_probe_csv("h264,video\n") == ("h264", "")
    assert media_prep._parse_probe_csv("") == ("", "")


async def test_compatible_video_is_not_transcoded(settings, engine, monkeypatch, tmp_path):
    """已经是 h264/aac 的 mp4 不该再转一遍（ComfyUI/H3 的输出就是这种）。"""
    from app.config import get_settings

    get_settings.cache_clear()
    _make_thread("u3", "t3")
    ws = settings.data_dir / "users" / "u3" / "threads" / "t3" / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    source = ws / "成片.mp4"
    source.write_bytes(b"fake")

    async def fake_probe(_path):
        return ("h264", "aac")

    monkeypatch.setattr(media_prep, "_probe_video", fake_probe)
    with Session(get_engine()) as session:
        artifact = Artifact(
            thread_id="t3", filename="成片.mp4", stored_path=str(source), kind="video", size=4
        )
        session.add(artifact)
        session.commit()
        artifact_id = artifact.id

    await media_prep.prepare_artifact(artifact_id)

    with Session(get_engine()) as session:
        updated = session.get(Artifact, artifact_id)
    assert updated is not None and updated.preview_path is None, "兼容格式不该生成转码副本"
    get_settings.cache_clear()

