export interface User {
  id: string
  username: string
  /** 仅 admin 账号为 true；决定是否显示安全监控入口 */
  is_admin?: boolean
}

export interface Visitor {
  ip: string
  requests: number
  first_seen: string
  last_seen: string
  authenticated: boolean
  usernames: string[]
  user_agents: string[]
  top_paths: string[]
  error_count: number
  blocked: boolean
  block_reason?: string | null
  block_expires_at?: string | null
  /** 归属地与网络类型（离线库推断） */
  location: string
  org: string
  asn?: number | null
  network_type: string
  /** 设备画像（UA 解析） */
  device_label: string
  device_ids: string[]
  trusted_device: boolean
  known_device: boolean
  is_bot: boolean
  /** 数据访问审计 */
  threads_viewed: string[]
  materials_downloaded: string[]
  uploads: number
  turns: number
  logins_ok: number
  logins_failed: number
  sensitive_probes: string[]
  /** 可疑度 */
  risk_score: number
  risk_level: '低' | '中' | '高'
  risk_flags: string[]
  source_trusted: boolean
}

export interface MonitoredDevice {
  id: string
  label: string
  user_agent: string
  first_seen: string
  last_seen: string
  last_ip: string
  trusted: boolean
  note: string
  request_count: number
  ip_count: number
}

export interface VisitorsResponse {
  window_days: number
  generated_at: string
  total_records: number
  visitors: Visitor[]
}

export interface IpBlock {
  ip: string
  reason: string
  created_by: string
  created_at: string
  expires_at?: string | null
}

export interface LoginSession {
  id: string
  username: string
  user_id: string
  created_at: string
  expires_at?: string | null
  current: boolean
}

export interface AuthResponse {
  token: string
  user: User
}

export type MaterialKind = 'document' | 'image' | 'audio' | 'video'
export type MaterialStatus = 'processing' | 'ready' | 'failed'

export interface Material {
  id: string
  filename: string
  kind: MaterialKind
  status: MaterialStatus
}

export type TaskType = 'auto' | 'grading' | 'english_passage' | 'general'

export interface Thread {
  id: string
  title: string
  task_type?: TaskType
  created_at?: string
  updated_at?: string
}

export type MessageRole = 'user' | 'assistant'

export interface ChatMessage {
  id: string
  role: MessageRole
  text: string
  materials?: Material[]
  /** 结构化结果 JSON，内容按 result_type 对应 schema（字段名历史遗留叫 grading_result） */
  grading_result?: TaskResult | null
  /** 结果类型："grading" | "english_passage"，缺省按 grading 处理 */
  result_type?: string | null
  created_at?: string
}

export interface ThreadDetail extends Thread {
  messages: ChatMessage[]
}

export interface KnowledgePoint {
  name: string
  mastery: 'good' | 'weak' | 'poor'
}

export interface GradedQuestion {
  index: number
  question: string
  student_answer: string
  correct: boolean
  correct_answer: string
  error_reason: string | null
  explanation: string
}

export interface GradingResult {
  summary: string
  knowledge_points: KnowledgePoint[]
  questions: GradedQuestion[]
  suggestions: string[]
}

/** 英语短文学习任务结果：逐词对照 + 填空答案 + 单词卡 */
export interface EnglishPassageResult {
  title: string
  answers: { blank: number; answer: string; word: string; meaning: string }[]
  sentences: { text: string; words: [string, string][] }[]
  word_cards: { word: string; meaning: string }[]
  notes: string[]
}

/** 消息结构化结果的联合类型，按 ChatMessage.result_type 区分 */
export type TaskResult = GradingResult | EnglishPassageResult

export interface Mistake {
  id: string
  knowledge_point: string
  question: string
  student_answer: string
  correct_answer: string
  error_reason: string | null
  explanation: string
  source_thread_id: string
  source_message_id: string
  mastered: boolean
  created_at: string
}

export type ThreadEvent =
  | { type: 'material_status'; material_id: string; status: MaterialStatus; error?: string }
  | { type: 'message_delta'; text: string }
  | { type: 'turn_queued' }
  | { type: 'turn_completed'; grading_result?: TaskResult; result_type?: string | null }
  | { type: 'error'; message: string }
