import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import type { GradedQuestion, GradingResult, KnowledgePoint } from '../api/types'
import { api } from '../api/client'
import { useSpeech } from '../hooks/useSpeech'

const MASTERY_STYLE: Record<KnowledgePoint['mastery'], { label: string; className: string }> = {
  good: { label: '掌握好', className: 'bg-emerald-100 text-emerald-700' },
  weak: { label: '需巩固', className: 'bg-amber-100 text-amber-700' },
  poor: { label: '较薄弱', className: 'bg-red-100 text-red-600' },
}

/** 把讲解按句号/问号/叹号/换行切成 2-4 个步骤，用于分步揭示 */
function splitExplanation(text: string): string[] {
  const sentences = text
    .split(/(?<=[。！？!?])|\n+/)
    .map((s) => s.trim())
    .filter(Boolean)
  if (sentences.length <= 1) return [text.trim()].filter(Boolean)
  const stepCount = Math.min(4, Math.max(2, Math.ceil(sentences.length / 2)))
  const perStep = Math.ceil(sentences.length / stepCount)
  const steps: string[] = []
  for (let i = 0; i < sentences.length; i += perStep) {
    steps.push(sentences.slice(i, i + perStep).join(''))
  }
  return steps
}

const CONFETTI_COLORS = ['#059669', '#f59e0b', '#ef4444', '#3b82f6', '#a855f7', '#facc15']

/** 确定性伪随机（撒花只播一次，无需真随机；也避免 render 中调用 impure 函数） */
function seeded(seed: number) {
  const x = Math.sin(seed * 127.1) * 43758.5453
  return x - Math.floor(x)
}

/** 全对时的纯 CSS 撒花动画，播一次后自动移除 */
function Confetti() {
  const [done, setDone] = useState(false)
  const pieces = useMemo(
    () =>
      Array.from({ length: 40 }, (_, i) => ({
        id: i,
        left: seeded(i + 1) * 100,
        delay: seeded(i + 11) * 0.6,
        duration: 2 + seeded(i + 21) * 1.5,
        size: 6 + seeded(i + 31) * 6,
        color: CONFETTI_COLORS[i % CONFETTI_COLORS.length],
        rotate: seeded(i + 41) * 360,
        round: i % 3 === 0,
      })),
    [],
  )

  useEffect(() => {
    const timer = setTimeout(() => setDone(true), 4000)
    return () => clearTimeout(timer)
  }, [])

  if (done) return null
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden rounded-3xl">
      {pieces.map((p) => (
        <span
          key={p.id}
          className="confetti-piece"
          style={
            {
              left: `${p.left}%`,
              width: p.size,
              height: p.size * (p.round ? 1 : 0.5),
              backgroundColor: p.color,
              borderRadius: p.round ? '50%' : 2,
              animationDelay: `${p.delay}s`,
              animationDuration: `${p.duration}s`,
              '--confetti-rotate': `${p.rotate + 360}deg`,
            } as CSSProperties
          }
        />
      ))}
    </div>
  )
}

function AccuracyRing({ correct, total }: { correct: number; total: number }) {
  const percent = total === 0 ? 0 : Math.round((correct / total) * 100)
  const r = 34
  const circumference = 2 * Math.PI * r
  const offset = circumference * (1 - percent / 100)
  const color = percent >= 80 ? '#059669' : percent >= 50 ? '#d97706' : '#dc2626'

  return (
    <div className="relative h-24 w-24 shrink-0">
      <svg viewBox="0 0 80 80" className="h-full w-full -rotate-90">
        <circle cx="40" cy="40" r={r} fill="none" stroke="#e7e5e4" strokeWidth="8" />
        <circle
          cx="40"
          cy="40"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          className="transition-all duration-700"
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-bold" style={{ color }}>
          {percent}%
        </span>
        <span className="text-[10px] text-stone-400">正确率</span>
      </div>
    </div>
  )
}

interface QuestionCardProps {
  q: GradedQuestion
  threadId?: string
  messageId?: string
  /** 收藏错题时兜底使用的知识点名 */
  defaultKnowledgePoint: string
}

function QuestionCard({ q, threadId, messageId, defaultKnowledgePoint }: QuestionCardProps) {
  const [step, setStep] = useState(0)
  const [collecting, setCollecting] = useState(false)
  const [collected, setCollected] = useState(false)
  const [collectError, setCollectError] = useState('')
  const { speaking, synthesizing, speak, stop } = useSpeech()

  const steps = useMemo(() => splitExplanation(q.explanation), [q.explanation])
  const hasSteps = !q.correct && steps.length > 0
  const revealed = step >= steps.length

  async function handleCollect() {
    if (collected || collecting) return
    setCollecting(true)
    setCollectError('')
    try {
      await api.addMistake({
        knowledge_point: defaultKnowledgePoint,
        question: q.question,
        student_answer: q.student_answer,
        correct_answer: q.correct_answer,
        error_reason: q.error_reason,
        explanation: q.explanation,
        source_thread_id: threadId ?? '',
        source_message_id: messageId ?? '',
      })
      setCollected(true)
    } catch (err) {
      setCollectError(err instanceof Error ? err.message : '收藏失败')
    } finally {
      setCollecting(false)
    }
  }

  return (
    <div
      className={`rounded-2xl border p-4 ${
        q.correct ? 'border-emerald-200 bg-emerald-50/60' : 'border-red-200 bg-red-50/50'
      }`}
    >
      <div className="flex items-start gap-3">
        <span
          className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-base font-bold text-white ${
            q.correct ? 'bg-emerald-500' : 'bg-red-400'
          }`}
        >
          {q.correct ? '✓' : '✗'}
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-medium leading-relaxed">
            <span className="mr-1 text-stone-400">第 {q.index} 题</span>
            {q.question}
          </p>
          <p className="mt-2 text-sm">
            <span className="text-stone-400">孩子的答案：</span>
            <span className={q.correct ? 'text-emerald-700' : 'text-red-600'}>
              {q.student_answer || '（未作答）'}
            </span>
          </p>
          {!q.correct && (
            <>
              {/* 有分步讲解时，正确答案到最后一步才揭示 */}
              {(!hasSteps || revealed) && (
                <p className="mt-1 text-sm">
                  <span className="text-stone-400">正确答案：</span>
                  <span className="font-medium text-emerald-700">{q.correct_answer}</span>
                </p>
              )}
              {q.error_reason && (
                <p className="mt-1 text-sm">
                  <span className="text-stone-400">错因：</span>
                  {q.error_reason}
                </p>
              )}
            </>
          )}

          {/* 错题操作：分步讲解 / 朗读 / 收藏 */}
          {!q.correct && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {hasSteps && step === 0 && (
                <button
                  onClick={() => setStep(1)}
                  className="rounded-full bg-white px-3 py-1 text-sm text-emerald-700 shadow-sm transition hover:bg-emerald-50"
                >
                  👂 讲给孩子听
                </button>
              )}
              {hasSteps && step > 0 && !revealed && (
                <button
                  onClick={() => setStep((s) => Math.min(s + 1, steps.length))}
                  className="rounded-full bg-emerald-600 px-3 py-1 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700"
                >
                  下一步（{step}/{steps.length}）→
                </button>
              )}
              {q.explanation && (
                <button
                  onClick={() => (speaking ? stop() : speak(q.explanation))}
                  disabled={synthesizing}
                  className="rounded-full bg-white px-3 py-1 text-sm text-emerald-700 shadow-sm transition hover:bg-emerald-50 disabled:cursor-wait disabled:opacity-60"
                >
                  {synthesizing ? '⏳ 合成中…' : speaking ? '⏹ 停止' : '🔊 听讲解'}
                </button>
              )}
              <button
                onClick={handleCollect}
                disabled={collected || collecting}
                className={`rounded-full px-3 py-1 text-sm shadow-sm transition ${
                  collected
                    ? 'cursor-default bg-stone-100 text-stone-400'
                    : 'bg-white text-amber-600 hover:bg-amber-50'
                }`}
              >
                {collected ? '✓ 已收藏' : collecting ? '收藏中…' : '⭐ 加入错题本'}
              </button>
            </div>
          )}
          {collectError && <p className="mt-1 text-xs text-red-500">{collectError}</p>}

          {/* 分步揭示的讲解内容 */}
          {hasSteps && step > 0 && (
            <div className="mt-2 space-y-2">
              {steps.slice(0, step).map((s, i) => (
                <p
                  key={i}
                  className="rounded-xl bg-white/80 p-3 text-sm leading-relaxed text-stone-600"
                >
                  <span className="mr-1 font-bold text-emerald-600">第 {i + 1} 步</span>
                  {s}
                </p>
              ))}
              {revealed && (
                <p className="text-xs text-stone-400">🎉 讲解完成，再想想为什么这样做吧</p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

interface GradingCardProps {
  result: GradingResult
  threadId?: string
  messageId?: string
}

/** 对话流里的精简摘要卡：正确率环 + 对错计数 + 总评预览，完整内容在报告页 */
export function GradingSummary({ result, threadId, messageId }: GradingCardProps) {
  const total = result.questions.length
  const correct = result.questions.filter((q) => q.correct).length
  // 流式中的临时消息没有真实 id，不提供报告入口
  const canReport =
    !!threadId && !!messageId && !/^(streaming|result|local)-/.test(messageId)

  return (
    <div className="space-y-3 rounded-3xl bg-white p-4 shadow-sm">
      <div className="flex items-center gap-4">
        <AccuracyRing correct={correct} total={total} />
        <div className="min-w-0 flex-1">
          <p className="text-sm">
            <span className="font-bold text-emerald-600">对 {correct}</span>
            <span className="mx-1.5 text-stone-300">·</span>
            <span className="font-bold text-red-500">错 {total - correct}</span>
            <span className="ml-1.5 text-stone-400">/ 共 {total} 题</span>
          </p>
          <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-stone-600">
            {result.summary}
          </p>
        </div>
      </div>
      {canReport && (
        <Link
          to={`/threads/${threadId}/report/${messageId}`}
          className="block rounded-2xl border border-emerald-200 bg-emerald-50/60 px-4 py-2.5 text-center text-sm font-medium text-emerald-700 transition hover:bg-emerald-100"
        >
          查看完整批改 →
        </Link>
      )}
    </div>
  )
}

/** 完整批改卡（逐题讲解 + 收藏错题），目前报告页有自己的排版，此组件保留备用 */
export default function GradingCard({ result, threadId, messageId }: GradingCardProps) {
  const total = result.questions.length
  const correct = result.questions.filter((q) => q.correct).length
  const allCorrect = total > 0 && correct === total
  const defaultKnowledgePoint = result.knowledge_points[0]?.name ?? ''
  // 流式中的临时消息没有真实 id，不提供报告入口
  const canReport =
    !!threadId && !!messageId && !/^(streaming|result|local)-/.test(messageId)

  return (
    <div className="relative space-y-4 rounded-3xl bg-white p-4 shadow-sm sm:p-6">
      {allCorrect && <Confetti />}

      {/* 总评 + 正确率 */}
      <div className="flex items-center gap-4">
        <AccuracyRing correct={correct} total={total} />
        <div className="min-w-0">
          <p className="text-sm text-stone-400">
            共 {total} 题，答对 {correct} 题
          </p>
          <p className="mt-1 text-base font-medium leading-relaxed">{result.summary}</p>
        </div>
      </div>

      {allCorrect && (
        <p className="rounded-2xl bg-emerald-50 px-4 py-3 text-center font-medium text-emerald-700">
          🎉 太棒了！全部答对，继续保持哦！
        </p>
      )}

      {/* 知识点掌握度 */}
      {result.knowledge_points.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-bold text-stone-500">知识点掌握度</h3>
          <div className="flex flex-wrap gap-2">
            {result.knowledge_points.map((kp) => {
              const style = MASTERY_STYLE[kp.mastery]
              return (
                <span
                  key={kp.name}
                  className={`rounded-full px-3 py-1 text-sm ${style.className}`}
                >
                  {kp.name} · {style.label}
                </span>
              )
            })}
          </div>
        </div>
      )}

      {/* 逐题批改 */}
      {total > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-bold text-stone-500">逐题详情</h3>
          <div className="space-y-3">
            {result.questions.map((q) => (
              <QuestionCard
                key={q.index}
                q={q}
                threadId={threadId}
                messageId={messageId}
                defaultKnowledgePoint={defaultKnowledgePoint}
              />
            ))}
          </div>
        </div>
      )}

      {/* 建议 */}
      {result.suggestions.length > 0 && (
        <div className="rounded-2xl bg-amber-50/80 p-4">
          <h3 className="mb-2 text-sm font-bold text-amber-700">💡 给家长的建议</h3>
          <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-stone-600">
            {result.suggestions.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      )}

      {canReport && (
        <Link
          to={`/threads/${threadId}/report/${messageId}`}
          className="block rounded-2xl border border-emerald-200 bg-emerald-50/60 px-4 py-3 text-center text-sm font-medium text-emerald-700 transition hover:bg-emerald-100"
        >
          查看完整报告 →
        </Link>
      )}
    </div>
  )
}
