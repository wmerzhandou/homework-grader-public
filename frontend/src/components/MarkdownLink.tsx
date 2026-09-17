import type { ReactNode } from 'react'

/**
 * Markdown 链接渲染：模型有时会把服务器上的绝对路径（/home/.../workspace/xxx）写成链接，
 * 那些链接在浏览器里必然打不开（还会顺带把你的机器路径显示出来）。
 * 这里把它们降级成"文件名"文本，真正的获取入口是消息下方的产出物卡片。
 */
export default function MarkdownLink({
  href,
  children,
}: {
  href?: string
  children?: ReactNode
}) {
  const raw = href ?? ''
  if (isServerPath(raw)) {
    const name = raw.split('/').pop() || String(children ?? '')
    return (
      <span
        className="rounded-md bg-stone-100 px-1.5 py-0.5 text-stone-500"
        title="该文件由系统生成，下方卡片可直接播放或下载"
      >
        📎 {name}
      </span>
    )
  }
  return (
    <a
      href={raw}
      target="_blank"
      rel="noreferrer noopener"
      className="text-emerald-700 underline decoration-dotted underline-offset-2"
    >
      {children}
    </a>
  )
}

function isServerPath(href: string): boolean {
  if (!href) return false
  if (href.startsWith('/home/') || href.startsWith('/Users/') || href.startsWith('/tmp/')) {
    return true
  }
  if (href.includes('/workspace/')) return true
  return /^[A-Za-z]:[\\/]/.test(href) // Windows 绝对路径
}
