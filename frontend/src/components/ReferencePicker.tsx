import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import type { Thread } from '../api/types'

/**
 * "引用会话"选择器：列出自己的其他会话（带会话码、材料/消息数），支持按标题或码搜索，
 * 点选后由调用方把 `#CODE ` 插进输入框。
 */
export default function ReferencePicker({
  currentThreadId,
  onPick,
  onClose,
}: {
  currentThreadId?: string
  onPick: (thread: Thread) => void
  onClose: () => void
}) {
  const [threads, setThreads] = useState<Thread[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    api
      .listThreads()
      .then((list) => {
        if (alive) setThreads(list.filter((t) => t.id !== currentThreadId))
      })
      .catch((err) => alive && setError(err instanceof Error ? err.message : '加载失败'))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [currentThreadId])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (!needle) return threads
    return threads.filter(
      (t) =>
        (t.title || '').toLowerCase().includes(needle) ||
        (t.code || '').toLowerCase().includes(needle),
    )
  }, [threads, query])

  return (
    <div className="mb-2 rounded-2xl border border-stone-200 bg-white p-2 shadow-lg">
      <div className="flex items-center gap-2">
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索会话标题或会话码…"
          className="min-w-0 flex-1 rounded-xl bg-stone-50 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-emerald-100"
        />
        <button
          onClick={onClose}
          className="shrink-0 rounded-full px-3 py-1 text-sm text-stone-400 hover:bg-stone-100"
        >
          关闭
        </button>
      </div>
      {loading && <p className="px-2 py-3 text-sm text-stone-400">加载中…</p>}
      {error && <p className="px-2 py-3 text-sm text-red-500">{error}</p>}
      {!loading && !error && (
        <ul className="mt-1 max-h-64 overflow-y-auto">
          {filtered.length === 0 && (
            <li className="px-2 py-3 text-sm text-stone-400">没有匹配的会话</li>
          )}
          {filtered.map((thread) => (
            <li key={thread.id}>
              <button
                onClick={() => onPick(thread)}
                className="flex w-full items-center gap-2 rounded-xl px-2 py-2 text-left transition hover:bg-emerald-50"
              >
                <span className="shrink-0 rounded-full bg-stone-100 px-2 py-0.5 font-mono text-xs text-stone-500">
                  #{thread.code}
                </span>
                <span className="min-w-0 flex-1 truncate text-sm">{thread.title || '未命名会话'}</span>
                <span className="shrink-0 text-xs text-stone-400">
                  {thread.material_count ?? 0} 材料 · {thread.message_count ?? 0} 条
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-1 px-2 text-xs text-stone-400">
        选中后会把 <span className="font-mono">#码</span> 插进输入框；发送时后端只注入该会话的材料索引与对话索引，内容由模型按需读取。
      </p>
    </div>
  )
}
