/**
 * 把模型的最终答复拆成「归纳」和「详情」两部分。
 *
 * 新数据按约定格式：第一段是 `归纳：…`，然后一行 `---`，之后是详情。
 * 老数据没有标记，则退化成"第一段当归纳，其余当详情"。
 */
/** 过程叙述的典型措辞（历史消息里没有单独的 process_log，只能按句式剥离） */
const NARRATION_ANYWHERE =
  /(我先|我来|我把|我会|我再|我正在|我现在|我继续|我使用|我调用|我用了|我读|我看|我查|我找|我确认|我检查|我核对|我发现|我注意|我试|我需要先|我准备|接下来|让我|稍等|正在生成|正在处理)/

/** 指向服务器路径的句子（例如"结果写进了 [grading_result.json](/home/…)"）——文件已经用卡片呈现，这类句子属于过程 */
const MENTIONS_SERVER_PATH = /(\/home\/|\/workspace\/|\]\(\/|文件已保存到|写进了)/

function stripLeadingNarration(text: string, maxSentences = 6): string {
  let rest = text.trim()
  for (let i = 0; i < maxSentences; i += 1) {
    // 句子上限放宽到 400 字：带 Markdown 链接的句子会很长，80 字会导致整句匹配不上而放弃剥离
    const match = rest.match(/^([^。！？!?\n]{0,400}[。！？!?])\s*/)
    if (!match) break
    if (!NARRATION_ANYWHERE.test(match[1]) && !MENTIONS_SERVER_PATH.test(match[1])) break
    const remainder = rest.slice(match[0].length).trim()
    if (!remainder) break // 别把整段都剥没了
    rest = remainder
  }
  return rest.trim() || text.trim()
}

export function splitReply(text: string): { summary: string; detail: string; structured: boolean } {
  const normalized = (text ?? '').trim()
  if (!normalized) return { summary: '', detail: '', structured: false }

  const lines = normalized.split('\n')
  const markerIndex = lines.findIndex((line) => line.trim() === '---')
  if (markerIndex > 0) {
    const summary = lines
      .slice(0, markerIndex)
      .join('\n')
      .replace(/^\s*归纳\s*[:：]\s*/, '')
      .trim()
    const detail = lines.slice(markerIndex + 1).join('\n').trim()
    if (summary) return { summary, detail, structured: true }
  }

  // 历史消息：没有标记，就剥掉开头的"我先…"过程句，用剩下的第一段做归纳，
  // 全文仍然完整保留在详情页（detail 留空 → 详情页原样渲染 message.text）。
  const cleaned = stripLeadingNarration(normalized)
  const paragraphs = cleaned.split(/\n{2,}/)
  if (paragraphs.length > 1) {
    return { summary: paragraphs[0].trim(), detail: '', structured: false }
  }
  return { summary: cleaned, detail: '', structured: false }
}

/** 对话列表里用的归纳：超长才截断，并告诉调用方"被截断了"（据此决定是否给详情入口） */
export function truncate(text: string, max = 160): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false }
  return { text: `${cleanCut(text.slice(0, max))}…`, truncated: true }
}

/** 截断处如果正好切在 Markdown 链接中间，就把半截链接去掉，避免页面上出现 "[xxx](/home/user…" 这种残片 */
function cleanCut(text: string): string {
  let out = text.trimEnd()
  const openBracket = out.lastIndexOf('[')
  if (openBracket !== -1 && !out.slice(openBracket).includes(')')) {
    out = out.slice(0, openBracket).trimEnd()
  }
  const openParen = out.lastIndexOf('(')
  if (openParen > out.lastIndexOf(')')) {
    out = out.slice(0, openParen).trimEnd()
  }
  return out.replace(/[，、；:：\-–—\s]+$/, '')
}
