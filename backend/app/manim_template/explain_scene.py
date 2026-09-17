"""数学讲解动画模板（Manim）——复制这个文件改内容即可。

渲染由后端负责：你在会话工作区里写
  .manim_request.json = {"script": "explain_math.py", "scene": "MathLesson",
                         "output": "讲解.mp4", "quality": "draft",
                         "narration": {"text": "…"}}
后端会先把旁白合成好，再把**逐句时间轴**写到本目录的 narration_meta.json，
然后渲染并把音轨合进成片。你只要保证：每一句旁白对应的动画，
在该句 start 时刻出现；句尾的 end 之前讲完。

对轴用 `wait_until(self, t)`：等到旁白第 t 秒（已经过去就立刻继续）。
"""

from __future__ import annotations

import json
from pathlib import Path

from manim import *

META = json.loads(Path("narration_meta.json").read_text(encoding="utf-8"))
SEGMENTS = META.get("segments") or []
DURATION = float(META.get("duration") or 0.0)
FONT = "Noto Sans CJK SC"


def wait_until(scene: Scene, t: float) -> None:
    """等到旁白时间轴的第 t 秒；已经过点就立刻返回。"""
    now = float(scene.renderer.time)
    if t > now:
        scene.wait(t - now)


class MathLesson(Scene):
    """把下面每一段换成你要讲的内容；seg 下标就是旁白第几句（从 0 开始）。"""

    def construct(self) -> None:
        # ---------- 第 1 句：起题 ----------
        title = Text("分数的意义", font=FONT, font_size=52).to_edge(UP, buff=0.8)
        wait_until(self, _start(0))
        self.play(FadeIn(title, shift=DOWN * 0.2), run_time=1.2)

        # ---------- 第 2 句：平均分 ----------
        circle = Circle(radius=1.6, color=BLUE)
        radius_v = Line(circle.get_top(), circle.get_bottom(), color=GREY_B)
        radius_h = Line(circle.get_left(), circle.get_right(), color=GREY_B)
        wait_until(self, _start(1))
        self.play(Create(circle), run_time=1.0)
        self.play(Create(radius_v), Create(radius_h), run_time=1.0)
        quarter = Sector(radius=1.6, angle=PI / 2, color=YELLOW, fill_opacity=0.6)
        self.play(FadeIn(quarter), run_time=0.8)

        # ---------- 第 3 句：几分之一 ----------
        label = MathTex(r"\frac{1}{4}").next_to(circle, DOWN, buff=0.4)
        wait_until(self, _start(2))
        self.play(Write(label), run_time=1.2)

        # ---------- 收尾：把最后一句讲完（不要提前结束） ----------
        wait_until(self, max(_end(len(SEGMENTS) - 1), DURATION - 0.4))


def _start(index: int) -> float:
    if 0 <= index < len(SEGMENTS):
        return float(SEGMENTS[index].get("start") or 0.0)
    return 0.0


def _end(index: int) -> float:
    if 0 <= index < len(SEGMENTS):
        return float(SEGMENTS[index].get("end") or 0.0)
    return DURATION
