import { useCallback, useEffect, useState } from 'react'
import { Link, Navigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { Artifact, ChatMessage, EnglishPassageResult, Material } from '../api/types'
import EnglishPassageCard from '../components/results/EnglishPassage'
import Markdown from '../components/Markdown'
import { splitReply } from '../lib/reply'
import { useMediaTicket } from '../hooks/useMediaTicket'

const KIND_ICON: Record<string, string> = {
  document: '📄',
  image: '🖼️',
  audio: '🎤',
  video: '🎬',
}

/**
 * 消息详情页：对话里只显示归纳，这里给"全且丰富"——
 * 归纳 + 完整详情 + 产出物（音频/图片可直接播放查看）+ 提交的材料 + 过程记录 + 复制全文。
 */
export default function MessageDetail() {
  const { threadId, messageId } = useParams<{ threadId: string; messageId: string }>()
  const [title, setTitle] = useState('')
  const [message, setMessage] = useState<ChatMessage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  // 产出物用短时票据取，票据到位后重渲染
  useMediaTicket()

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
  const { summary, detail } = splitReply(message.text)
  const artifacts = message.artifacts ?? []
  const materials = message.materials ?? []

  async function copyAll() {
    try {
      await navigator.clipboard.writeText(message?.text ?? '')
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('复制失败，请手动选择文本')
    }
  }

  return (
    <div className="h-full overflow-y-auto px-3 py-4 sm:px-6">
      <div className="mx-auto max-w-3xl space-y-5 pb-10">
        {/* 顶部导航（打印时隐藏） */}
        <div className="flex items-center justify-between print:hidden">
          <Link
            to={`/threads/${threadId}`}
            className="rounded-full bg-white px-4 py-1.5 text-sm text-stone-500 shadow-sm transition hover:bg-stone-100"
          >
            ‹ 返回会话
          </Link>
          <div className="flex items-center gap-2">
            <button
              onClick={copyAll}
              className="rounded-full bg-white px-4 py-1.5 text-sm text-stone-500 shadow-sm transition hover:bg-stone-100"
            >
              {copied ? '✓ 已复制' : '⧉ 复制全文'}
            </button>
            {!isEnglishPassage && (
              <button
                onClick={() => window.print()}
                className="rounded-full bg-emerald-600 px-4 py-1.5 text-sm font-medium text-white shadow-sm transition hover:bg-emerald-700"
              >
                🖨 打印
              </button>
            )}
          </div>
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

        {/* 归纳 */}
        {summary && (
          <section className="rounded-3xl border-l-4 border-emerald-400 bg-emerald-50/60 p-5 print:shadow-none">
            <p className="mb-1 text-xs font-medium text-emerald-700">归纳</p>
            <Markdown className="text-base leading-relaxed">{summary}</Markdown>
          </section>
        )}

        {/* 产出物 */}
        {artifacts.length > 0 && (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
            <h2 className="mb-3 font-bold text-stone-700">生成的文件（{artifacts.length}）</h2>
            <div className="space-y-3">
              {artifacts.map((a) => (
                <ArtifactBlock key={a.id} threadId={threadId ?? ''} artifact={a} />
              ))}
            </div>
          </section>
        )}

        {/* 提交的材料 */}
        {materials.length > 0 && (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:shadow-none">
            <h2 className="mb-3 font-bold text-stone-700">提交的材料（{materials.length}）</h2>
            <div className="flex flex-wrap gap-2">
              {materials.map((m) => (
                <MaterialChip key={m.id} material={m} />
              ))}
            </div>
          </section>
        )}

        {/* 完整详情 */}
        {isEnglishPassage ? (
          <EnglishPassageCard result={message.grading_result as EnglishPassageResult} />
        ) : (
          <section className="rounded-3xl bg-white p-5 shadow-sm sm:p-8 print:shadow-none">
            <h2 className="mb-3 font-bold text-stone-700">完整内容</h2>
            <Markdown className="text-base leading-relaxed sm:text-lg sm:leading-loose">
              {detail || message.text}
            </Markdown>
          </section>
        )}

        {/* 过程记录（默认折叠，不打扰阅读） */}
        {message.process_log && (
          <details className="rounded-3xl bg-white p-5 shadow-sm sm:p-6 print:hidden">
            <summary className="cursor-pointer text-sm font-medium text-stone-500">
              🛠 过程记录（{message.process_log.length} 字）
            </summary>
            <pre className="mt-3 whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-stone-500">
              {message.process_log}
            </pre>
          </details>
        )}
      </div>
    </div>
  )
}

function ArtifactBlock({ threadId, artifact }: { threadId: string; artifact: Artifact }) {
  const url = api.artifactFileUrl(threadId, artifact.id)
  const previewUrl = api.artifactPreviewUrl(threadId, artifact.id)
  const sizeKb = Math.max(1, Math.round(artifact.size / 1024))
  return (
    <div className="rounded-2xl bg-stone-50 p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="truncate text-sm text-stone-600">
          {KIND_ICON[artifact.kind] ?? '📎'} {artifact.filename}
          <span className="ml-2 text-xs text-stone-400">{sizeKb} KB</span>
          {artifact.oversized && (
            <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-700">
              文件较大，仅提供下载
            </span>
          )}
          {artifact.kind === 'video' && !artifact.has_preview && !artifact.oversized && (
            <span className="ml-2 rounded-full bg-stone-200 px-2 py-0.5 text-xs text-stone-500">
              正在准备可播放版本…
            </span>
          )}
        </span>
        <a
          href={url}
          download={artifact.filename}
          className="rounded-full bg-white px-3 py-1 text-xs text-emerald-700 shadow-sm transition hover:bg-emerald-50"
        >
          ⬇ 下载
        </a>
      </div>
      {!artifact.oversized && artifact.kind === 'audio' && (
        <audio controls preload="none" src={url} className="w-full" />
      )}
      {!artifact.oversized && artifact.kind === 'video' && (
        <video controls preload="none" src={previewUrl} className="max-h-96 w-full rounded-xl" />
      )}
      {!artifact.oversized && artifact.kind === 'image' && (
        <img
          src={previewUrl}
          alt={artifact.filename}
          className="max-h-96 rounded-xl object-contain"
        />
      )}
    </div>
  )
}

function MaterialChip({ material }: { material: Material }) {
  const url = api.materialFileUrl(material.id)
  if (material.kind === 'image') {
    return (
      <a href={url} target="_blank" rel="noreferrer noopener" className="block">
        <img
          src={url}
          alt={material.filename}
          className="h-24 w-24 rounded-xl border border-stone-200 object-cover transition hover:opacity-80"
        />
      </a>
    )
  }
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      className="rounded-full bg-stone-100 px-3 py-1 text-sm text-stone-500 transition hover:bg-stone-200"
    >
      {KIND_ICON[material.kind] ?? '📄'} {material.filename}
    </a>
  )
}
