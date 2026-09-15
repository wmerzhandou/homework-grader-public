import type {
  AuthResponse,
  ChatMessage,
  IpBlock,
  LoginSession,
  Material,
  Mistake,
  MonitoredDevice,
  TaskType,
  Thread,
  ThreadDetail,
  User,
  Visitor,
  VisitorsResponse,
} from './types'

const TOKEN_KEY = 'hg_token'
const CLIENT_ID_KEY = 'hg_client_id'

/**
 * 本机设备标识：随机生成一次、存在本地，随请求头 X-Client-Id 上报。
 * 用途是让管理端能把"同一台设备换了 IP"和"同一 IP 下多台设备"区分开；
 * 它是随机值，不含任何指纹信息，清缓存后会重新生成（那本来就该算新设备）。
 */
export function getClientId(): string {
  let id = localStorage.getItem(CLIENT_ID_KEY) ?? ''
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
    id = (crypto.randomUUID?.() ?? Math.random().toString(36).slice(2)).replace(/-/g, '')
    localStorage.setItem(CLIENT_ID_KEY, id)
  }
  return id
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string | null) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
  clearMediaTicket()
}

// 会话过期（401）通知：token 有 7 天有效期，到期后需要自动把用户送回登录页，
// 而不是让页面继续抛一堆"请求失败"。
const unauthorizedListeners = new Set<() => void>()

export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

function notifyUnauthorized() {
  unauthorizedListeners.forEach((listener) => listener())
}

// ---------------------------------------------------------------------------
// 材料下载短时票据
// 浏览器 <img>/<audio>/<video> 带不了 Authorization 头，以前是把长期登录 token
// 拼进 URL（会漏进日志/历史）。现在改成后端签发的 15 分钟票据，只存内存。
// ---------------------------------------------------------------------------
let mediaTicket: string | null = null
let mediaTicketExpiresAt = 0
let mediaTicketInflight: Promise<void> | null = null
const mediaTicketListeners = new Set<() => void>()

function notifyMediaTicket() {
  mediaTicketListeners.forEach((listener) => listener())
}

export function subscribeMediaTicket(listener: () => void): () => void {
  mediaTicketListeners.add(listener)
  return () => {
    mediaTicketListeners.delete(listener)
  }
}

/** 取当前可用票据；临近过期（<60s）当作不可用，触发续期。 */
export function getMediaTicket(): string | null {
  if (!mediaTicket) return null
  if (Date.now() > mediaTicketExpiresAt - 60_000) return null
  return mediaTicket
}

export function clearMediaTicket() {
  mediaTicket = null
  mediaTicketExpiresAt = 0
  notifyMediaTicket()
}

export function ensureMediaTicket(force = false): Promise<void> {
  if (!force && getMediaTicket()) return Promise.resolve()
  if (mediaTicketInflight) return mediaTicketInflight
  const token = getToken()
  if (!token) return Promise.resolve()
  mediaTicketInflight = request<{ ticket: string; expires_in: number }>(
    '/api/auth/media-ticket',
    { method: 'POST' },
  )
    .then((res) => {
      mediaTicket = res.ticket
      mediaTicketExpiresAt = Date.now() + res.expires_in * 1000
      notifyMediaTicket()
    })
    .catch(() => {
      // 拿不到票据时预览会 401，下次渲染再试，不打断主流程
    })
    .finally(() => {
      mediaTicketInflight = null
    })
  return mediaTicketInflight
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  auth = true,
): Promise<T> {
  const headers = new Headers(options.headers)
  headers.set('X-Client-Id', getClientId())
  if (auth) {
    const token = getToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }
  const res = await fetch(path, { ...options, headers })
  if (!res.ok) {
    // 只对"需要鉴权的请求"上报 401：登录接口本身返回 401 属于口令错误，不算会话过期
    if (res.status === 401 && auth) notifyUnauthorized()
    let message = `请求失败（${res.status}）`
    try {
      const body = await res.json()
      if (typeof body?.detail === 'string') message = body.detail
      else if (typeof body?.message === 'string') message = body.message
    } catch {
      // 保持默认错误信息
    }
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function jsonRequest<T>(path: string, method: string, body: unknown, auth = true) {
  return request<T>(
    path,
    {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
    auth,
  )
}

export const api = {
  register(username: string, password: string, inviteCode: string) {
    return jsonRequest<AuthResponse>(
      '/api/auth/register',
      'POST',
      { username, password, invite_code: inviteCode },
      false,
    )
  },

  login(username: string, password: string) {
    return jsonRequest<AuthResponse>(
      '/api/auth/login',
      'POST',
      { username, password },
      false,
    )
  },

  me() {
    return request<User>('/api/auth/me')
  },

  logout() {
    return request<void>('/api/auth/logout', { method: 'POST' })
  },

  listThreads() {
    return request<Thread[]>('/api/threads')
  },

  createThread(title?: string, taskType?: TaskType) {
    return jsonRequest<Thread>('/api/threads', 'POST', {
      ...(title ? { title } : {}),
      ...(taskType ? { task_type: taskType } : {}),
    })
  },

  renameThread(id: string, title: string) {
    return jsonRequest<Thread>(`/api/threads/${id}`, 'PATCH', { title })
  },

  deleteThread(id: string) {
    return request<void>(`/api/threads/${id}`, { method: 'DELETE' })
  },

  async getThread(id: string): Promise<ThreadDetail> {
    const data = await request<ThreadDetail>(`/api/threads/${id}`)
    return { ...data, messages: (data.messages ?? []).map(normalizeMessage) }
  },

  createTurn(threadId: string, text: string, materialIds?: string[]) {
    return jsonRequest<{ turn_id: string }>(
      `/api/threads/${threadId}/turns`,
      'POST',
      { text, material_ids: materialIds },
    )
  },

  materialFileUrl(materialId: string) {
    const ticket = getMediaTicket()
    return `/api/materials/${materialId}/file${ticket ? `?ticket=${encodeURIComponent(ticket)}` : ''}`
  },

  addMistake(body: {
    knowledge_point: string
    question: string
    student_answer: string
    correct_answer: string
    error_reason: string | null
    explanation: string
    source_thread_id: string
    source_message_id: string
  }) {
    return jsonRequest<Mistake>('/api/mistakes', 'POST', body)
  },

  listMistakes(knowledgePoint?: string) {
    const query = knowledgePoint
      ? `?knowledge_point=${encodeURIComponent(knowledgePoint)}`
      : ''
    return request<Mistake[]>(`/api/mistakes${query}`)
  },

  setMistakeMastered(id: string, mastered: boolean) {
    return jsonRequest<Mistake>(`/api/mistakes/${id}/mastered`, 'POST', { mastered })
  },

  deleteMistake(id: string) {
    return request<void>(`/api/mistakes/${id}`, { method: 'DELETE' })
  },

  practiceMistake(id: string) {
    return jsonRequest<{ thread_id: string }>(`/api/mistakes/${id}/practice`, 'POST', {})
  },

  // ---- 管理员监控（仅 is_admin 账号显示；服务端会再校验一次）----
  adminVisitors(days: number, onlySuspicious = false) {
    return request<VisitorsResponse>(
      `/api/admin/visitors?days=${days}${onlySuspicious ? '&only_suspicious=true' : ''}`,
    )
  },

  adminBlocks() {
    return request<IpBlock[]>('/api/admin/blocks')
  },

  adminBlock(ip: string, reason: string, ttlHours?: number) {
    return jsonRequest<IpBlock>('/api/admin/blocks', 'POST', {
      ip,
      reason,
      ttl_hours: ttlHours ?? null,
    })
  },

  adminUnblock(ip: string) {
    return request<void>(`/api/admin/blocks/${encodeURIComponent(ip)}`, { method: 'DELETE' })
  },

  adminSessions() {
    return request<LoginSession[]>('/api/admin/sessions')
  },

  adminRevokeSession(id: string) {
    return request<void>(`/api/admin/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
  },

  adminRevokeAllSessions() {
    return jsonRequest<{ revoked: number }>('/api/admin/sessions/revoke-all', 'POST', {})
  },

  adminDevices() {
    return request<MonitoredDevice[]>('/api/admin/devices')
  },

  adminUpdateDevice(id: string, body: { trusted?: boolean; note?: string }) {
    return jsonRequest<MonitoredDevice>(
      `/api/admin/devices/${encodeURIComponent(id)}`,
      'POST',
      body,
    )
  },

  /** 把某个来源 IP 标记为可信（自家网络/公司出口），打分时会降权 */
  adminUpdateSource(ip: string, body: { trusted?: boolean; note?: string }) {
    return jsonRequest<Visitor>(`/api/admin/sources/${encodeURIComponent(ip)}`, 'POST', body)
  },
}

/** 后端消息字段可能叫 content 或 text，这里统一成 ChatMessage。 */
function normalizeMessage(raw: unknown): ChatMessage {
  const r = raw as Record<string, unknown>
  return {
    id: String(r.id ?? crypto.randomUUID()),
    role: r.role === 'assistant' ? 'assistant' : 'user',
    text: String(r.text ?? r.content ?? ''),
    materials: r.materials as Material[] | undefined,
    grading_result: (r.grading_result ?? null) as ChatMessage['grading_result'],
    result_type: (r.result_type ?? null) as string | null,
    created_at: r.created_at as string | undefined,
  }
}

/**
 * 上传材料（multipart）。用 XMLHttpRequest 以拿到上传进度。
 * 一个文件一次请求，进度按文件粒度回调。
 */
export function uploadMaterial(
  threadId: string,
  file: File,
  purpose: string,
  onProgress: (percent: number) => void,
): Promise<Material> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('file', file)
    form.append('purposes', JSON.stringify([purpose]))

    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/threads/${threadId}/materials`)
    xhr.setRequestHeader('X-Client-Id', getClientId())
    const token = getToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100))
    }
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const body = JSON.parse(xhr.responseText) as { materials: Material[] }
          resolve(body.materials[0])
        } catch {
          reject(new ApiError(xhr.status, '上传响应解析失败'))
        }
      } else {
        let message = `上传失败（${xhr.status}）`
        try {
          const body = JSON.parse(xhr.responseText)
          if (typeof body?.detail === 'string') message = body.detail
        } catch {
          // 保持默认错误信息
        }
        reject(new ApiError(xhr.status, message))
      }
    }
    xhr.onerror = () => reject(new ApiError(0, '网络错误，上传失败'))
    xhr.send(form)
  })
}
