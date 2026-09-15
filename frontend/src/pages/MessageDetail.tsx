import { useCallback, useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import type { ChatMessage, EnglishPassageResult } from '../api/types'
import EnglishPassageCard from '../components/results/EnglishPassage'

/**
 * 消息详情页：按消息的 result_type 渲染完整内容。
 * - grading：直接跳到批改报告页
 * - english_passage：完整英语短文（逐句对照 + 点读 + 单词卡）
 * - 其他（无结果的长回答）：阅读器模式，大字号宽松行距、打印友好
 */
export default function MessageDetail() {
  const { threadId, messageId } = useParams<{ threadId: string; messageId: string }>()
  const [title, setTitle] = useState('')
  const [message, setMessage] = useState<ChatMessage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    if (!threadId || !messageId) return
    setLoading(true)
    setError('')
    try {
      const detail = await api.getThread(threadId)
      setTitle(detail.title)
      const msg = detail.messages.find((m) => m.id === messageId)
      if (!msg) setError('没有找到对应的消息')
      else setMessage(msg)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [threadId, messageId])

  useEffect(() => {
    load()
  }, [load])

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-stone-400">加载中…</div>
    )
  }
  if (error || !message) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <p className="text-sm text-red-500">{error || '没有找到对应的消息'}</p>
        <Link to={`/threads/${threadId}`} className="text-sm text-emerald-600 underline">
          返回会话
        </Link>
      </div>
    )
  }

  // 批改结果的完整视图是报告页
  if (message.result_type === 'grading' && message.grading_result) {
    return <Navigate to={`/threads/${threadId}/report/${message.id}`} replace />
  }

  const isEnglishPassage = message.result_type === 'english_passage' && message.grading_result

  return (
    <div className="h-full overflow-y-auto px-3 py-4 sm:px-6">
      <div className="mx-auto max-w-3xl space-y-6 pb-10">
        {/* 顶部导航（打印时隐藏） */}
        <div className="flex items-center justify-between print:hidden">
          <Link
            to={`/threads/${threadId}`}
            className="rounded-full bg-white px-4 py-1.5 text-sm text-stone-500 shadow-sm transition hover:bg-stone-100"
          >
            ‹ 返回会话
          </Link>
          {!isEnglishPassage && (
            <button
              onClick={() => window.print()}
              className="rounded-full bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700"
            >
              🖨 打印
            </button>
          )}
        </div>

        {/* 标题栏 */}
        <header className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
          <h1 className="text-xl font-bold sm:text-2xl">
            {isEnglishPassage ? '📖 ' : '📄 '}
            {title || '会话'}
          </h1>
          {message.created_at && (
            <p className="mt-1 text-sm text-stone-400">
              {new Date(message.created_at).toLocaleString()}
            </p>
          )}
        </header>

        {isEnglishPassage ? (
          <EnglishPassageCard result={message.grading_result as EnglishPassageResult} />
        ) : (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-8 print:shadow-none">
            <div className="markdown-body text-lg leading-loose">
              <ReactMarkdown>{message.text}</ReactMarkdown>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}
