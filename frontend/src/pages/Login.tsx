import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

export default function Login() {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const { login, register } = useAuth()
  const navigate = useNavigate()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      if (mode === 'login') await login(username.trim(), password)
      else await register(username.trim(), password, inviteCode.trim())
      navigate('/', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-gradient-to-b from-emerald-50/60 to-cream px-4 py-10">
      <div className="w-full max-w-sm rounded-3xl bg-white/90 p-8 shadow-lg shadow-stone-200/60">
        <div className="mb-6 text-center">
          <span className="text-4xl">🎓</span>
          <h1 className="mt-2 text-2xl font-bold">作业批改小助手</h1>
          <p className="mt-1 text-sm text-stone-500">拍一拍，AI 帮你批改孩子的作业</p>
        </div>

        <div className="mb-6 grid grid-cols-2 rounded-full bg-stone-100 p-1 text-sm font-medium">
          {(['login', 'register'] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => {
                setMode(m)
                setError('')
              }}
              className={`rounded-full py-2 transition ${
                mode === m ? 'bg-white shadow text-emerald-700' : 'text-stone-500'
              }`}
            >
              {m === 'login' ? '登录' : '注册'}
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="mb-1 block text-sm text-stone-500">用户名</span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoComplete="username"
              className="w-full rounded-xl border border-stone-200 bg-stone-50 px-4 py-3 outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100"
              placeholder="请输入用户名"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-sm text-stone-500">密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              className="w-full rounded-xl border border-stone-200 bg-stone-50 px-4 py-3 outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100"
              placeholder="请输入密码"
            />
          </label>
          {mode === 'register' && (
            <label className="block">
              <span className="mb-1 block text-sm text-stone-500">邀请码</span>
              <input
                value={inviteCode}
                onChange={(e) => setInviteCode(e.target.value)}
                required
                className="w-full rounded-xl border border-stone-200 bg-stone-50 px-4 py-3 outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100"
                placeholder="内测邀请码"
              />
            </label>
          )}

          {error && (
            <p className="rounded-xl bg-red-50 px-4 py-2 text-sm text-red-600">{error}</p>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-xl bg-emerald-600 py-3 font-medium text-white transition hover:bg-emerald-700 disabled:opacity-50"
          >
            {submitting ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}
          </button>
        </form>
      </div>
    </div>
  )
}
