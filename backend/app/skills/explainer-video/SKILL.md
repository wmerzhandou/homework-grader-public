---
name: explainer-video
description: 用 HTML 动画做"讲解视频"（讲题、知识点、步骤演示、图解、带字幕的口播）。当用户要"讲解视频/教学视频/动画讲解/把这两道题讲一遍/做个知识点动画"时使用。不要用 AI 生成镜头（.video_request.json / H3）来做讲解类内容。
---

# 讲解视频（HTML 动画渲染）

**先定旁白，再让画面跟着旁白走。** 这是避免"话没说完画面就切走"的唯一可靠做法。

## 什么时候用它

- 讲一道题 / 一个知识点 / 一组步骤；
- 需要**字幕与旁白严格对齐**、需要**逐条展开**的教学内容；
- 图解、流程、对比、公式推导。

**不要**用它做写实画面（"小猫在草地上"这种氛围镜头）——那类走 `.video_request.json`（H3 农场）。

**要先分流**：如果需要**严格排版的数学内容**（分数、根号、上下标、公式变形、几何证明、
数轴数形结合），走 **Manim**（下面第二节），画面比 HTML 精致得多；其余讲解仍用 HTML 动画。

## 第二节：数学动画（Manim）

请求文件（隐藏文件）：`.manim_request.json`

```json
{
  "script": "explain_math.py",
  "scene": "MathLesson",
  "output": "分数的意义讲解.mp4",
  "quality": "draft",
  "narration": { "text": "把一个整体，平均分成若干份。\n\n其中的一份，就是几分之一。" }
}
```

- `quality`：**默认 `draft` = 1280×720@30**（够看、文件小）；只有用户明确要"高清/1080p"才用 `high` = 1920×1080@30；
- `scene` 是脚本里的类名；脚本要放在会话工作区根下；
- 模板：`backend/app/manim_template/explain_scene.py`，**复制改内容**即可。

**后端做的事**（你不必自己合成音频，也不要在沙箱里跑 manim）：

1. 先合成旁白（同讲解视频的慢速 + 留白 + 音色配置）；
2. 把逐句时间轴写成脚本同目录的 `narration_meta.json`：

```json
{"duration": 23.73, "fps": 30, "resolution": [1920, 1080],
 "segments": [{"index": 0, "text": "…", "start": 0.83, "end": 3.21}]}
```

3. 跑 `manim render` 渲染；
4. 把旁白音轨合进成片（成片比旁白短就冻结最后一帧补齐），成片与配音一起交付。

**硬要求**

1. 脚本开头读时间轴，动画挂在句子时间上（模板里的 `wait_until(self, t)`）；
2. 中文字体必须 `font="Noto Sans CJK SC"`（否则显示方块）；
3. 公式用 `MathTex(r"\frac{1}{2}")`，中文用 `Text(...)`；
4. 结尾等到 `meta["duration"]`（别提前结束，否则最后一句没画面）；
5. 一屏只讲一件事；`run_time` 之和不要超过对应句子的时长。

## 请求文件

在工作目录根下写 `.render_request.json`（隐藏文件，不会变成产出物）：

```json
{
  "composition": "explain.html",
  "output": "两道题讲解.mp4",
  "quality": "draft",
  "narration": { "text": "先看第一题。\n\n把两个分母变成一样的，就好算了。", "voice": "default" }
}
```

- `narration.engine`：省略=`index_tts`（本机 IndexTTS-2.5，中文自然）；写 `"mmx"` 可选音色（如 `female-tianna`/`female-tianmei`）。
- 也可以用 `"audio": "已有的配音.mp3"` 直接用工作区里现成的音频。
- `quality`：**默认 `draft`（成片统一压到 720p，够看又轻）**；只有用户明确说"要高清/1080p"才用 `high`。
- 后端会：合成旁白 → 探测时长 → **把时长注入组合**（变量 `narrationDuration` + 改写根节点 `data-duration`）
  → 自动把 `<audio>` 挂到根节点下 → 渲染成 mp4 → 作为产出物交付（配音 mp3 也会一起给）。

## 语速与节奏（讲解视频听起来"赶"就是这里没做对）

**默认已经是慢速讲解档**（后端按 `pace=narration` 处理）：语速 0.9 倍、句末留白 0.75 秒、
段末 1.4 秒、结尾 0.5 秒。你**不需要**自己拼静音，但要在写旁白时配合它：

1. **短句**：每句 10~20 字，一句话一件事，句末用 `。`。逗号串成长句 = 听着喘不过气。
2. **换行分步**：讲完一步就空一行（空行 = 1.4 秒停顿），画面也正好一步一幕。
3. **要点前留白**：在结论前插 `[[pause:1.2]]`，让观众有一点"想一下"的时间。
4. **估算时长按 2 字/秒**（已含停顿与留白，这是实测值）：15 秒的片子约 30 字旁白、20 秒约 40 字，宁可少说。
5. 想整体更慢/更快：`"narration": {"text": "…", "speed": 0.85}`（0.5~2.0）；
   想单独调留白：`"pauses": {"sentence": 0.9, "paragraph": 1.5, "tail": 0.6}`。

> 后端做的事：先按标点**分句**合成，再在句间插入静音，最后做**保音高**的语速调整
> （rubberband，缺失时退回 atempo）。所以"慢"不会变成低音、拖长音。

## 组合（HTML）契约速查

```html
<html data-composition-variables='[{"id":"narrationDuration","type":"number","label":"旁白时长","default":20,"min":3,"max":600}]'>
  ...
  <div id="root" data-composition-id="main" data-start="0" data-duration="20"
       data-width="1920" data-height="1080">
     <h1 class="clip" data-start="0" data-duration="20" data-track-index="0">标题</h1>
     <!-- 字幕条 / 讲解卡片同样是 .clip，用 data-start/data-duration 控制出现区间 -->
     <!-- 旁白 <audio> 由后端自动插入；你也可以自己写，但必须是 #root 的直接子元素 -->
  </div>
  <script src="assets/gsap.min.js"></script>
  <script>
    const { narrationDuration } = window.__hyperframes.getVariables();
    const D = Math.max(3, Number(narrationDuration) || 20);
    // 所有时间点都用 D 的比例来算，不要写死秒数
    const tl = gsap.timeline({ paused: true });
    window.__timelines["main"] = tl;   // key 必须等于 data-composition-id
    tl.seek(0);
  </script>
</html>
```

**硬性要求**

1. **一个** 暂停的 GSAP 时间轴，注册到 `window.__timelines["<data-composition-id>"]`，且必须**同步**创建；
2. 动画时间全部由 `narrationDuration` 推算（比例式），不要在 `.clip` 上写死比旁白更短的时长；
3. GSAP 引 `assets/gsap.min.js`（本地），**禁止 CDN**；
4. 中文字体用 `"Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Zen Hei", sans-serif`；
5. 字幕：底部单行、字号 ≈44px、深色半透明底 + 白字；一句话一行，按旁白分段切换；
6. 信息密度：一屏只讲一件事；数字/结论用高亮色（如 `#fbbf24`）。

## 可直接复制的骨架

`backend/app/render_template/composition-skeleton.html` 是一份**已验证可用**的骨架（标题 + 两张讲解卡片 + 底部字幕 + 按旁白时长排的时间轴），复制改内容即可。渲染实测：1920×1080 十秒素材约 15 秒出片。

## 交付前自检

- 时间轴上最后一个动作的结束时间 ≤ `narrationDuration`；
- 每个 `.clip` 的 `data-duration` 不小于它需要停留的时长；
- 字幕文本与旁白一一对应（不要出现旁白没说的字幕，或字幕停留时间与旁白不一致）。
