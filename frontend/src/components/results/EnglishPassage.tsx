import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import type { EnglishPassageResult } from '../../api/types'
import { useSpeech } from '../../hooks/useSpeech'

/** ①②③… 角标，超出 20 退化为 (n) */
function circled(n: number): string {
  return n >= 1 && n <= 20 ? String.fromCodePoint(0x2460 + n - 1) : `(${n})`
}

/** 匹配填空答案时忽略大小写和标点（句中单词可能带逗号句号） */
function normalizeWord(word: string): string {
  return word.toLowerCase().replace(/[^a-z'-]/g, '')
}

interface AnswerMark {
  blank: number
}

type Sentence = EnglishPassageResult['sentences'][number]
type Answers = EnglishPassageResult['answers']

/**
 * 预先算出每个句子里哪些单词是填空答案（按出现顺序逐次消费），
 * 返回与 sentences[i].words 对齐的标记数组，避免渲染期间做匹配副作用。
 */
function markAnswers(answers: Answers, sentences: Sentence[]): (AnswerMark | undefined)[][] {
  const remaining = new Map<string, AnswerMark[]>()
  for (const a of answers) {
    const key = normalizeWord(a.word)
    const list = remaining.get(key) ?? []
    list.push({ blank: a.blank })
    remaining.set(key, list)
  }
  return sentences.map((s) =>
    s.words.map(([word]) => {
      const queue = remaining.get(normalizeWord(word))
      return queue && queue.length > 0 ? queue.shift() : undefined
    }),
  )
}

/** 通用朗读按钮：🔊 朗读 / ⏳ 合成中 / ⏹ 停止（只在详情页使用） */
function SpeakButton({
  text,
  label,
  className = '',
}: {
  text: string
  /** 非朗读态显示的文案，缺省只显示 🔊 图标 */
  label?: string
  className?: string
}) {
  const { speaking, synthesizing, speak, stop } = useSpeech()
  return (
    <button
      onClick={() => (speaking ? stop() : speak(text))}
      disabled={synthesizing}
      aria-label={speaking ? '停止朗读' : '朗读'}
      className={`rounded-full px-2.5 py-1 text-sm shadow-sm transition disabled:cursor-wait disabled:opacity-60 ${
        speaking
          ? 'bg-emerald-600 text-white'
          : 'bg-white text-emerald-700 hover:bg-emerald-50'
      } ${className}`}
    >
      {synthesizing ? '⏳ 合成中…' : speaking ? '⏹ 停止' : `🔊${label ? ` ${label}` : ''}`}
    </button>
  )
}

/** 单个单词的竖排小块：英文在上、中文在下；填空的词翠绿高亮 + 序号角标 */
function WordToken({
  word,
  meaning,
  mark,
}: {
  word: string
  meaning: string
  mark?: AnswerMark
}) {
  return (
    <span
      className={`relative inline-flex flex-col items-center rounded-lg px-1.5 pt-1 pb-0.5 ${
        mark ? 'bg-emerald-100/80' : ''
      }`}
    >
      {mark && (
        <span className="absolute -top-2 -right-1.5 text-xs font-bold text-emerald-600">
          {circled(mark.blank)}
        </span>
      )}
      <span
        className={`text-lg leading-snug ${
          mark ? 'font-bold text-emerald-700' : 'font-medium text-stone-800'
        }`}
      >
        {word}
      </span>
      <span className={`text-xs leading-snug ${mark ? 'text-emerald-600' : 'text-stone-400'}`}>
        {meaning}
      </span>
    </span>
  )
}

/** 一句的英汉逐词对照横排（自动换行） */
function SentenceInterlinear({
  sentence,
  marks,
}: {
  sentence: Sentence
  marks?: (AnswerMark | undefined)[]
}) {
  return (
    <div className="flex flex-wrap items-end gap-x-2 gap-y-3">
      {sentence.words.map(([word, meaning], wi) => (
        <WordToken key={wi} word={word} meaning={meaning} mark={marks?.[wi]} />
      ))}
    </div>
  )
}

/** 答案 chips：① B smile 微笑 */
function AnswerChips({ answers }: { answers: Answers }) {
  return (
    <div className="flex flex-wrap gap-2">
      {answers.map((a) => (
        <span
          key={a.blank}
          className="rounded-full bg-emerald-50 px-3 py-1 text-sm text-emerald-700 ring-1 ring-emerald-200"
        >
          <span className="mr-1 font-bold">{circled(a.blank)}</span>
          <span className="font-medium">{a.answer}</span>
          <span className="mx-1 font-bold">{a.word}</span>
          <span className="text-emerald-600/80">{a.meaning}</span>
        </span>
      ))}
    </div>
  )
}

/** 底部单词卡：点击朗读单词 */
function WordCard({ word, meaning }: { word: string; meaning: string }) {
  const { speaking, synthesizing, speak, stop } = useSpeech()
  return (
    <button
      onClick={() => (speaking ? stop() : speak(word))}
      disabled={synthesizing}
      className={`flex flex-col items-center gap-0.5 rounded-2xl border px-3 py-2.5 transition disabled:cursor-wait disabled:opacity-60 ${
        speaking
          ? 'border-emerald-300 bg-emerald-50'
          : 'border-stone-200 bg-stone-50/60 hover:border-emerald-200 hover:bg-emerald-50/50'
      }`}
    >
      <span className="text-base font-bold text-stone-800">
        {speaking ? '🔊 ' : ''}
        {word}
      </span>
      <span className="text-xs text-stone-400">{meaning}</span>
    </button>
  )
}

interface EnglishPassageProps {
  result: EnglishPassageResult
}

interface EnglishPassageSummaryProps extends EnglishPassageProps {
  threadId?: string
  messageId?: string
}

/** 对话流里的摘要卡：标题 + 答案 chips + 前两句对照预览（淡化截断）+ 详情链接 */
export function EnglishPassageSummary({
  result,
  threadId,
  messageId,
}: EnglishPassageSummaryProps) {
  const answers = result.answers ?? []
  const preview = useMemo(() => (result.sentences ?? []).slice(0, 2), [result.sentences])
  const marked = useMemo(
    () => markAnswers(result.answers ?? [], preview),
    [result.answers, preview],
  )
  // 流式中的临时消息没有真实 id，不能进详情页
  const canDetail = !!threadId && !!messageId && !/^(streaming|result|local)-/.test(messageId)

  return (
    <div className="space-y-3 rounded-3xl bg-white p-4 shadow-sm">
      <h3 className="text-base font-bold leading-snug">
        <span className="mr-1">📖</span>
        {result.title}
      </h3>

      {answers.length > 0 && <AnswerChips answers={answers} />}

      {preview.length > 0 && (
        <div className="relative">
          <div className="space-y-2">
            {preview.map((s, si) => (
              <div key={si} className="rounded-2xl bg-stone-50/80 p-3">
                <SentenceInterlinear sentence={s} marks={marked[si]} />
              </div>
            ))}
          </div>
          {/* 底部渐变遮罩，提示还有更多句子 */}
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-10 rounded-b-2xl bg-gradient-to-t from-white via-white/70 to-transparent" />
        </div>
      )}

      {canDetail && (
        <Link
          to={`/threads/${threadId}/detail/${messageId}`}
          className="block rounded-2xl border border-emerald-200 bg-emerald-50/60 px-4 py-2.5 text-center text-sm font-medium text-emerald-700 transition hover:bg-emerald-100"
        >
          查看全文对照 →
        </Link>
      )}
    </div>
  )
}

/** 详情页完整版：逐句对照 + 逐句点读 + 全文朗读 + 单词卡 + 温馨提示 */
export default function EnglishPassageCard({ result }: EnglishPassageProps) {
  const answers = result.answers ?? []
  const sentences = result.sentences ?? []
  const wordCards = result.word_cards ?? []
  const notes = result.notes ?? []

  const markedSentences = useMemo(
    () => markAnswers(result.answers ?? [], result.sentences ?? []),
    [result.answers, result.sentences],
  )
  const fullText = useMemo(
    () => (result.sentences ?? []).map((s) => s.text).join(' '),
    [result.sentences],
  )

  return (
    <div className="space-y-5 rounded-3xl bg-white p-4 shadow-sm sm:p-6">
      {/* 标题 + 全文朗读 */}
      <div className="flex items-start justify-between gap-3">
        <h3 className="min-w-0 text-lg font-bold leading-snug">
          <span className="mr-1">📖</span>
          {result.title}
        </h3>
        {fullText && (
          <SpeakButton text={fullText} label="全文朗读" className="shrink-0 font-medium" />
        )}
      </div>

      {/* 答案区 */}
      {answers.length > 0 && (
        <div>
          <h4 className="mb-2 text-sm font-bold text-stone-500">答案</h4>
          <AnswerChips answers={answers} />
        </div>
      )}

      {/* 英汉逐词对照 + 逐句点读 */}
      {sentences.length > 0 && (
        <div>
          <h4 className="mb-2 text-sm font-bold text-stone-500">逐句点读</h4>
          <div className="space-y-3">
            {sentences.map((s, si) => (
              <div key={si} className="rounded-2xl bg-stone-50/80 p-3.5">
                <SentenceInterlinear sentence={s} marks={markedSentences[si]} />
                <div className="mt-2 flex justify-end">
                  <SpeakButton text={s.text} />
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 单词卡 */}
      {wordCards.length > 0 && (
        <div>
          <h4 className="mb-2 text-sm font-bold text-stone-500">单词卡（点我读）</h4>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {wordCards.map((c) => (
              <WordCard key={c.word} word={c.word} meaning={c.meaning} />
            ))}
          </div>
        </div>
      )}

      {/* 温馨提示 */}
      {notes.length > 0 && (
        <div className="rounded-2xl bg-amber-50/80 p-4">
          <h4 className="mb-2 text-sm font-bold text-amber-700">💡 温馨提示</h4>
          <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-stone-600">
            {notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
