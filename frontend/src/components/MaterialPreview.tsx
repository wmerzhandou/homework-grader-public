import { useEffect } from 'react'
import type { MaterialKind } from '../api/types'
import { useMediaTicket } from '../hooks/useMediaTicket'

/** 预览对象：用户上传的材料与模型产出的文件共用（区别只在 url 来源） */
export interface PreviewItem {
  filename: string
  kind: MaterialKind
  url: string
}

/** 附件预览弹窗：图片全屏查看，音频/视频在线播放，文档提供下载 */
export default function MaterialPreview({
  item,
  onClose,
}: {
  item: PreviewItem
  onClose: () => void
}) {
  // 刷新短时票据（票据到期前会自动续，父组件会用新 url 重渲染）
  useMediaTicket()
  const { filename, kind, url } = item

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-3xl bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-stone-100 px-4 py-3">
          <p className="min-w-0 truncate text-sm font-medium text-stone-600">
            {filename}
          </p>
          <div className="flex shrink-0 items-center gap-1">
            <a
              href={url}
              download={filename}
              className="rounded-full px-3 py-1 text-sm text-emerald-600 transition hover:bg-emerald-50"
            >
              ⬇ 下载
            </a>
            <button
              onClick={onClose}
              aria-label="关闭"
              className="rounded-full px-3 py-1 text-sm text-stone-400 transition hover:bg-stone-100"
            >
              ✕
            </button>
          </div>
        </div>

        <div className="flex flex-1 items-center justify-center overflow-auto bg-stone-50 p-4">
          {kind === 'image' && (
            <img src={url} alt={filename} className="max-h-[70vh] rounded-2xl object-contain" />
          )}
          {kind === 'audio' && (
            <div className="flex w-full max-w-md flex-col items-center gap-4 py-8">
              <span className="text-6xl">🎤</span>
              <audio controls autoPlay src={url} className="w-full" />
            </div>
          )}
          {kind === 'video' && (
            <video controls autoPlay src={url} className="max-h-[70vh] w-full rounded-2xl" />
          )}
          {kind === 'document' && (
            <div className="flex flex-col items-center gap-3 py-10 text-stone-500">
              <span className="text-6xl">📄</span>
              <p className="text-sm">文档暂不支持在线预览，请下载后查看</p>
              <a
                href={url}
                download={filename}
                className="rounded-2xl bg-emerald-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-emerald-700"
              >
                ⬇ 下载 {filename}
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
