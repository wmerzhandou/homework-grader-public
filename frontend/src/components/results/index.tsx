import type { ComponentType } from 'react'
import type { TaskResult } from '../../api/types'
import { GradingSummary } from '../GradingCard'
import { EnglishPassageSummary } from './EnglishPassage'

export interface ResultRenderContext {
  threadId?: string
  messageId?: string
}

interface ResultRendererProps extends ResultRenderContext {
  result: TaskResult
}

/** 各渲染器只认自己的 result schema，注册时收窄成联合类型 */
function adapt<T>(
  Component: ComponentType<ResultRenderContext & { result: T }>,
): ComponentType<ResultRendererProps> {
  return Component as unknown as ComponentType<ResultRendererProps>
}

/**
 * 按任务类型注册结果渲染器。
 * 对话流里只渲染精简摘要卡；完整内容在详情页
 * （grading → /report/ 报告页，english_passage → /detail/ 详情页）。
 */
const RENDERERS: Record<string, ComponentType<ResultRendererProps>> = {
  grading: adapt(GradingSummary),
  english_passage: adapt(EnglishPassageSummary),
}

export type ResultType = keyof typeof RENDERERS

interface ResultViewProps extends ResultRenderContext {
  /** 任务类型，默认 grading */
  type?: string
  result: TaskResult
}

export default function ResultView({ type = 'grading', result, ...ctx }: ResultViewProps) {
  const Renderer = RENDERERS[type]
  if (!Renderer) return null
  return <Renderer result={result} {...ctx} />
}
