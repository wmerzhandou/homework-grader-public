import { useState } from 'react'

/**
 * 会话码徽标：点一下就复制 `#CODE`，方便贴到别的会话里引用。
 * 用法：<SessionCode code={thread.code} />
 */
export default function SessionCode({
  code,
  className = '',
  title = '点一下复制，然后在别的会话里写 #码 即可引用',
}: {
  code?: string
  className?: string
  title?: string
}) {
  const [copied, setCopied] = useState(false)
  if (!code) return null

  async function copy(event: React.MouseEvent) {
    event.preventDefault()
    event.stopPropagation()
    const text = `#${code}`
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // 不支持剪贴板 API 时退化成"选中文本"
      window.prompt('复制这个会话码', text)
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <button
      type="button"
      onClick={copy}
      title={copied ? '已复制' : title}
      className={`shrink-0 rounded-full px-2 py-0.5 font-mono text-xs transition ${
        copied
          ? 'bg-emerald-100 text-emerald-700'
          : 'bg-stone-100 text-stone-500 hover:bg-emerald-50 hover:text-emerald-700'
      } ${className}`}
    >
      {copied ? '已复制' : `#${code}`}
    </button>
  )
}
