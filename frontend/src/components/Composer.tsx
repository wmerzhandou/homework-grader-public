import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Material, MaterialKind, ReferencePreviewEntry } from '../api/types'
import ReferencePicker from './ReferencePicker'

export interface PendingAttachment {
  localId: string
  file: File
  kind: MaterialKind
  purpose: string
  previewUrl: string | null
  progress: number
  status: 'pending' | 'uploading' | 'uploaded' | 'failed'
}

interface ComposerProps {
  disabled?: boolean
  /** 当前会话 id：引用选择器会排除它自己 */
  currentThreadId?: string
  /** 上传单个附件，需回报进度 */
  uploadAttachment: (
    file: File,
    purpose: string,
    onProgress: (percent: number) => void,
  ) => Promise<Material>
  /** 附件全部上传完成后调用，materials 与附件一一对应 */
  onSubmit: (text: string, materials: Material[]) => Promise<void>
}

const KIND_ICON: Record<MaterialKind, string> = {
  document: '📄',
  image: '🖼️',
  audio: '🎤',
  video: '🎬',
}

function guessKind(file: File): MaterialKind {
  if (file.type.startsWith('image/')) return 'image'
  if (file.type.startsWith('audio/')) return 'audio'
  if (file.type.startsWith('video/')) return 'video'
  return 'document'
}

function formatDuration(sec: number) {
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

type RecorderMode = 'audio' | 'video' | null

export default function Composer({
  disabled,
  currentThreadId,
  uploadAttachment,
  onSubmit,
}: ComposerProps) {
  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const [recorderMode, setRecorderMode] = useState<RecorderMode>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [preview, setPreview] = useState<{
    references: ReferencePreviewEntry[]
    unresolved: string[]
  }>({ references: [], unresolved: [] })

  const fileInputRef = useRef<HTMLInputElement>(null)
  const cameraInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const busy = disabled || sending

  /** 输入框里出现的会话码（与后端同一套规则：4 位、前面不能是字母数字） */
  const codesInText = useMemo(() => {
    const found: string[] = []
    const re = /(?<![0-9A-Za-z])[#＃]([0-9A-Za-z]{4})(?![0-9A-Za-z])/g
    for (const match of text.matchAll(re)) {
      const code = match[1].toUpperCase()
      if (!found.includes(code)) found.push(code)
    }
    return found
  }, [text])

  // 发送前预览：把 #码 解析成"将引用哪个会话、带多少材料/对话"（防抖 400ms）
  useEffect(() => {
    if (codesInText.length === 0) {
      setPreview({ references: [], unresolved: [] })
      return
    }
    let alive = true
    const timer = window.setTimeout(() => {
      api
        .referencePreview(text)
        .then((res) => alive && setPreview(res))
        .catch(() => alive && setPreview({ references: [], unresolved: codesInText }))
    }, 400)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [text, codesInText])

  function insertCode(code: string) {
    setText((prev) => {
      const trimmed = prev.replace(/\s+$/, '')
      return trimmed ? `${trimmed} #${code} ` : `#${code} `
    })
    textareaRef.current?.focus()
  }

  function removeCode(code: string) {
    setText((prev) =>
      prev
        .replace(new RegExp(`[#＃]${code}\\s?`, 'gi'), '')
        .replace(/\s{2,}/g, ' ')
        .trimStart(),
    )
  }

  // 随内容自动撑高（含软换行），上限约 6 行
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`
  }, [text])

  function addFiles(files: FileList | File[]) {
    const next = Array.from(files).map<PendingAttachment>((file) => ({
      localId: crypto.randomUUID(),
      file,
      kind: guessKind(file),
      purpose: '',
      previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : null,
      progress: 0,
      status: 'pending',
    }))
    setAttachments((prev) => [...prev, ...next])
  }

  function removeAttachment(localId: string) {
    setAttachments((prev) => {
      const target = prev.find((a) => a.localId === localId)
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl)
      return prev.filter((a) => a.localId !== localId)
    })
  }

  function setPurpose(localId: string, purpose: string) {
    setAttachments((prev) =>
      prev.map((a) => (a.localId === localId ? { ...a, purpose } : a)),
    )
  }

  function patchAttachment(localId: string, patch: Partial<PendingAttachment>) {
    setAttachments((prev) =>
      prev.map((a) => (a.localId === localId ? { ...a, ...patch } : a)),
    )
  }

  async function handleSend() {
    const trimmed = text.trim()
    if ((!trimmed && attachments.length === 0) || busy) return
    setError('')
    setSending(true)
    try {
      // 逐个上传附件（逐文件请求以保证每个附件的进度准确）
      const materials: Material[] = []
      for (const att of attachments) {
        patchAttachment(att.localId, { status: 'uploading', progress: 0 })
        try {
          const material = await uploadAttachment(att.file, att.purpose, (pct) =>
            patchAttachment(att.localId, { progress: pct }),
          )
          materials.push(material)
          patchAttachment(att.localId, { status: 'uploaded', progress: 100 })
        } catch (err) {
          patchAttachment(att.localId, { status: 'failed' })
          throw err
        }
      }
      await onSubmit(trimmed, materials)
      attachments.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl))
      setAttachments([])
      setText('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送失败，请重试')
    } finally {
      setSending(false)
    }
  }

  function handleRecorded(file: File) {
    addFiles([file])
    setRecorderMode(null)
  }

  return (
    <div className="border-t border-stone-200/80 bg-white/80 px-3 pb-[max(env(safe-area-inset-bottom),0.75rem)] pt-2 backdrop-blur sm:px-4">
      {/* 附件列表 */}
      {attachments.length > 0 && (
        <ul className="mb-2 space-y-2">
          {attachments.map((att) => (
            <li
              key={att.localId}
              className="flex items-center gap-2 rounded-2xl bg-stone-50 p-2"
            >
              {att.previewUrl ? (
                <img
                  src={att.previewUrl}
                  alt=""
                  className="h-10 w-10 shrink-0 rounded-xl object-cover"
                />
              ) : (
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-stone-200/70 text-lg">
                  {KIND_ICON[att.kind]}
                </span>
              )}
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm">{att.file.name}</p>
                {att.status === 'pending' ? (
                  <input
                    value={att.purpose}
                    onChange={(e) => setPurpose(att.localId, e.target.value)}
                    placeholder="用途说明（可选），如：数学第三页"
                    className="mt-0.5 w-full bg-transparent text-xs text-stone-500 outline-none placeholder:text-stone-300"
                  />
                ) : (
                  <div className="mt-1 flex items-center gap-2">
                    <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-stone-200">
                      <div
                        className={`h-full rounded-full transition-all ${
                          att.status === 'failed' ? 'bg-red-400' : 'bg-emerald-500'
                        }`}
                        style={{ width: `${att.progress}%` }}
                      />
                    </div>
                    <span className="shrink-0 text-xs text-stone-400">
                      {att.status === 'uploading' && `${att.progress}%`}
                      {att.status === 'uploaded' && '已上传，处理中'}
                      {att.status === 'failed' && '上传失败'}
                    </span>
                  </div>
                )}
              </div>
              {att.status === 'pending' && (
                <button
                  onClick={() => removeAttachment(att.localId)}
                  className="shrink-0 rounded-full p-1 text-stone-300 hover:text-stone-500"
                  aria-label="移除附件"
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* 录制面板 */}
      {recorderMode && (
        <RecorderPanel
          mode={recorderMode}
          onDone={handleRecorded}
          onCancel={() => setRecorderMode(null)}
        />
      )}

      {error && <p className="mb-1 text-sm text-red-500">{error}</p>}

      {/* 引用选择器（点"引用"打开） */}
      {pickerOpen && (
        <ReferencePicker
          currentThreadId={currentThreadId}
          onPick={(thread) => {
            if (thread.code) insertCode(thread.code)
            setPickerOpen(false)
          }}
          onClose={() => setPickerOpen(false)}
        />
      )}

      {/* 发送前预览：将引用哪些会话、各带多少材料与对话 */}
      {(preview.references.length > 0 || preview.unresolved.length > 0) && (
        <div className="mb-1 flex flex-wrap items-center gap-1.5">
          {preview.references.map((ref) => (
            <span
              key={ref.code}
              className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2.5 py-0.5 text-xs text-emerald-700 ring-1 ring-emerald-200"
            >
              将引用 #{ref.code}《{ref.title}》· {ref.material_count} 份材料 · {ref.message_count} 条对话
              <button
                onClick={() => removeCode(ref.code)}
                className="ml-0.5 text-emerald-500 hover:text-emerald-800"
                aria-label={`移除 ${ref.code}`}
              >
                ✕
              </button>
            </span>
          ))}
          {preview.unresolved.map((code) => (
            <span
              key={code}
              className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2.5 py-0.5 text-xs text-amber-700 ring-1 ring-amber-200"
            >
              找不到会话 #{code}
              <button
                onClick={() => removeCode(code)}
                className="ml-0.5 text-amber-500 hover:text-amber-800"
                aria-label={`移除 ${code}`}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      {/* 输入框（整行）+ 底部工具栏 */}
      <div className="flex flex-col gap-1.5">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // 仅桌面端 Enter 发送；移动端 Enter 换行
            if (
              e.key === 'Enter' &&
              !e.shiftKey &&
              !e.nativeEvent.isComposing &&
              window.matchMedia('(pointer: fine)').matches
            ) {
              e.preventDefault()
              handleSend()
            }
          }}
          rows={2}
          placeholder="说说要批改什么，如：检查数学第三页"
          disabled={busy}
          className="w-full resize-none overflow-y-auto rounded-2xl border border-stone-200 bg-stone-50 px-4 py-3 text-base leading-relaxed outline-none focus:border-emerald-400 focus:ring-2 focus:ring-emerald-100 disabled:opacity-60"
        />
        <div className="flex items-center justify-between">
          <div className="flex gap-1">
            <IconButton label="文件" icon="📎" disabled={busy} onClick={() => fileInputRef.current?.click()} />
            <IconButton label="拍照" icon="📷" disabled={busy} onClick={() => cameraInputRef.current?.click()} />
            <IconButton label="录音" icon="🎤" disabled={busy} onClick={() => setRecorderMode('audio')} />
            <IconButton label="录像" icon="🎬" disabled={busy} onClick={() => setRecorderMode('video')} />
            <IconButton
              label="引用其他会话"
              icon="🔗"
              disabled={busy}
              onClick={() => setPickerOpen((open) => !open)}
            />
          </div>
          <button
            onClick={handleSend}
            disabled={busy || (!text.trim() && attachments.length === 0)}
            className="shrink-0 rounded-2xl bg-emerald-600 px-5 py-2.5 font-medium text-white transition hover:bg-emerald-700 disabled:opacity-40"
          >
            {sending ? '上传中' : '发送'}
          </button>
        </div>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,image/*"
        onChange={(e) => {
          if (e.target.files) addFiles(e.target.files)
          e.target.value = ''
        }}
      />
      <input
        ref={cameraInputRef}
        type="file"
        hidden
        accept="image/*"
        capture="environment"
        onChange={(e) => {
          if (e.target.files) addFiles(e.target.files)
          e.target.value = ''
        }}
      />
    </div>
  )
}

function IconButton({
  label,
  icon,
  disabled,
  onClick,
}: {
  label: string
  icon: string
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className="flex h-10 w-10 items-center justify-center rounded-full text-xl transition hover:bg-stone-100 disabled:opacity-40"
    >
      {icon}
    </button>
  )
}

/** 录音/录像面板：MediaRecorder 采集，显示计时，可取消或完成 */
function RecorderPanel({
  mode,
  onDone,
  onCancel,
}: {
  mode: 'audio' | 'video'
  onDone: (file: File) => void
  onCancel: () => void
}) {
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState('')
  const [ready, setReady] = useState(false)
  const videoRef = useRef<HTMLVideoElement>(null)
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)

  useEffect(() => {
    let cancelled = false
    async function start() {
      try {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
          setError(
            '当前页面不是安全上下文（HTTPS），浏览器禁止调用摄像头/麦克风。请使用 https:// 开头的地址访问本页面。',
          )
          return
        }
        const stream = await navigator.mediaDevices.getUserMedia(
          mode === 'audio'
            ? { audio: true }
            : { audio: true, video: { facingMode: 'environment' } },
        )
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        streamRef.current = stream
        if (mode === 'video' && videoRef.current) {
          videoRef.current.srcObject = stream
        }
        const mimeType = MediaRecorder.isTypeSupported(
          mode === 'audio' ? 'audio/webm' : 'video/webm',
        )
          ? mode === 'audio'
            ? 'audio/webm'
            : 'video/webm'
          : undefined
        const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
        recorderRef.current = recorder
        recorder.ondataavailable = (e) => {
          if (e.data.size > 0) chunksRef.current.push(e.data)
        }
        recorder.start(250)
        setReady(true)
      } catch {
        setError(mode === 'audio' ? '无法访问麦克风，请检查权限' : '无法访问摄像头，请检查权限')
      }
    }
    start()
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000)
    return () => {
      cancelled = true
      clearInterval(timer)
      if (recorderRef.current?.state !== 'inactive') recorderRef.current?.stop()
      streamRef.current?.getTracks().forEach((t) => t.stop())
    }
  }, [mode])

  function finish() {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') return
    recorder.onstop = () => {
      const type = recorder.mimeType || (mode === 'audio' ? 'audio/webm' : 'video/webm')
      const ext = type.includes('mp4') ? 'mp4' : 'webm'
      const blob = new Blob(chunksRef.current, { type })
      const file = new File([blob], `${mode === 'audio' ? '录音' : '录像'}-${Date.now()}.${ext}`, {
        type,
      })
      onDone(file)
    }
    recorder.stop()
    streamRef.current?.getTracks().forEach((t) => t.stop())
  }

  function cancel() {
    chunksRef.current = []
    if (recorderRef.current?.state !== 'inactive') {
      recorderRef.current?.stop()
    }
    streamRef.current?.getTracks().forEach((t) => t.stop())
    onCancel()
  }

  return (
    <div className="mb-2 rounded-2xl bg-stone-50 p-3">
      {error ? (
        <div className="flex items-center justify-between">
          <p className="text-sm text-red-500">{error}</p>
          <button onClick={onCancel} className="text-sm text-stone-400 underline">
            关闭
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-3">
          {mode === 'video' && (
            <video
              ref={videoRef}
              autoPlay
              playsInline
              muted
              className="h-24 w-32 rounded-xl bg-stone-900 object-cover"
            />
          )}
          {mode === 'audio' && (
            <div className="flex h-8 items-center gap-1">
              {[0, 1, 2, 3, 4].map((i) => (
                <span
                  key={i}
                  className="eq-bar block h-6 w-1.5 rounded-full bg-emerald-500"
                  style={{ animationDelay: `${i * 0.15}s` }}
                />
              ))}
            </div>
          )}
          <span className="font-mono text-lg tabular-nums text-red-500">
            ● {formatDuration(seconds)}
          </span>
          <div className="ml-auto flex gap-2">
            <button
              onClick={cancel}
              className="rounded-full px-4 py-2 text-sm text-stone-500 transition hover:bg-stone-200/70"
            >
              取消
            </button>
            <button
              onClick={finish}
              disabled={!ready || seconds < 1}
              className="rounded-full bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-700 disabled:opacity-40"
            >
              完成
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
