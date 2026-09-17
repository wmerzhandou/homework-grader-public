import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { ChatMessage, GradingResult, KnowledgePoint, Material } from '../api/types'
import { useMediaTicket } from '../hooks/useMediaTicket'
import MaterialPreview, { type PreviewItem } from '../components/MaterialPreview'

const MASTERY_META: Record<
  KnowledgePoint['mastery'],
  { label: string; percent: number; barClass: string }
> = {
  good: { label: '掌握好', percent: 90, barClass: 'bg-emerald-500' },
  weak: { label: '需巩固', percent: 60, barClass: 'bg-amber-400' },
  poor: { label: '较薄弱', percent: 30, barClass: 'bg-red-400' },
}

const KIND_ICON: Record<string, string> = {
  document: '📄',
  image: '🖼️',
  audio: '🎤',
  video: '🎬',
}

/** 找到带批改结果的消息；材料优先取本消息，否则取之前最近一条用户消息的材料 */
function locateMessage(
  messages: ChatMessage[],
  messageId: string,
): { message: ChatMessage; materials: Material[] } | null {
  const idx = messages.findIndex((m) => m.id === messageId)
  if (idx < 0) return null
  const message = messages[idx]
  let materials = message.materials ?? []
  if (materials.length === 0) {
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].materials && messages[i].materials!.length > 0) {
        materials = messages[i].materials!
        break
      }
    }
  }
  return { message, materials }
}

export default function Report() {
  const { threadId, messageId } = useParams<{ threadId: string; messageId: string }>()
  // 报告页里的材料预览/下载同样走短时票据
  useMediaTicket()
  const [title, setTitle] = useState('')
  const [found, setFound] = useState<{ message: ChatMessage; materials: Material[] } | null>(null)
  const [preview, setPreview] = useState<PreviewItem | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    if (!threadId || !messageId) return
    setLoading(true)
    setError('')
    try {
      const detail = await api.getThread(threadId)
      setTitle(detail.title)
      const located = locateMessage(detail.messages, messageId)
      // 报告页只面向批改结果；其他任务类型（如英语短文）没有报告视图
      const resultType = located?.message.result_type
      if (!located || !located.message.grading_result || (resultType && resultType !== 'grading')) {
        setError('没有找到对应的批改结果')
      } else {
        setFound(located)
      }
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
  if (error || !found || !found.message.grading_result) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <p className="text-sm text-red-500">{error || '没有找到对应的批改结果'}</p>
        <Link to={`/threads/${threadId}`} className="text-sm text-emerald-600 underline">
          返回会话
        </Link>
      </div>
    )
  }

  const { message, materials } = found
  // 上面的 guard 已确保这是批改结果
  const result = message.grading_result as GradingResult
  const total = result.questions.length
  const correct = result.questions.filter((q) => q.correct).length

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
          <button
            onClick={() => window.print()}
            className="rounded-full bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700"
          >
            🖨 打印报告
          </button>
        </div>

        {/* 标题 */}
        <header className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
          <h1 className="text-xl font-bold sm:text-2xl">📋 批改报告 · {title || '作业批改'}</h1>
          <p className="mt-1 text-sm text-stone-400">
            {message.created_at
              ? new Date(message.created_at).toLocaleString()
              : ''}
            {` · 共 ${total} 题，答对 ${correct} 题`}
          </p>
        </header>

        {/* 总评 */}
        <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
          <h2 className="mb-2 font-bold text-stone-700">总评</h2>
          <p className="leading-relaxed text-stone-600">{result.summary}</p>
        </section>

        {/* 知识点掌握度条形图 */}
        {result.knowledge_points.length > 0 && (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
            <h2 className="mb-3 font-bold text-stone-700">知识点掌握度</h2>
            <div className="space-y-3">
              {result.knowledge_points.map((kp) => {
                const meta = MASTERY_META[kp.mastery]
                return (
                  <div key={kp.name}>
                    <div className="mb-1 flex items-center justify-between text-sm">
                      <span className="font-medium">{kp.name}</span>
                      <span className="text-stone-400">
                        {meta.label} · 约 {meta.percent}%
                      </span>
                    </div>
                    <div className="h-3 overflow-hidden rounded-full bg-stone-100 print:border print:border-stone-300">
                      <div
                        className={`h-full rounded-full ${meta.barClass} print:bg-stone-500`}
                        style={{ width: `${meta.percent}%` }}
                      />
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        )}

        {/* 材料缩略 */}
        {materials.length > 0 && (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
            <h2 className="mb-3 font-bold text-stone-700">提交的材料</h2>
            <div className="flex flex-wrap gap-3">
              {materials.map((m) =>
                m.kind === 'image' ? (
                  <button
                    key={m.id}
                    onClick={() =>
                      setPreview({ filename: m.filename, kind: m.kind, url: api.materialFileUrl(m.id) })
                    }
                    className="block transition hover:opacity-80 print:hidden"
                    title="点击查看大图"
                  >
                    <img
                      src={api.materialFileUrl(m.id)}
                      alt={m.filename}
                      className="h-24 w-24 rounded-xl border border-stone-200 object-cover"
                    />
                  </button>
                ) : (
                  <button
                    key={m.id}
                    onClick={() =>
                      setPreview({ filename: m.filename, kind: m.kind, url: api.materialFileUrl(m.id) })
                    }
                    className="rounded-full bg-stone-100 px-3 py-1 text-sm text-stone-500 transition hover:bg-stone-200"
                    title="点击预览/播放"
                  >
                    {KIND_ICON[m.kind] ?? '📄'} {m.filename}
                  </button>
                ),
              )}
            </div>
            {/* 打印时图片平铺显示 */}
            <div className="hidden flex-wrap gap-3 print:flex">
              {materials
                .filter((m) => m.kind === 'image')
                .map((m) => (
                  <img
                    key={m.id}
                    src={api.materialFileUrl(m.id)}
                    alt={m.filename}
                    className="h-24 w-24 rounded-xl border border-stone-200 object-cover"
                  />
                ))}
            </div>
          </section>
        )}

        {/* 逐题对照 */}
        {total > 0 && (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
            <h2 className="mb-3 font-bold text-stone-700">逐题对照</h2>
            <div className="space-y-4">
              {result.questions.map((q) => (
                <div
                  key={q.index}
                  className={`rounded-2xl border p-4 ${
                    q.correct
                      ? 'border-emerald-200 bg-emerald-50/50'
                      : 'border-red-200 bg-red-50/40'
                  } print:bg-white`}
                >
                  <p className="font-medium leading-relaxed">
                    <span
                      className={`mr-1 font-bold ${q.correct ? 'text-emerald-600' : 'text-red-500'}`}
                    >
                      {q.correct ? '✓' : '✗'} 第 {q.index} 题
                    </span>
                    {q.question}
                  </p>
                  <dl className="mt-2 space-y-1 text-sm">
                    <div>
                      <dt className="inline text-stone-400">孩子的答案：</dt>
                      <dd className={`inline ${q.correct ? 'text-emerald-700' : 'text-red-600'}`}>
                        {q.student_answer || '（未作答）'}
                      </dd>
                    </div>
                    <div>
                      <dt className="inline text-stone-400">正确答案：</dt>
                      <dd className="inline font-medium text-emerald-700">{q.correct_answer}</dd>
                    </div>
                    {q.error_reason && (
                      <div>
                        <dt className="inline text-stone-400">错因：</dt>
                        <dd className="inline">{q.error_reason}</dd>
                      </div>
                    )}
                  </dl>
                  {q.explanation && (
                    <p className="mt-2 rounded-xl bg-white/80 p-3 text-sm leading-relaxed text-stone-600 print:border print:border-stone-200">
                      {q.explanation}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 建议 */}
        {result.suggestions.length > 0 && (
          <section className="rounded-3xl bg-amber-50/80 p-5 shadow-sm sm:p-6 print:bg-white print:shadow-none">
            <h2 className="mb-2 font-bold text-amber-700 print:text-black">💡 给家长的建议</h2>
            <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-stone-600">
              {result.suggestions.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ul>
          </section>
        )}
      </div>
      {preview && <MaterialPreview item={preview} onClose={() => setPreview(null)} />}
    </div>
  )
}
