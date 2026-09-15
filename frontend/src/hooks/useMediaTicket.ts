import { useEffect, useState } from 'react'
import { ensureMediaTicket, getMediaTicket, subscribeMediaTicket } from '../api/client'

/**
 * 材料预览用的短时票据。
 *
 * 挂载时拉取、每分钟检查续期；票据变化时触发重渲染，
 * 让 `<img src={api.materialFileUrl(id)}>` 拿到新的 URL。
 */
export function useMediaTicket(): string | null {
  const [, bump] = useState(0)

  useEffect(
    () => subscribeMediaTicket(() => bump((n) => n + 1)),
    [],
  )

  useEffect(() => {
    ensureMediaTicket()
    const timer = setInterval(() => ensureMediaTicket(), 60_000)
    return () => clearInterval(timer)
  }, [])

  return getMediaTicket()
}
