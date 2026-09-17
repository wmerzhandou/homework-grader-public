import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import type { Mistake } from '../api/types'

export default function Mistakes() {
  const [mistakes, setMistakes] = useState<Mistake[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)
  const navigate = useNavigate()

  const load = useCallback(async (knowledgePoint?: string) => {
    setLoading(true)
    setError('')
    try {
      setMistakes(await api.listMistakes(knowledgePoint || undefined))
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(filter)
  }, [load, filter])

  // 从已加载的列表聚合知识点，供筛选 chips 使用
  const [allPoints, setAllPoints] = useState<string[]>([])
  useEffect(() => {
    if (filter) return
    setAllPoints(
      Array.from(new Set(mistakes.map((m) => m.knowledge_point).filter(Boolean))),
    )
  }, [mistakes, filter])

  async function handleMastered(m: Mistake) {
    setBusyId(m.id)
    try {
      await api.setMistakeMastered(m.id, !m.mastered)
      setMistakes((prev) =>
        prev.map((x) => (x.id === m.id ? { ...x, mastered: !m.mastered } : x)),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusyId(null)
    }
  }

  async function handleDelete(m: Mistake) {
    if (!window.confirm('确定把这道题从错题本移除吗？')) return
    setBusyId(m.id)
    try {
      await api.deleteMistake(m.id)
      setMistakes((prev) => prev.filter((x) => x.id !== m.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusyId(null)
    }
  }

  async function handlePractice(m: Mistake) {
    setBusyId(m.id)
    try {
      const { thread_id } = await api.practiceMistake(m.id)
      navigate(`/threads/${thread_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : '出题失败')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-stone-200/80 bg-white/70 px-3 py-3 backdrop-blur">
        <Link
          to="/"
          className="rounded-full px-2 py-1 text-stone-400 transition hover:bg-stone-100 lg:hidden"
          aria-label="返回"
        >
          ‹ 返回
        </Link>
        <h1 className="min-w-0 flex-1 truncate font-bold">📚 错题本</h1>
      </header>

      {/* 知识点筛选 */}
      {allPoints.length > 0 && (
        <div className="flex flex-wrap gap-2 px-4 pt-3">
          <button
            onClick={() => setFilter('')}
            className={`rounded-full px-3 py-1 text-sm shadow-sm transition ${
              filter === ''
                ? 'bg-emerald-600 text-white'
                : 'bg-white text-stone-500 hover:bg-stone-100'
            }`}
          >
            全部
          </button>
          {allPoints.map((kp) => (
            <button
              key={kp}
              onClick={() => setFilter(kp)}
              className={`rounded-full px-3 py-1 text-sm shadow-sm transition ${
                filter === kp
                  ? 'bg-emerald-600 text-white'
                  : 'bg-white text-stone-500 hover:bg-stone-100'
              }`}
            >
              {kp}
            </button>
          ))}
        </div>
      )}

      <div className="mt-3 flex-1 overflow-y-auto px-3 pb-6 sm:px-4">
        {loading && <p className="py-8 text-center text-sm text-stone-400">加载中…</p>}
        {error && (
          <div className="py-8 text-center">
            <p className="text-sm text-red-500">{error}</p>
            <button
              onClick={() => load(filter)}
              className="mt-2 text-sm text-emerald-600 underline"
            >
              重试
            </button>
          </div>
        )}
        {!loading && !error && mistakes.length === 0 && (
          <p className="py-10 text-center text-sm text-stone-400">
            错题本还是空的
            <br />
            批改时点击「⭐ 加入错题本」收藏错题
          </p>
        )}

        <ul className="mx-auto max-w-3xl space-y-3">
          {mistakes.map((m) => (
            <li
              key={m.id}
              className={`rounded-3xl bg-white p-4 shadow-sm transition sm:p-5 ${
                m.mastered ? 'opacity-50 grayscale' : ''
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                {m.knowledge_point && (
                  <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs text-emerald-700">
                    {m.knowledge_point}
                  </span>
                )}
                {m.mastered && (
                  <span className="rounded-full bg-stone-100 px-2.5 py-0.5 text-xs text-stone-500">
                    ✅ 已掌握
                  </span>
                )}
                <span className="ml-auto text-xs text-stone-400">
                  {m.created_at ? new Date(m.created_at).toLocaleString() : ''}
                </span>
              </div>

              <p className="mt-2 font-medium leading-relaxed">{m.question}</p>
              {m.error_reason && (
                <p className="mt-1 text-sm text-stone-500">
                  <span className="text-stone-400">错因：</span>
                  {m.error_reason}
                </p>
              )}
              {m.source_thread_id && (
                <Link
                  to={`/threads/${m.source_thread_id}`}
                  className="mt-1 inline-block text-xs text-emerald-600 underline"
                >
                  来源会话 →
                </Link>
              )}

              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => handleMastered(m)}
                  disabled={busyId === m.id}
                  className="rounded-full bg-emerald-50 px-3 py-1 text-sm text-emerald-700 transition hover:bg-emerald-100 disabled:opacity-50"
                >
                  {m.mastered ? '↩ 撤销掌握' : '✅ 已掌握'}
                </button>
                <button
                  onClick={() => handlePractice(m)}
                  disabled={busyId === m.id}
                  className="rounded-full bg-amber-50 px-3 py-1 text-sm text-amber-700 transition hover:bg-amber-100 disabled:opacity-50"
                >
                  📝 出类似题
                </button>
                <button
                  onClick={() => handleDelete(m)}
                  disabled={busyId === m.id}
                  className="rounded-full bg-red-50 px-3 py-1 text-sm text-red-500 transition hover:bg-red-100 disabled:opacity-50"
                >
                  🗑 移除
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
