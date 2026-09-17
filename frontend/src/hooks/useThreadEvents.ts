import { useEffect, useRef, useState } from 'react'
import { getClientId, getToken } from '../api/client'
import type { ThreadEvent } from '../api/types'

interface UseThreadEventsOptions {
  /** 为 null 时不连接 */
  threadId: string | null
  onEvent: (event: ThreadEvent) => void
  enabled?: boolean
}

/**
 * 订阅某个会话的 SSE 事件流。
 * EventSource 无法带 Authorization 头，所以用 fetch + ReadableStream 手动解析
 * `data: {...}\n\n` 格式。断线后自动重连（简单退避）。
 */
export function useThreadEvents({ threadId, onEvent, enabled = true }: UseThreadEventsOptions) {
  const [connected, setConnected] = useState(false)
  const onEventRef = useRef(onEvent)
  useEffect(() => {
    onEventRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    if (!threadId || !enabled) return

    let aborted = false
    let abortController = new AbortController()
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let retryDelay = 1000

    async function connect() {
      if (aborted || !threadId) return
      abortController = new AbortController()
      try {
        const headers: Record<string, string> = {
          Accept: 'text/event-stream',
          'X-Client-Id': getClientId(),
        }
        const token = getToken()
        if (token) headers.Authorization = `Bearer ${token}`

        const res = await fetch(`/api/threads/${threadId}/events`, {
          headers,
          signal: abortController.signal,
        })
        if (!res.ok || !res.body) throw new Error(`SSE 连接失败（${res.status}）`)

        setConnected(true)
        retryDelay = 1000
        await readSSEStream(res.body, (eventName, data) => {
          try {
            const payload = JSON.parse(data) as Record<string, unknown>
            // 类型取自 SSE 的 `event:` 行（负载里自带 type 时以负载为准）
            const event = { ...payload, type: payload.type ?? eventName } as ThreadEvent
            onEventRef.current(event)
          } catch {
            // 忽略无法解析的事件
          }
        })
      } catch {
        // 连接失败或被中断，走重连
      } finally {
        setConnected(false)
      }
      if (!aborted) {
        retryTimer = setTimeout(connect, retryDelay)
        retryDelay = Math.min(retryDelay * 2, 15000)
      }
    }

    connect()

    return () => {
      aborted = true
      if (retryTimer) clearTimeout(retryTimer)
      abortController.abort()
    }
  }, [threadId, enabled])

  return { connected }
}

/** 逐行解析 SSE 流，把每个 `data:` 负载交给回调。 */
export async function readSSEStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (eventName: string, data: string) => void,
) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // SSE 事件以空行（\n\n）分隔
    let sepIndex = buffer.indexOf('\n\n')
    while (sepIndex !== -1) {
      const rawEvent = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)
      const lines = rawEvent.split('\n')
      // 事件类型只写在 `event:` 这行上；丢掉它，前端就拿不到任何事件类型
      const eventName =
        lines
          .find((line) => line.startsWith('event:'))
          ?.slice(6)
          .trim() ?? ''
      const dataLines = lines
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).replace(/^ /, ''))
      if (dataLines.length > 0) onEvent(eventName, dataLines.join('\n'))
      sepIndex = buffer.indexOf('\n\n')
    }
  }
}
