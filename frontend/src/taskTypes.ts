import type { TaskType } from './api/types'

export interface TaskTypeMeta {
  value: TaskType
  icon: string
  label: string
  hint: string
}

/** 建会话时可选的任务类型；auto 由后端自动识别 */
export const TASK_TYPES: TaskTypeMeta[] = [
  { value: 'auto', icon: '🤖', label: '自动识别', hint: '让 AI 判断任务类型' },
  { value: 'grading', icon: '📝', label: '作业批改', hint: '拍照逐题批改讲解' },
  { value: 'english_passage', icon: '🔤', label: '英语短文', hint: '逐词对照点读学单词' },
  { value: 'general', icon: '💬', label: '通用问答', hint: '自由聊天提问' },
]

export function taskTypeMeta(taskType?: string | null): TaskTypeMeta | null {
  return TASK_TYPES.find((t) => t.value === taskType) ?? null
}
