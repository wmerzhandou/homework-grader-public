import { BrowserRouter, Navigate, Outlet, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthContext'
import Login from './pages/Login'
import ThreadList from './pages/ThreadList'
import Chat from './pages/Chat'
import Report from './pages/Report'
import MessageDetail from './pages/MessageDetail'
import Mistakes from './pages/Mistakes'

function RequireAuth() {
  const { user, loading } = useAuth()
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-stone-400">
        加载中…
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  return <Outlet />
}

/** 桌面端：左侧会话列表 + 右侧内容；手机端：列表与聊天分屏切换 */
function Layout() {
  return (
    <div className="mx-auto flex h-full max-w-7xl">
      <aside className="hidden w-80 shrink-0 border-r border-stone-200/80 lg:flex lg:flex-col">
        <ThreadList />
      </aside>
      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  )
}

/** 首页：手机上看会话列表，桌面端看占位页 */
function Home() {
  return (
    <>
      <div className="h-full lg:hidden">
        <ThreadList />
      </div>
      <div className="hidden h-full flex-col items-center justify-center gap-3 text-stone-400 lg:flex">
        <span className="text-5xl">📝</span>
        <p>选择左侧会话，或新建一次批改</p>
      </div>
    </>
  )
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route element={<RequireAuth />}>
            {/* 报告页/详情页独立全屏，便于打印和阅读 */}
            <Route path="/threads/:threadId/report/:messageId" element={<Report />} />
            <Route path="/threads/:threadId/detail/:messageId" element={<MessageDetail />} />
            <Route element={<Layout />}>
              <Route index element={<Home />} />
              <Route path="/threads/:threadId" element={<Chat />} />
              <Route path="/mistakes" element={<Mistakes />} />
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}
