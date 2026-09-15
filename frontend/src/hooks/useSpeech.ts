import { useCallback, useEffect, useRef, useState } from 'react'
import { getToken } from '../api/client'

/** 后端 IndexTTS 首次合成较慢（30 秒级），给足 90s 超时 */
const TTS_TIMEOUT_MS = 90_000

export type NarrationStatus = 'idle' | 'synthesizing' | 'speaking'

/** 全局同时只允许一个讲解在播放/合成：新的一次会先停掉上一个 */
let activeStop: (() => void) | null = null

function stopBrowserSpeech() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel()
}

/**
 * 朗读讲解：优先走后端高质量 TTS（POST /api/tts → wav 播放），
 * 失败或超时自动降级到浏览器 speechSynthesis，UI 无需报错。
 * 组件卸载时自动停止。
 */
export function useSpeech() {
  const [status, setStatus] = useState<NarrationStatus>('idle')
  const audioRef = useRef<{ audio: HTMLAudioElement; url: string } | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  /** 停止/卸载后，进行中的 fetch 回调不再改变状态 */
  const cancelledRef = useRef(false)
  /** stop 需要判断全局 activeStop 是不是自己，用 ref 避免自引用 */
  const stopSelfRef = useRef<(() => void) | null>(null)

  const stop = useCallback(() => {
    cancelledRef.current = true
    abortRef.current?.abort()
    abortRef.current = null
    if (audioRef.current) {
      audioRef.current.audio.pause()
      URL.revokeObjectURL(audioRef.current.url)
      audioRef.current = null
    }
    stopBrowserSpeech()
    if (activeStop === stopSelfRef.current) activeStop = null
    setStatus('idle')
  }, [])

  useEffect(() => {
    stopSelfRef.current = stop
  }, [stop])

  const releaseAudio = useCallback(() => {
    if (audioRef.current) {
      URL.revokeObjectURL(audioRef.current.url)
      audioRef.current = null
    }
    if (activeStop === stop) activeStop = null
    setStatus('idle')
  }, [stop])

  /** 降级：浏览器内置朗读 */
  const speakWithBrowser = useCallback(
    (text: string) => {
      if (!('speechSynthesis' in window)) {
        releaseAudio()
        return
      }
      stopBrowserSpeech()
      const utterance = new SpeechSynthesisUtterance(text)
      utterance.lang = 'zh-CN'
      utterance.rate = 0.9
      utterance.onend = () => {
        if (!cancelledRef.current) releaseAudio()
      }
      utterance.onerror = () => {
        if (!cancelledRef.current) releaseAudio()
      }
      setStatus('speaking')
      window.speechSynthesis.speak(utterance)
    },
    [releaseAudio],
  )

  const speak = useCallback(
    async (text: string) => {
      if (!text.trim()) return
      // 停掉其他题目正在播放/合成的讲解
      if (activeStop && activeStop !== stop) activeStop()
      stop()
      cancelledRef.current = false
      activeStop = stop

      const controller = new AbortController()
      abortRef.current = controller
      const timer = setTimeout(() => controller.abort(), TTS_TIMEOUT_MS)
      setStatus('synthesizing')

      try {
        const headers: Record<string, string> = { 'Content-Type': 'application/json' }
        const token = getToken()
        if (token) headers.Authorization = `Bearer ${token}`
        const res = await fetch('/api/tts', {
          method: 'POST',
          headers,
          body: JSON.stringify({ text }),
          signal: controller.signal,
        })
        if (!res.ok) throw new Error(`TTS 请求失败（${res.status}）`)
        const blob = await res.blob()
        if (cancelledRef.current) return

        const url = URL.createObjectURL(blob)
        const audio = new Audio(url)
        audioRef.current = { audio, url }
        audio.onended = releaseAudio
        audio.onerror = () => {
          // 播放失败也降级到浏览器朗读
          if (cancelledRef.current) return
          releaseAudio()
          activeStop = stop
          speakWithBrowser(text)
        }
        setStatus('speaking')
        await audio.play()
      } catch {
        // 网络错误 / 502 / 超时中断 / play() 被拒：静默降级到浏览器朗读
        if (!cancelledRef.current) speakWithBrowser(text)
      } finally {
        clearTimeout(timer)
        if (abortRef.current === controller) abortRef.current = null
      }
    },
    [releaseAudio, speakWithBrowser, stop],
  )

  useEffect(() => stop, [stop])

  return {
    status,
    speaking: status === 'speaking',
    synthesizing: status === 'synthesizing',
    speak,
    stop,
  }
}
