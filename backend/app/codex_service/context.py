"""Builds the model-facing context: material list block and turn input items."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings
from ..models import Material, MaterialKind, MaterialStatus, Message, Thread
from . import workspace

SUMMARY_CHAR_LIMIT = 500

_MATERIALS_GUIDANCE = """\
工作目录（cwd）里可能有作业材料文件。每份材料都在用户消息的材料清单中列出，包含文件名、类型、用途说明和预处理摘要。

重要：预处理结果可以直接信任，不要重复劳动——
- 音频/视频的"完整转写文本"已在清单中，直接使用，不要自己再转写。
- 图片和视频关键帧已作为图片输入直接提供，通常无需再读工作区文件；只有需要放大核对细节时才去读。
- 只有清单中缺少你需要的信息时，才用工具去读原始文件。"""

_OUTPUT_FILES_GUIDANCE = """\
关于"产出文件"（重要）：
- 如果用户要求生成音频、图片、文档等文件，直接写到当前工作目录即可，前端会自动把它们展示成
  可播放/可下载的卡片（音频出播放器、图片出缩略图）。
- **中间产物请写进 `.tmp/` 目录**（例如逐帧图片、临时切片、日志），只有最终要给用户看的文件才放在工作目录根下，
  否则中间产物会被当成成品塞进聊天窗口。
- 视频请尽量输出 **H.264 + mp4**（浏览器兼容性最好）；图片长边不超过 2000px，避免生成几十 MB 的大图。
- **不要在回复里写服务器上的绝对路径**（例如 /home/... 或 /Users/...），也不要写 Markdown 链接指向本地路径，
  那些链接在用户端打不开。只需要用文件名说明"已生成什么"即可，例如：已生成「短文朗读.mp3」。
- 回复里也不要粘贴无法访问的文件 URL。"""

_GENERATION_GUIDANCE = """\
关于"生成语音 / 生成图片"（沙箱没有网络，必须按下面的方式发起）：

**不要**在 shell 里调用 TTS 服务或 mmx，沙箱里连不通。要生成媒体时，请在**工作目录根目录**写一个请求文件，
后端会在本轮结束时执行，并把生成结果作为"产出物"展示给用户（音频出播放器、图片出缩略图）。

生成语音：写 `.tts_request.json`
    {"text": "要朗读的文本（≤2000 字）", "filename": "短文朗读.wav", "speed": 1.0}
  - 使用本机 IndexTTS-2.5；音色由服务器配置决定（现在固定为默认音色），voice 字段可省略。
  - 适合：朗读课文、生词、短文，给孩子跟读。

生成图片：写 `.image_request.json`
    {"prompt": "画面描述（中文即可）", "filename": "插图.jpg", "aspect_ratio": "1:1", "n": 1}
  - aspect_ratio 可用 1:1 / 16:9 / 4:3 / 3:4 / 9:16；n 最多 4 张。
  - 适合：给短文配插图、做图示卡片、生成练习情境图。

生成视频：写 `.video_request.json`
    {"prompt": "画面 + 声音的完整描述（含台词与所需环境音）", "filename": "成片.mp4",
     "seconds": 5, "resolution": "480p", "aspect_ratio": "16:9"}
  - seconds 2~15 秒；resolution 用 "480p"（草稿，1-2 分钟出片）或 "768p"（定稿，3-5 分钟）。
  - 想"让某张图动起来"时，加 "reference": "图片文件名"（必须是本次材料或工作区里的图片），走图生视频。
  - 视频生成很慢（分钟级），**一轮只发 1 个视频请求**，并且要在回复里告诉用户需要等一会儿。

选择音色（只对语音）：`.tts_request.json` 里可以指定引擎
    {"text": "…", "filename": "朗读.mp3", "engine": "mmx", "voice": "female-shaonv", "speed": 1.0, "emotion": "happy"}
  - 不写 engine 时用本机 IndexTTS-2.5（音色固定、中文自然，适合课文朗读）。
  - 写 "engine": "mmx" 时可选音色（云端音色库），常用中文音色：
    female-shaonv（少女）、female-yujie（御姐）、female-chengshu（成熟女声）、female-tianmei（甜美女声）、
    male-qn-qingse（青年男声）、male-qn-jingying（精英男声）、male-qn-badao（霸道男声）、
    clever_boy（聪明男孩）、cute_boy（可爱男孩）、lovely_girl（可爱女孩）。
  - emotion 可选 happy / sad / angry / fearful / surprised / calm / whisper；输出格式按文件后缀（.mp3/.wav）。

注意事项：
- 生成比较慢（语音 30~60 秒、图片约 30 秒），一轮里最多写 1~2 个请求，别一次写很多。
- 写完请求文件后，直接在回复里说明"已生成 xxx"，**不要**自己去读或复制那个 JSON 文件内容。
- 请求文件是隐藏文件（以 . 开头），不会被当成产出物。"""

_REFERENCE_GUIDANCE = """\
关于"引用其他会话"（用户会写 #XXXX 这样的会话码）：
- 后端会把被引用会话的**材料索引（含文件路径）与对话索引**放在用户消息末尾的【跨会话引用】块里。
- 索引里**没有文件内容**：需要细节时请用工具读取"可读文本"路径（文档=markdown、音视频=转写 txt）；
  图片已作为图片输入提供。**不要假装看过没读过的内容**，也不要整份乱读，按用户要求选择性读取即可。
- 用户给的会话码如果无效，后端会提示；你不需要自己去找或猜码。"""

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
3. **最终回复必须按下面的结构写**（前端会拆开：对话里只显示第一段，点开详情能看到全部）：

   归纳：<一句话概括结论，不超过 80 字>
   ---
   <详细内容：完整说明、原文、步骤、表格等。没有额外内容时这一部分可以留空>

4. 最终回复里**不要描述你自己的执行过程**（不要写"我先看一下…""接下来我要…""我读了某个文件"），
   过程叙述请放在 commentary 里，最终回复只讲结论和结果。
5. 细节和完整内容可以写进结果 JSON 文件，前端有专门的详情页展示。
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
    return (
        f"{base}\n{_OUTPUT_FILES_GUIDANCE}\n{_GENERATION_GUIDANCE}\n"
        f"{_REFERENCE_GUIDANCE}\n{GLOBAL_RULES}"
    )



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


# --------------------------------------------------------------------------
# 跨会话引用：只注入"索引 + 路径"，内容由模型按需读取
# --------------------------------------------------------------------------
_REFERENCE_RULES = """\
【跨会话引用】
下面列出的是**其他会话**里的材料与对话索引。为了不撑爆上下文，只给索引与文件路径，**没有直接注入文件内容**。
使用规则：
- 需要某份材料的具体内容时，用工具读取它对应的"可读文本"路径（文档已转成 markdown、音视频已转成转写 txt）；
  图片以图片输入的形式直接提供给你，不需要再读文件。
- **不要凭空推测没读过的内容**；用户点名某份文件时，只读那一份即可，不必全部读。
- 引用块里的路径只是给你读的，回复用户时不要粘贴服务器路径。"""


def _material_index_lines(material: Material, position: int) -> list[str]:
    head = f"    {position}. {material.filename} | {material.kind.value}"
    if material.purpose:
        head += f" | 用途：{material.purpose}"
    lines = [head]
    if material.stored_path:
        lines.append(f"        原文件：{material.stored_path}")
    if material.text_path:
        lines.append(f"        可读文本：{material.text_path}")
    elif material.kind == MaterialKind.document:
        lines.append("        可读文本：无（预处理未产出，可直接读原文件）")
    if material.kind == MaterialKind.image:
        lines.append("        （图片已作为图片输入提供）")
    return lines


def _result_digest(message: Message) -> str | None:
    """把批改/英语结果压缩成一行索引。"""
    if not message.grading_result_json:
        return None
    try:
        data = json.loads(message.grading_result_json)
    except (TypeError, ValueError):
        return None
    if message.result_type == "grading":
        summary = str(data.get("summary") or "").strip().replace("\n", " ")[:80]
        points = [str(p.get("name", "")) for p in data.get("knowledge_points") or []][:6]
        wrong = [
            str(q.get("index"))
            for q in data.get("questions") or []
            if not q.get("correct")
        ][:8]
        parts = []
        if summary:
            parts.append(f"总评「{summary}」")
        if points:
            parts.append("知识点：" + "、".join(p for p in points if p))
        if wrong:
            parts.append("错题：" + "、".join(w for w in wrong if w))
        return "｜".join(parts) or None
    if message.result_type == "english_passage":
        title = str(data.get("title") or "").strip()
        blanks = len(data.get("answers") or [])
        return f"标题「{title}」｜填空 {blanks} 题"
    return None


def build_reference_block(
    references: list[tuple[Thread, list[Material], list[Message]]],
    *,
    message_head_chars: int = 120,
    max_chars: int = 6000,
) -> str:
    """构造引用块：材料索引（含路径）+ 完整对话索引（每条取首行）+ 结果索引。"""
    if not references:
        return ""
    chunks: list[str] = [_REFERENCE_RULES]
    for thread, materials, messages in references:
        lines = [f"# {thread.code}《{thread.title}》"]
        if materials:
            lines.append(f"  材料索引（{len(materials)} 份，内容未注入）：")
            for position, material in enumerate(materials, start=1):
                lines.extend(_material_index_lines(material, position))
        else:
            lines.append("  材料索引：无")
        if messages:
            lines.append(f"  对话索引（{len(messages)} 条，仅取首行）：")
            for position, message in enumerate(messages, start=1):
                who = "用户" if message.role == "user" else "老师"
                # 只取首行（索引本来就是"目录"，不搬正文）
                first_line = (message.content or "").strip().splitlines()[0] if (message.content or "").strip() else ""
                head = first_line[:message_head_chars]
                lines.append(f"    {position}. [{who}] {head}")
            digests = [d for d in (_result_digest(m) for m in messages if m.role == "assistant") if d]
            if digests:
                lines.append(f"  批改结果索引：{digests[-1]}")
        chunks.append("\n".join(lines))

    block = "\n\n".join(chunks)
    if len(block) > max_chars:
        block = block[:max_chars].rstrip() + "\n…（引用索引过长，已按上限截断；需要更多内容请让用户缩小引用范围）"
    return block


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
    reference_block: str = "",
    reference_images: list[str] | None = None,
) -> list[dict]:
    """Full turn/start input list: user text + material block + image inputs."""
    block = build_materials_block(settings, materials)
    full_text = f"{text}\n\n{block}" if block else text
    if reference_block:
        full_text = f"{full_text}\n\n{reference_block}"
    items: list[dict] = [{"type": "text", "text": full_text}]
    items.extend(image_inputs_for_materials(settings, user_id, thread_id, materials))
    for path in reference_images or []:
        items.append({"type": "localImage", "path": path})
    return items
