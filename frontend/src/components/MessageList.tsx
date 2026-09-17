import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import type { Artifact, ChatMessage, Material } from '../api/types'
import { useMediaTicket } from '../hooks/useMediaTicket'
import MaterialPreview, { type PreviewItem } from './MaterialPreview'
import MarkdownLink from './MarkdownLink'
import ResultView from './results'
import { splitReply, truncate } from '../lib/reply'

const KIND_ICON: Record<string, string> = {
  document: '📄',
  image: '🖼️',
  audio: '🎤',
  video: '🎬',
}

/** 纯文本回答超过这个长度（约 8 行）就在对话里折叠，全文进详情页阅读器 */
const LONG_TEXT_THRESHOLD = 400

/** 流式/本地的临时消息 id 在服务端不存在，不能进详情页 */
function isTempMessageId(id: string): boolean {
  return /^(streaming|result|local)-/.test(id)
}

/** 人类可读的文件大小 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${Math.round(bytes / 1024 ** 2)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

interface Props {
  messages: ChatMessage[]
  /** 正在流式生成的消息 id（用于打字光标） */
  streamingId?: string | null
  /** 当前会话 id，传给结果渲染器（报告链接、收藏错题用） */
  threadId?: string
}

export default function MessageList({ messages, streamingId, threadId }: Props) {
  // 材料缩略图用短时票据拼 URL；票据到位后重渲染一次把图加载出来
  useMediaTicket()
  const bottomRef = useRef<HTMLDivElement>(null)
  const [preview, setPreview] = useState<PreviewItem | null>(null)
  const last = messages[messages.length - 1]

  /** 用户上传的材料 → 预览对象 */
  function materialPreview(m: Material): PreviewItem {
    return { filename: m.filename, kind: m.kind, url: api.materialFileUrl(m.id) }
  }

  /** 模型产出的文件 → 预览对象 */
  function artifactPreview(a: Artifact): PreviewItem {
    return {
      filename: a.filename,
      kind: a.kind,
      // 图片/视频优先用预览副本（缩放、转码后），保证手机能看
      url:
        a.kind === 'image' || a.kind === 'video'
          ? api.artifactPreviewUrl(threadId ?? '', a.id)
          : api.artifactFileUrl(threadId ?? '', a.id),
    }
  }

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, last?.text])

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-stone-400">
        <span className="text-5xl">📸</span>
        <p className="max-w-xs text-sm leading-relaxed">
          拍下孩子的作业发给我，
          <br />
          我会逐题批改并给出讲解和建议
        </p>
      </div>
    )
  }

  return (
    <div className="flex-1 space-y-4 overflow-y-auto px-3 py-4 sm:px-6">
      {messages.map((msg) => {
        // 对话里只展示「归纳」；完整内容（详情 + 过程 + 产出物）在详情页
        const { summary, detail } = splitReply(msg.text)
        const short = truncate(summary, 160)
        const isLongAssistant = msg.role === 'assistant' && msg.text.length > LONG_TEXT_THRESHOLD
        const hasMore =
          short.truncated ||
          !!detail ||
          !!msg.grading_result ||
          (msg.artifacts?.length ?? 0) > 0
        const canOpenDetail = !!threadId && !isTempMessageId(msg.id) && hasMore
        return (
          <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[92%] sm:max-w-[80%] ${
                msg.role === 'user'
                  ? 'rounded-3xl rounded-br-md bg-emerald-600 px-4 py-3 text-white'
                  : 'w-full rounded-3xl rounded-bl-md bg-white px-4 py-3 shadow-sm'
              }`}
            >
              {msg.references && msg.references.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {msg.references.map((ref) => (
                    <Link
                      key={ref.thread_id}
                      to={`/threads/${ref.thread_id}`}
                      className="rounded-full bg-white/25 px-2.5 py-0.5 text-xs text-white transition hover:bg-white/40"
                      title="这条消息引用了该会话的材料与对话索引"
                    >
                      引用 #{ref.code}《{ref.title}》
                    </Link>
                  ))}
                </div>
              )}
              {msg.materials && msg.materials.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {msg.materials.map((m) =>
                    m.kind === 'image' ? (
                      <button
                        key={m.id}
                        onClick={() => setPreview(materialPreview(m))}
                        className="block overflow-hidden rounded-xl transition hover:opacity-80"
                        title={m.filename}
                      >
                        <img
                          src={api.materialFileUrl(m.id)}
                          alt={m.filename}
                          className="h-20 w-20 rounded-xl object-cover"
                          loading="lazy"
                        />
                      </button>
                    ) : (
                      <button
                        key={m.id}
                        onClick={() => setPreview(materialPreview(m))}
                        className={`rounded-full px-2.5 py-0.5 text-xs transition hover:opacity-75 ${
                          msg.role === 'user'
                            ? 'bg-emerald-500/60 text-emerald-50'
                            : 'bg-stone-100 text-stone-500'
                        }`}
                      >
                        {KIND_ICON[m.kind] ?? '📎'} {m.filename}
                      </button>
                    ),
                  )}
                </div>
              )}
              {msg.role === 'assistant' ? (
                <div className="relative">
                  <div
                    className={`markdown-body text-base ${
                      msg.id === streamingId ? 'stream-caret' : ''
                    }`}
                  >
                    <ReactMarkdown components={{ a: MarkdownLink }}>{short.text}</ReactMarkdown>
                  </div>
                </div>
              ) : (
                <p className="whitespace-pre-wrap break-words text-base leading-relaxed">
                  {msg.text}
                </p>
              )}
              {msg.artifacts && msg.artifacts.length > 0 && (
                <div className="mt-2 space-y-2">
                  {msg.artifacts
                    .filter((a) => a.kind === 'audio' && !a.oversized)
                    .map((a) => (
                      <div key={a.id} className="rounded-2xl bg-stone-50 px-3 py-2">
                        <div className="mb-1 truncate text-xs text-stone-500">
                          🎤 {a.filename}
                        </div>
                        <audio
                          controls
                          preload="none"
                          src={api.artifactFileUrl(threadId ?? '', a.id)}
                          className="w-full"
                        />
                      </div>
                    ))}
                  <div className="flex flex-wrap gap-1.5">
                    {msg.artifacts
                      .filter((a) => a.oversized || a.kind !== 'audio')
                      .map((a) =>
                        a.oversized ? (
                          <a
                            key={a.id}
                            href={api.artifactFileUrl(threadId ?? '', a.id)}
                            download={a.filename}
                            className="rounded-full bg-amber-50 px-2.5 py-0.5 text-xs text-amber-700 transition hover:bg-amber-100"
                            title="文件较大，未做内嵌预览，点击下载"
                          >
                            {KIND_ICON[a.kind] ?? '📎'} {a.filename}（{formatSize(a.size)}）
                          </a>
                        ) : a.kind === 'image' ? (
                          <button
                            key={a.id}
                            onClick={() => setPreview(artifactPreview(a))}
                            className="block overflow-hidden rounded-xl transition hover:opacity-80"
                            title={a.filename}
                          >
                            <img
                              src={api.artifactPreviewUrl(threadId ?? '', a.id)}
                              alt={a.filename}
                              className="h-24 w-24 rounded-xl object-cover"
                              loading="lazy"
                            />
                          </button>
                        ) : (
                          <button
                            key={a.id}
                            onClick={() => setPreview(artifactPreview(a))}
                            className="rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs text-emerald-700 transition hover:bg-emerald-100"
                            title={
                              a.kind === 'video' && !a.has_preview
                                ? '预览准备中（正在转码为浏览器可播放的格式），也可直接下载原文件'
                                : a.filename
                            }
                          >
                            {KIND_ICON[a.kind] ?? '📎'} {a.filename}
                            {a.kind === 'video' && !a.has_preview ? '（转码中…）' : ''}
                          </button>
                        ),
                      )}
                  </div>
                </div>
              )}
              {canOpenDetail && (
                <Link
                  to={`/threads/${threadId}/detail/${msg.id}`}
                  className="mt-1 inline-block rounded-full bg-stone-100 px-3 py-1 text-sm text-emerald-700 transition hover:bg-emerald-50"
                >
                  {isLongAssistant || detail ? '查看完整内容与过程 →' : '查看详情 →'}
                </Link>
              )}
              {msg.grading_result && (
                <div className="mt-3">
                  <ResultView
                    type={msg.result_type ?? undefined}
                    result={msg.grading_result}
                    threadId={threadId}
                    messageId={msg.id}
                  />
                </div>
              )}
            </div>
          </div>
        )
      })}
      <div ref={bottomRef} />
      {preview && <MaterialPreview item={preview} onClose={() => setPreview(null)} />}
    </div>
  )
}
