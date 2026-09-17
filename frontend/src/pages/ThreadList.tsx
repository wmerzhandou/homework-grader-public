import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { TaskType, Thread } from '../api/types'
import { useAuth } from '../auth/AuthContext'
import SessionCode from '../components/SessionCode'
import { TASK_TYPES } from '../taskTypes'

export default function ThreadList() {
  const [threads, setThreads] = useState<Thread[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [taskType, setTaskType] = useState<TaskType>('auto')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editingTitle, setEditingTitle] = useState('')
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const { threadId: activeId } = useParams()

  const load = useCallback(async () => {
    setError('')
    try {
      setThreads(await api.listThreads())
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function handleCreate() {
    setCreating(true)
    try {
      const thread = await api.createThread(undefined, taskType)
      navigate(`/threads/${thread.id}`)
      // 列表稍后由 Chat 页操作后刷新
      setThreads((prev) => [thread, ...prev])
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建失败')
    } finally {
      setCreating(false)
    }
  }

  async function handleRename(id: string) {
    const title = editingTitle.trim()
    if (!title) {
      setEditingId(null)
      return
    }
    try {
      const updated = await api.renameThread(id, title)
      setThreads((prev) => prev.map((t) => (t.id === id ? { ...t, title: updated.title } : t)))
    } catch (err) {
      setError(err instanceof Error ? err.message : '重命名失败')
    } finally {
      setEditingId(null)
    }
  }

  async function handleDelete(id: string, title: string) {
    if (!window.confirm(`确定删除会话「${title}」吗？里面的消息和材料会一起删除，且无法恢复。`)) return
    try {
      await api.deleteThread(id)
      setThreads((prev) => prev.filter((t) => t.id !== id))
      if (id === activeId) navigate('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between px-4 py-4">
        <div>
          <h1 className="text-lg font-bold">作业批改</h1>
          <p className="text-xs text-stone-400">你好，{user?.username}</p>
        </div>
        <div className="flex items-center gap-1">
          <Link
            to="/mistakes"
            className="rounded-full px-3 py-1 text-sm text-emerald-600 transition hover:bg-emerald-50"
          >
            📚 错题本
          </Link>
          {user?.is_admin && (
            <Link
              to="/admin"
              className="rounded-full px-3 py-1 text-sm text-stone-500 transition hover:bg-stone-100"
              title="安全监控（仅 admin 可见）"
            >
              🛡 监控
            </Link>
          )}
          <button
            onClick={logout}
            className="rounded-full px-3 py-1 text-sm text-stone-400 transition hover:bg-stone-100 hover:text-stone-600"
          >
            退出
          </button>
        </div>
      </header>

      <div className="px-4">
        <button
          onClick={handleCreate}
          disabled={creating}
          className="w-full rounded-2xl bg-emerald-600 py-3 font-medium text-white shadow-sm transition hover:bg-emerald-700 disabled:opacity-50"
        >
          {creating ? '创建中…' : '＋ 新建会话'}
        </button>
        {/* 任务类型选择：默认自动识别 */}
        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {TASK_TYPES.map((t) => (
            <button
              key={t.value}
              onClick={() => setTaskType(t.value)}
              title={t.hint}
              aria-pressed={taskType === t.value}
              className={`rounded-xl px-2 py-1.5 text-sm transition ${
                taskType === t.value
                  ? 'bg-emerald-100 font-medium text-emerald-800 ring-1 ring-emerald-300'
                  : 'bg-white text-stone-500 ring-1 ring-stone-200 hover:bg-stone-50'
              }`}
            >
              {t.icon} {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-4 flex-1 overflow-y-auto px-3 pb-4">
        {loading && <p className="py-8 text-center text-sm text-stone-400">加载中…</p>}
        {error && (
          <div className="py-8 text-center">
            <p className="text-sm text-red-500">{error}</p>
            <button onClick={load} className="mt-2 text-sm text-emerald-600 underline">
              重试
            </button>
          </div>
        )}
        {!loading && !error && threads.length === 0 && (
          <p className="py-10 text-center text-sm text-stone-400">
            还没有批改记录
            <br />
            点击上方按钮开始第一次批改吧
          </p>
        )}
        <ul className="space-y-1">
          {threads.map((t) => (
            <li key={t.id} className="group relative">
              {editingId === t.id ? (
                <div className="flex items-center gap-1 rounded-2xl bg-white px-2 py-2 shadow-sm ring-1 ring-emerald-300">
                  <input
                    autoFocus
                    value={editingTitle}
                    onChange={(e) => setEditingTitle(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleRename(t.id)
                      if (e.key === 'Escape') setEditingId(null)
                    }}
                    className="min-w-0 flex-1 rounded-xl bg-stone-50 px-3 py-1.5 text-base outline-none"
                    maxLength={50}
                  />
                  <button
                    onClick={() => handleRename(t.id)}
                    className="shrink-0 rounded-full px-2.5 py-1 text-sm text-emerald-600"
                  >
                    ✓
                  </button>
                  <button
                    onClick={() => setEditingId(null)}
                    className="shrink-0 rounded-full px-2.5 py-1 text-sm text-stone-400"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <>
                  <Link
                    to={`/threads/${t.id}`}
                    className={`block rounded-2xl px-4 py-3 pr-16 transition ${
                      t.id === activeId
                        ? 'bg-emerald-100/70 text-emerald-900'
                        : 'hover:bg-stone-100'
                    }`}
                  >
                    <p className="truncate font-medium">{t.title || '未命名批改'}</p>
                    <div className="mt-1 flex items-center gap-2">
                      <SessionCode code={t.code} />
                      {t.updated_at && (
                        <span className="truncate text-xs text-stone-400">
                          {new Date(t.updated_at).toLocaleString()}
                        </span>
                      )}
                    </div>
                  </Link>
                  <div className="absolute right-2.5 top-1/2 flex -translate-y-1/2 gap-0.5">
                    <button
                      onClick={() => {
                        setEditingId(t.id)
                        setEditingTitle(t.title || '')
                      }}
                      aria-label="重命名"
                      className="rounded-full p-1.5 text-sm text-stone-300 transition hover:bg-stone-200/60 hover:text-stone-500"
                    >
                      ✏️
                    </button>
                    <button
                      onClick={() => handleDelete(t.id, t.title || '未命名批改')}
                      aria-label="删除"
                      className="rounded-full p-1.5 text-sm text-stone-300 transition hover:bg-red-50 hover:text-red-500"
                    >
                      🗑
                    </button>
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
