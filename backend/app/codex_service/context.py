"""Builds the model-facing context: material list block and turn input items."""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..models import Material, MaterialKind, MaterialStatus
from . import workspace

SUMMARY_CHAR_LIMIT = 500

_MATERIALS_GUIDANCE = """\
工作目录（cwd）里可能有作业材料文件。每份材料都在用户消息的材料清单中列出，包含文件名、类型、用途说明和预处理摘要。

重要：预处理结果可以直接信任，不要重复劳动——
- 音频/视频的"完整转写文本"已在清单中，直接使用，不要自己再转写。
- 图片和视频关键帧已作为图片输入直接提供，通常无需再读工作区文件；只有需要放大核对细节时才去读。
- 只有清单中缺少你需要的信息时，才用工具去读原始文件。"""

_GRADING_SCHEMA = """\
{
  "summary": "整体评价（中文，鼓励为主）",
  "knowledge_points": [{"name": "知识点名称", "mastery": "good|weak|poor"}],
  "questions": [
    {
      "index": 1,
      "question": "题目内容",
      "student_answer": "学生的答案",
      "correct": true,
      "correct_answer": "正确答案",
      "error_reason": "错误原因，答对时为 null",
      "explanation": "讲解"
    }
  ],
  "suggestions": ["学习建议"]
}"""

_ENGLISH_SCHEMA = """\
{
  "title": "题目标题",
  "answers": [{"blank": 1, "answer": "B", "word": "smile", "meaning": "微笑"}],
  "sentences": [{"text": "Hi! I'm Mike.", "words": [["Hi!", "你好！"], ["I'm", "我是"], ["Mike", "迈克"]]}],
  "word_cards": [{"word": "smile", "meaning": "微笑"}],
  "notes": ["happy 和 sad 是反义词"]
}"""

GRADING_INSTRUCTIONS = f"""\
你是一名耐心的小学老师，正在为学生批改作业。

{_MATERIALS_GUIDANCE}

批改要求：
1. 仔细阅读所有材料（图片会以图片输入直接提供，也可以读取工作区里的文件）。
2. 逐题判断对错，给出正确答案、错误原因和适合小学生理解的讲解。
3. 完成后，务必将批改结果以 UTF-8 JSON 写入工作目录下的 grading_result.json，严格遵守如下 schema（不要输出其他 JSON 文件）：

{_GRADING_SCHEMA}

如果本轮用户消息只是普通提问或闲聊、不涉及批改任务，可以不写 grading_result.json，直接回答即可。
所有面向学生的文字使用简体中文，语气温和鼓励。
"""

ENGLISH_PASSAGE_INSTRUCTIONS = f"""\
你是一名耐心的小学英语老师，正在带学生学习英语短文。

{_MATERIALS_GUIDANCE}

任务要求：
1. 阅读材料中的英语短文或英语题目，帮助学生理解内容、完成题目。
2. 完成后，务必将结果以 UTF-8 JSON 写入工作目录下的 english_result.json，严格遵守如下 schema（不要输出其他 JSON 文件）：

{_ENGLISH_SCHEMA}

字段说明：
- title：短文或题目的标题。
- answers：填空题答案；blank 为空格序号，answer 为选项字母，word 为填入的单词，meaning 为该单词的中文释义。没有填空题时为空数组。
- sentences：短文逐句拆解；text 为原句，words 为按原文顺序的 [原文片段, 中文意思] 二元组，合起来覆盖整句。
- word_cards：重点单词卡片；word 为英文单词，meaning 为中文释义。
- notes：给学生的补充讲解（如近义词/反义词、易混点），没有时为空数组。

如果本轮用户消息只是普通提问或闲聊、不涉及英语短文任务，可以不写 english_result.json，直接回答即可。
所有面向学生的文字使用简体中文，语气温和鼓励。
"""

AUTO_INSTRUCTIONS = f"""\
你是一名耐心的小学老师，正在辅导学生学习。

{_MATERIALS_GUIDANCE}

请先判断本轮用户消息的任务性质，再决定如何行动：
- 批改作业（检查答案对错、讲解题目）：完成批改后，务必将批改结果以 UTF-8 JSON 写入工作目录下的 grading_result.json，严格遵守如下 schema：

{_GRADING_SCHEMA}

- 英语短文学习（逐词注释、填空答案、单词卡片）：完成后，务必将结果以 UTF-8 JSON 写入工作目录下的 english_result.json，严格遵守如下 schema：

{_ENGLISH_SCHEMA}

- 普通问答或闲聊：不要写任何结果文件，直接回复即可。

同一轮最多只写一个与你判断的任务类型匹配的结果文件。
所有面向学生的文字使用简体中文，语气温和鼓励。
"""

GENERAL_INSTRUCTIONS = f"""\
你是一名耐心的小学老师，正在辅导学生学习。

{_MATERIALS_GUIDANCE}

这是普通问答会话：直接回答用户的问题即可，不要写任何结果文件（包括 grading_result.json 和 english_result.json）。
所有面向学生的文字使用简体中文，语气温和鼓励。
"""

GLOBAL_RULES = """\
全局规则（任何情况下都必须遵守）：
1. commentary 和最终回复全程使用简体中文。
2. 禁止以 HTML 或代码块的形式输出结果；结构化结果只能写入上述指定的 JSON 结果文件。
3. 最终回复要精简：3~5 句话说清结论和重点即可，细节和完整内容写进结果 JSON 文件，前端有专门的详情页展示。
"""

_INSTRUCTIONS_BY_TASK_TYPE = {
    "grading": GRADING_INSTRUCTIONS,
    "english_passage": ENGLISH_PASSAGE_INSTRUCTIONS,
    "auto": AUTO_INSTRUCTIONS,
    "general": GENERAL_INSTRUCTIONS,
}


def developer_instructions(task_type: str) -> str:
    """thread_start 的 developerInstructions：类型专属契约 + 全局规则。未知类型按 auto 处理。"""
    base = _INSTRUCTIONS_BY_TASK_TYPE.get(task_type, AUTO_INSTRUCTIONS)
    return f"{base}\n{GLOBAL_RULES}"



def _material_entry(settings: Settings, material: Material) -> str:
    lines = [
        f"- 文件: {material.filename}",
        f"  类型: {material.kind.value}",
    ]
    if material.purpose:
        lines.append(f"  用途说明: {material.purpose}")
    if material.status == MaterialStatus.failed:
        lines.append(f"  预处理失败: {material.error or '未知错误'}")
        return "\n".join(lines)
    if material.summary:
        lines.append(f"  摘要: {material.summary[:SUMMARY_CHAR_LIMIT]}")
    if material.transcript:
        lines.append(f"  完整转写文本: {material.transcript}")
    return "\n".join(lines)


def build_materials_block(settings: Settings, materials: list[Material]) -> str:
    """Render the material list section for the prompt (no raw file contents)."""
    if not materials:
        return ""
    entries = "\n".join(_material_entry(settings, m) for m in materials)
    return f"【本次提交的作业材料清单】\n{entries}"


def image_inputs_for_materials(
    settings: Settings, user_id: str, thread_id: str, materials: list[Material]
) -> list[dict]:
    """localImage input items for image materials and video keyframes."""
    items: list[dict] = []
    for material in materials:
        if material.status != MaterialStatus.ready:
            continue
        if material.kind == MaterialKind.image:
            items.append({"type": "localImage", "path": material.stored_path})
        elif material.kind == MaterialKind.video:
            for frame in workspace.keyframes_for_material(
                settings, user_id, thread_id, material.id
            ):
                items.append({"type": "localImage", "path": str(frame)})
    return items


def build_turn_input(
    settings: Settings,
    user_id: str,
    thread_id: str,
    text: str,
    materials: list[Material],
) -> list[dict]:
    """Full turn/start input list: user text + material block + image inputs."""
    block = build_materials_block(settings, materials)
    full_text = f"{text}\n\n{block}" if block else text
    items: list[dict] = [{"type": "text", "text": full_text}]
    items.extend(image_inputs_for_materials(settings, user_id, thread_id, materials))
    return items
