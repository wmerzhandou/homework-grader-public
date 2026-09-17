import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ReactNode } from 'react'
import MarkdownLink from './MarkdownLink'

/**
 * 统一的 Markdown 渲染（富格式）。
 *
 * 为什么必须有 remark-gfm：react-markdown 默认只认 CommonMark，
 * **表格、删除线、任务列表、自动链接都不解析** —— 模型很爱写表格，
 * 少了它就会把 `| 项目 | 值 |` 原样糊在页面上（这是最影响观感的一处）。
 * 样式统一放在 index.css 的 `.markdown-body` 里，换页面不用重写。
 */
export default function Markdown({ children, className = '' }: { children: string; className?: string }) {
  return (
    <div className={`markdown-body ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: MarkdownLink,
          // 表格外面套一层滚动容器：表格本身还是真表格（不会被压成一列），
          // 窄屏放不下时只滚动这张表，不会把整页撑宽
          table: ({ children: rows }: { children?: ReactNode }) => (
            <div className="md-table-wrap">
              <table>{rows}</table>
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
