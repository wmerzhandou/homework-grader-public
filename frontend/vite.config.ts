import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// HTTPS 是必须的：浏览器的 getUserMedia（摄像头/麦克风）只在安全上下文
// （localhost 或 HTTPS）下可用，局域网/公网 IP 裸 HTTP 访问会被禁止。
const certDir = fileURLToPath(new URL('./certs', import.meta.url))

// 开发服务器（npm run dev）专用配置。
// 生产模式不需要它：后端 uvicorn 直接托管 dist/ 并监听 8040（HTTPS）。
// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    https: {
      key: readFileSync(`${certDir}/key.pem`),
      cert: readFileSync(`${certDir}/cert.pem`),
    },
    allowedHosts: true,
    proxy: {
      '/api': {
        // 生产模式后端（自带 TLS）
        target: 'https://localhost:8040',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
