import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, clearMediaTicket, ensureMediaTicket, setToken } from '../api/client'
import type { User } from '../api/types'

interface AuthState {
  user: User | null
  /** 首次校验 token 是否还在进行中 */
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string, inviteCode: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => {
        setToken(null)
        setUser(null)
      })
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.login(username, password)
    setToken(res.token)
    setUser(res.user)
    ensureMediaTicket(true)
  }, [])

  const register = useCallback(
    async (username: string, password: string, inviteCode: string) => {
      const res = await api.register(username, password, inviteCode)
      setToken(res.token)
      setUser(res.user)
      ensureMediaTicket(true)
    },
    [],
  )

  const logout = useCallback(() => {
    // 服务端吊销 token（失败也不影响本地登出）
    api.logout().catch(() => {})
    clearMediaTicket()
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, register, logout }),
    [user, loading, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}
