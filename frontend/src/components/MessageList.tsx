import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import type { ChatMessage, Material } from '../api/types'
import { useMediaTicket } from '../hooks/useMediaTicket'
import MaterialPreview from './MaterialPreview'
import ResultView from './results'

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
  const [preview, setPreview] = useState<Material | null>(null)
  const last = messages[messages.length - 1]

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
        // 长回答折叠：max-height 截断 + 渐变遮罩 + 详情页入口
        const isLongAssistant = msg.role === 'assistant' && msg.text.length > LONG_TEXT_THRESHOLD
        const canOpenDetail = isLongAssistant && !!threadId && !isTempMessageId(msg.id)
        return (
          <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[92%] sm:max-w-[80%] ${
                msg.role === 'user'
                  ? 'rounded-3xl rounded-br-md bg-emerald-600 px-4 py-3 text-white'
                  : 'w-full rounded-3xl rounded-bl-md bg-white px-4 py-3 shadow-sm'
              }`}
            >
              {msg.materials && msg.materials.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-1.5">
                  {msg.materials.map((m) =>
                    m.kind === 'image' ? (
                      <button
                        key={m.id}
                        onClick={() => setPreview(m)}
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
                        onClick={() => setPreview(m)}
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
                    } ${isLongAssistant ? 'max-h-56 overflow-hidden' : ''}`}
                  >
                    <ReactMarkdown>{msg.text}</ReactMarkdown>
                  </div>
                  {isLongAssistant && (
                    <div className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-white via-white/80 to-transparent" />
                  )}
                </div>
              ) : (
                <p className="whitespace-pre-wrap break-words text-base leading-relaxed">
                  {msg.text}
                </p>
              )}
              {canOpenDetail && (
                <Link
                  to={`/threads/${threadId}/detail/${msg.id}`}
                  className="mt-1 inline-block rounded-full bg-stone-100 px-3 py-1 text-sm text-emerald-700 transition hover:bg-emerald-50"
                >
                  查看全文排版 →
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
      {preview && <MaterialPreview material={preview} onClose={() => setPreview(null)} />}
    </div>
  )
}
