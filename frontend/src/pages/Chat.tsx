import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, uploadMaterial } from '../api/client'
import type { ChatMessage, Material, TaskType, ThreadEvent } from '../api/types'
import Composer from '../components/Composer'
import MessageList from '../components/MessageList'
import { useThreadEvents } from '../hooks/useThreadEvents'
import { taskTypeMeta } from '../taskTypes'

export default function Chat() {
  const { threadId } = useParams<{ threadId: string }>()
  const [title, setTitle] = useState('')
  const [taskType, setTaskType] = useState<TaskType | undefined>(undefined)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [streamError, setStreamError] = useState('')
  const [queued, setQueued] = useState(false)
  const [awaitingReply, setAwaitingReply] = useState(false)
  const [streamingId, setStreamingId] = useState<string | null>(null)
  const [materialStatus, setMaterialStatus] = useState<Record<string, string>>({})

  // 当前正在流式生成的消息 id；用 ref 避免在 setState updater 中做副作用
  const streamingIdRef = useRef<string | null>(null)

  const load = useCallback(async () => {
    if (!threadId) return
    setLoading(true)
    setLoadError('')
    try {
      const detail = await api.getThread(threadId)
      setTitle(detail.title)
      setTaskType(detail.task_type)
      setMessages(detail.messages)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [threadId])

  useEffect(() => {
    streamingIdRef.current = null
    setStreamingId(null)
    setMaterialStatus({})
    setStreamError('')
    load()
  }, [load])

  /** 把流式增量追加到当前 AI 消息；没有则新建一条 */
  const appendDelta = useCallback((text: string) => {
    const currentId = streamingIdRef.current
    if (currentId) {
      setMessages((prev) =>
        prev.map((m) => (m.id === currentId ? { ...m, text: m.text + text } : m)),
      )
    } else {
      const id = `streaming-${crypto.randomUUID()}`
      streamingIdRef.current = id
      setStreamingId(id)
      setMessages((prev) => [...prev, { id, role: 'assistant', text, grading_result: null }])
    }
  }, [])

  const handleEvent = useCallback(
    (event: ThreadEvent) => {
      switch (event.type) {
        case 'material_status':
          setMaterialStatus((prev) => ({
            ...prev,
            [event.material_id]: event.error ? `failed:${event.error}` : event.status,
          }))
          break
        case 'message_delta':
          setQueued(false)
          appendDelta(event.text)
          break
        case 'turn_queued':
          setQueued(true)
          break
        case 'turn_completed': {
          setQueued(false)
          setAwaitingReply(false)
          const currentId = streamingIdRef.current
          streamingIdRef.current = null
          setStreamingId(null)
          if (currentId) {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === currentId
                  ? {
                      ...m,
                      grading_result: event.grading_result ?? null,
                      result_type: event.result_type ?? m.result_type ?? null,
                    }
                  : m,
              ),
            )
          } else if (event.grading_result) {
            // 没有流式消息但带了批改结果：补一条消息展示
            setMessages((prev) => [
              ...prev,
              {
                id: `result-${crypto.randomUUID()}`,
                role: 'assistant',
                text: '',
                grading_result: event.grading_result,
                result_type: event.result_type ?? null,
              },
            ])
          }
          break
        }
        case 'error':
          setQueued(false)
          setAwaitingReply(false)
          setStreamError(event.message)
          break
      }
    },
    [appendDelta],
  )

  useThreadEvents({ threadId: threadId ?? null, onEvent: handleEvent })

  const uploadAttachment = useCallback(
    (file: File, purpose: string, onProgress: (percent: number) => void) => {
      if (!threadId) return Promise.reject(new Error('会话不存在'))
      return uploadMaterial(threadId, file, purpose, onProgress)
    },
    [threadId],
  )

  const handleSubmit = useCallback(
    async (text: string, materials: Material[]) => {
      if (!threadId) return
      setStreamError('')
      // 乐观插入用户消息
      setMessages((prev) => [
        ...prev,
        {
          id: `local-${crypto.randomUUID()}`,
          role: 'user',
          text,
          materials,
          grading_result: null,
        },
      ])
      try {
        await api.createTurn(
          threadId,
          text,
          materials.length > 0 ? materials.map((m) => m.id) : undefined,
        )
        setAwaitingReply(true)
      } catch (err) {
        setStreamError(err instanceof Error ? err.message : '发送失败，请重试')
      }
    },
    [threadId],
  )

  // SSE 在蜂窝网络下容易断流：发送后轮询兜底，服务端出现新回复就收敛到历史
  useEffect(() => {
    if (!awaitingReply || !threadId) return
    const timer = setInterval(async () => {
      try {
        const detail = await api.getThread(threadId)
        setMessages((prev) => {
          const serverCount = detail.messages.filter((m) => m.role === 'assistant').length
          const localCount = prev.filter(
            (m) => m.role === 'assistant' && !m.id.startsWith('streaming-'),
          ).length
          if (serverCount > localCount) {
            setAwaitingReply(false)
            return detail.messages
          }
          return prev
        })
      } catch {
        // 网络抖动时静默，下一轮再试
      }
    }, 5000)
    return () => clearInterval(timer)
  }, [awaitingReply, threadId])

  const processingCount = Object.values(materialStatus).filter(
    (s) => s === 'processing',
  ).length
  const typeMeta = taskType && taskType !== 'auto' ? taskTypeMeta(taskType) : null

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-stone-200/80 bg-white/70 px-3 py-3 backdrop-blur">
        <Link
          to="/"
          className="rounded-full px-2 py-1 text-stone-400 transition hover:bg-stone-100 lg:hidden"
          aria-label="返回会话列表"
        >
          ‹ 返回
        </Link>
        <h1 className="min-w-0 flex-1 truncate font-bold">{title || '批改会话'}</h1>
        {typeMeta && (
          <span className="shrink-0 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs text-emerald-700 ring-1 ring-emerald-200">
            {typeMeta.icon} {typeMeta.label}
          </span>
        )}
      </header>

      {loading ? (
        <div className="flex flex-1 items-center justify-center text-stone-400">加载中…</div>
      ) : loadError ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2">
          <p className="text-sm text-red-500">{loadError}</p>
          <button onClick={load} className="text-sm text-emerald-600 underline">
            重试
          </button>
        </div>
      ) : (
        <MessageList messages={messages} streamingId={streamingId} threadId={threadId} />
      )}

      {(processingCount > 0 || queued || (awaitingReply && !streamingId) || streamError) && (
        <div className="px-3 pb-1 sm:px-4">
          {processingCount > 0 && (
            <p className="text-xs text-stone-400">⏳ 正在解析 {processingCount} 份材料…</p>
          )}
          {queued && (
            <p className="text-xs text-stone-400">🕐 排队中：等上一个任务完成后自动继续</p>
          )}
          {awaitingReply && !streamingId && !queued && (
            <p className="text-xs text-stone-400">🤔 正在思考，网络不佳时会自动刷新结果…</p>
          )}
          {streamError && (
            <p className="rounded-xl bg-red-50 px-3 py-2 text-sm text-red-600">{streamError}</p>
          )}
        </div>
      )}

      <Composer
        disabled={loading || !!loadError}
        uploadAttachment={uploadAttachment}
        onSubmit={handleSubmit}
      />
    </div>
  )
}
