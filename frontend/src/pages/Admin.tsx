import { useCallback, useEffect, useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { api } from '../api/client'
import type { IpBlock, LoginSession, MonitoredDevice, Visitor } from '../api/types'
import { useAuth } from '../auth/AuthContext'

type Tab = 'visitors' | 'devices' | 'sessions'

const RANGES = [
  { days: 1, label: '24 小时' },
  { days: 7, label: '7 天' },
  { days: 30, label: '30 天' },
]

const RISK_STYLE: Record<string, string> = {
  高: 'bg-red-100 text-red-700',
  中: 'bg-amber-100 text-amber-700',
  低: 'bg-stone-100 text-stone-500',
}

const NETWORK_STYLE: Record<string, string> = {
  '云主机/VPN': 'bg-red-100 text-red-700',
  移动网络: 'bg-sky-100 text-sky-700',
  固网宽带: 'bg-emerald-100 text-emerald-700',
  国外网络: 'bg-amber-100 text-amber-700',
  内网: 'bg-stone-100 text-stone-500',
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('zh-CN', { hour12: false })
}

/** 安全监控页：仅 admin 可见（后端 /api/admin/* 也会独立校验一次） */
export default function Admin() {
  const { user, loading: authLoading } = useAuth()
  const [tab, setTab] = useState<Tab>('visitors')
  const [days, setDays] = useState(7)
  const [onlySuspicious, setOnlySuspicious] = useState(false)
  const [visitors, setVisitors] = useState<Visitor[]>([])
  const [blocks, setBlocks] = useState<IpBlock[]>([])
  const [sessions, setSessions] = useState<LoginSession[]>([])
  const [devices, setDevices] = useState<MonitoredDevice[]>([])
  const [expanded, setExpanded] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [v, b, s, d] = await Promise.all([
        api.adminVisitors(days, onlySuspicious),
        api.adminBlocks(),
        api.adminSessions(),
        api.adminDevices(),
      ])
      setVisitors(v.visitors)
      setBlocks(b)
      setSessions(s)
      setDevices(d)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [days, onlySuspicious])

  useEffect(() => {
    if (user?.is_admin) load()
  }, [user?.is_admin, load])

  if (authLoading) {
    return <div className="flex h-full items-center justify-center text-stone-400">加载中…</div>
  }
  if (!user?.is_admin) return <Navigate to="/" replace />

  const blockedIps = new Set(blocks.map((b) => b.ip))

  async function run(
    action: () => Promise<unknown>,
    okMessage: string,
    failMessage: string,
  ): Promise<void> {
    try {
      await action()
      if (okMessage) setNotice(okMessage)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : failMessage)
    }
  }

  function handleBlock(ip: string) {
    const reason = window.prompt(`封禁 ${ip} 的理由（可留空）`, '')
    if (reason === null) return
    void run(() => api.adminBlock(ip, reason), `已封禁 ${ip}：之后的请求会直接返回 403`, '封禁失败')
  }

  function handleRevoke(sessionId: string) {
    if (!window.confirm('吊销这个会话？该设备会立刻需要重新登录。')) return
    void run(() => api.adminRevokeSession(sessionId), '已吊销该会话', '吊销失败')
  }

  function handleRevokeAll() {
    if (!window.confirm('吊销除当前设备之外的所有登录会话？其他设备都要重新登录。')) return
    void (async () => {
      try {
        const res = await api.adminRevokeAllSessions()
        setNotice(`已吊销 ${res.revoked} 个会话（当前设备保留）`)
        await load()
      } catch (err) {
        setError(err instanceof Error ? err.message : '操作失败')
      }
    })()
  }

  function toggleTrust(deviceId: string, trusted: boolean) {
    void run(
      () => api.adminUpdateDevice(deviceId, { trusted }),
      trusted ? '已标记为可信设备（可疑度相应降低）' : '已取消可信标记',
      '操作失败',
    )
  }

  function toggleSourceTrust(ip: string, trusted: boolean) {
    void run(
      () => api.adminUpdateSource(ip, { trusted }),
      trusted ? `已把 ${ip} 标记为可信来源（自家网络）` : `已取消 ${ip} 的可信标记`,
      '操作失败',
    )
  }

  const suspiciousCount = visitors.filter((v) => v.risk_score >= 30).length

  return (
    <div className="h-full overflow-y-auto px-3 py-4 sm:px-6">
      <div className="mx-auto max-w-6xl space-y-4 pb-10">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-xl font-bold">🛡 安全监控</h1>
            <p className="mt-1 text-xs text-stone-400">
              仅 admin 可见 · 归属地来自离线 IP 库（不外发）· 访问记录保留 90 天
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              className="rounded-full bg-white px-4 py-1.5 text-sm text-stone-500 shadow-sm transition hover:bg-stone-100"
            >
              ↻ 刷新
            </button>
            <Link
              to="/"
              className="rounded-full bg-white px-4 py-1.5 text-sm text-stone-500 shadow-sm transition hover:bg-stone-100"
            >
              ‹ 返回会话
            </Link>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          {(
            [
              ['visitors', `访问来源 (${visitors.length})`],
              ['devices', `设备 (${devices.length})`],
              ['sessions', `登录会话 (${sessions.length})`],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`rounded-full px-4 py-1.5 text-sm transition ${
                tab === key
                  ? 'bg-emerald-600 font-medium text-white'
                  : 'bg-white text-stone-500 ring-1 ring-stone-200 hover:bg-stone-50'
              }`}
            >
              {label}
            </button>
          ))}
          {tab === 'visitors' && suspiciousCount > 0 && (
            <span className="self-center rounded-full bg-red-50 px-3 py-1 text-xs text-red-600">
              其中 {suspiciousCount} 个来源可疑度 ≥ 30
            </span>
          )}
        </div>

        {error && <p className="rounded-2xl bg-red-50 px-4 py-2 text-sm text-red-600">{error}</p>}
        {notice && (
          <p className="rounded-2xl bg-emerald-50 px-4 py-2 text-sm text-emerald-700">{notice}</p>
        )}

        {tab === 'visitors' && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              {RANGES.map((r) => (
                <button
                  key={r.days}
                  onClick={() => setDays(r.days)}
                  className={`rounded-full px-3 py-1 text-xs transition ${
                    days === r.days
                      ? 'bg-stone-800 font-medium text-white'
                      : 'bg-white text-stone-500 ring-1 ring-stone-200 hover:bg-stone-50'
                  }`}
                >
                  最近 {r.label}
                </button>
              ))}
              <label className="ml-2 flex items-center gap-1 text-xs text-stone-500">
                <input
                  type="checkbox"
                  checked={onlySuspicious}
                  onChange={(e) => setOnlySuspicious(e.target.checked)}
                />
                只看可疑来源
              </label>
            </div>

            <div className="overflow-hidden rounded-3xl bg-white shadow-sm">
              <table className="w-full text-left text-sm">
                <thead className="bg-stone-50 text-xs uppercase text-stone-400">
                  <tr>
                    <th className="px-4 py-3">IP / 归属</th>
                    <th className="px-2 py-3">可疑度</th>
                    <th className="px-2 py-3">身份</th>
                    <th className="px-2 py-3">设备</th>
                    <th className="px-2 py-3">请求</th>
                    <th className="px-2 py-3">最近访问</th>
                    <th className="px-4 py-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {loading && (
                    <tr>
                      <td colSpan={7} className="px-4 py-6 text-center text-stone-400">
                        加载中…
                      </td>
                    </tr>
                  )}
                  {!loading && visitors.length === 0 && (
                    <tr>
                      <td colSpan={7} className="px-4 py-6 text-center text-stone-400">
                        这段时间还没有记录
                      </td>
                    </tr>
                  )}
                  {visitors.map((v) => (
                    <tr key={v.ip} className="border-t border-stone-100 align-top">
                      <td className="px-4 py-3">
                        <button
                          onClick={() => setExpanded(expanded === v.ip ? null : v.ip)}
                          className="font-mono text-xs text-stone-700 underline decoration-dotted"
                        >
                          {v.ip}
                        </button>
                        {v.blocked && (
                          <span className="ml-2 rounded-full bg-red-100 px-2 py-0.5 text-xs text-red-600">
                            已封禁
                          </span>
                        )}
                        <div className="mt-1 text-xs text-stone-500">
                          {v.location || '未知'}
                          {v.org && <span className="text-stone-400"> · {v.org}</span>}
                        </div>
                        <span
                          className={`mt-1 inline-block rounded-full px-2 py-0.5 text-xs ${
                            NETWORK_STYLE[v.network_type] ?? 'bg-stone-100 text-stone-500'
                          }`}
                        >
                          {v.network_type || '未知网络'}
                        </span>
                        {expanded === v.ip && <VisitorDetail visitor={v} onToggleTrust={toggleTrust} />}
                      </td>
                      <td className="px-2 py-3">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                            RISK_STYLE[v.risk_level] ?? RISK_STYLE['低']
                          }`}
                        >
                          {v.risk_level} {v.risk_score}
                        </span>
                      </td>
                      <td className="px-2 py-3 text-xs">
                        {v.authenticated ? (
                          <span className="text-emerald-700">已登录 {v.usernames.join('、')}</span>
                        ) : (
                          <span className="text-stone-400">未登录</span>
                        )}
                        {v.logins_failed > 0 && (
                          <div className="text-xs text-amber-600">失败 {v.logins_failed}</div>
                        )}
                      </td>
                      <td className="px-2 py-3 text-xs text-stone-500">
                        {v.device_label || '未知'}
                        {v.trusted_device && (
                          <span className="ml-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-700">
                            可信
                          </span>
                        )}
                        {v.device_ids.length > 1 && (
                          <div className="text-xs text-stone-400">{v.device_ids.length} 台设备</div>
                        )}
                      </td>
                      <td className="px-2 py-3 text-xs text-stone-500">
                        {v.requests}
                        {v.error_count > 0 && (
                          <span className="text-amber-600"> / {v.error_count} 错</span>
                        )}
                        {v.materials_downloaded.length > 0 && (
                          <div className="text-xs text-red-600">
                            ↓ 下载 {v.materials_downloaded.length}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-3 text-xs text-stone-500">{formatTime(v.last_seen)}</td>
                      <td className="px-4 py-3 text-right">
                        {blockedIps.has(v.ip) ? (
                          <button
                            onClick={() =>
                              run(() => api.adminUnblock(v.ip), `已解封 ${v.ip}`, '解封失败')
                            }
                            className="rounded-full bg-stone-100 px-3 py-1 text-xs text-stone-600 transition hover:bg-stone-200"
                          >
                            解封
                          </button>
                        ) : (
                          <button
                            onClick={() => handleBlock(v.ip)}
                            className="rounded-full bg-red-50 px-3 py-1 text-xs text-red-600 transition hover:bg-red-100"
                          >
                            封禁
                          </button>
                        )}
                        <button
                          onClick={() => toggleSourceTrust(v.ip, !v.source_trusted)}
                          className={`ml-1 rounded-full px-3 py-1 text-xs transition ${
                            v.source_trusted
                              ? 'bg-emerald-100 text-emerald-700 hover:bg-emerald-200'
                              : 'bg-stone-100 text-stone-500 hover:bg-stone-200'
                          }`}
                          title="标记为自家网络后，该来源的可疑度会降低"
                        >
                          {v.source_trusted ? '可信 ✓' : '标可信'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs leading-relaxed text-stone-400">
              点 IP 展开可看它看过哪些会话、下载过哪些材料。封禁按 IP 生效；手机流量的 IP 会变、
              也可能多人共用，要精确踢设备请用「登录会话」。
            </p>
          </>
        )}

        {tab === 'devices' && (
          <>
            <div className="overflow-hidden rounded-3xl bg-white shadow-sm">
              <table className="w-full text-left text-sm">
                <thead className="bg-stone-50 text-xs uppercase text-stone-400">
                  <tr>
                    <th className="px-4 py-3">设备</th>
                    <th className="px-2 py-3">最近 IP</th>
                    <th className="px-2 py-3">IP 数</th>
                    <th className="px-2 py-3">请求</th>
                    <th className="px-2 py-3">最近活跃</th>
                    <th className="px-4 py-3 text-right">可信</th>
                  </tr>
                </thead>
                <tbody>
                  {devices.map((d) => (
                    <tr key={d.id} className="border-t border-stone-100">
                      <td className="px-4 py-3">
                        <div className="text-xs text-stone-700">{d.label || '未知设备'}</div>
                        <div className="font-mono text-[11px] text-stone-400">
                          {d.id.slice(0, 12)}
                        </div>
                        {d.note && <div className="text-xs text-emerald-700">备注：{d.note}</div>}
                      </td>
                      <td className="px-2 py-3 font-mono text-xs text-stone-500">{d.last_ip}</td>
                      <td className="px-2 py-3 text-xs text-stone-500">{d.ip_count}</td>
                      <td className="px-2 py-3 text-xs text-stone-500">{d.request_count}</td>
                      <td className="px-2 py-3 text-xs text-stone-500">{formatTime(d.last_seen)}</td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => toggleTrust(d.id, !d.trusted)}
                          className={`rounded-full px-3 py-1 text-xs transition ${
                            d.trusted
                              ? 'bg-emerald-100 text-emerald-700 hover:bg-emerald-200'
                              : 'bg-stone-100 text-stone-500 hover:bg-stone-200'
                          }`}
                        >
                          {d.trusted ? '可信设备 ✓' : '标记可信'}
                        </button>
                      </td>
                    </tr>
                  ))}
                  {!loading && devices.length === 0 && (
                    <tr>
                      <td colSpan={6} className="px-4 py-6 text-center text-stone-400">
                        还没有设备记录（家人用浏览器打开后会陆续出现）
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-stone-400">
              设备 ID 是浏览器本地生成的随机值（不含任何指纹信息）。标记为可信后该设备的可疑度会下降；
              「IP 数」大于 1 说明这台设备换过网络（例如从 WiFi 切到 4G），属正常现象。
            </p>
          </>
        )}

        {tab === 'sessions' && (
          <>
            <div className="flex justify-end">
              <button
                onClick={handleRevokeAll}
                className="rounded-full bg-red-50 px-4 py-1.5 text-sm text-red-600 transition hover:bg-red-100"
              >
                吊销其他所有会话
              </button>
            </div>
            <div className="overflow-hidden rounded-3xl bg-white shadow-sm">
              <table className="w-full text-left text-sm">
                <thead className="bg-stone-50 text-xs uppercase text-stone-400">
                  <tr>
                    <th className="px-4 py-3">会话指纹</th>
                    <th className="px-2 py-3">账号</th>
                    <th className="px-2 py-3">登录时间</th>
                    <th className="px-2 py-3">到期</th>
                    <th className="px-4 py-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((s) => (
                    <tr key={s.id} className="border-t border-stone-100">
                      <td className="px-4 py-3 font-mono text-xs text-stone-700">
                        {s.id}
                        {s.current && (
                          <span className="ml-2 rounded-full bg-emerald-100 px-2 py-0.5 text-xs text-emerald-700">
                            当前设备
                          </span>
                        )}
                      </td>
                      <td className="px-2 py-3 text-xs text-stone-500">{s.username}</td>
                      <td className="px-2 py-3 text-xs text-stone-500">
                        {formatTime(s.created_at)}
                      </td>
                      <td className="px-2 py-3 text-xs text-stone-500">
                        {s.expires_at ? formatTime(s.expires_at) : '长期'}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {s.current ? (
                          <span className="text-xs text-stone-300">—</span>
                        ) : (
                          <button
                            onClick={() => handleRevoke(s.id)}
                            className="rounded-full bg-stone-100 px-3 py-1 text-xs text-stone-600 transition hover:bg-stone-200"
                          >
                            吊销
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!loading && sessions.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-4 py-6 text-center text-stone-400">
                        没有有效会话
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-stone-400">
              会话指纹是 token 的 SHA-256 前 12 位——服务端只下发指纹，原始 token 永不离开数据库。
            </p>
          </>
        )}
      </div>
    </div>
  )
}

/** 展开区：可疑度解释 + 数据访问审计 + 设备列表 */
function VisitorDetail({
  visitor,
  onToggleTrust,
}: {
  visitor: Visitor
  onToggleTrust: (deviceId: string, trusted: boolean) => void
}) {
  return (
    <div className="mt-3 space-y-2 border-l-2 border-stone-200 pl-3 text-xs text-stone-500">
      {visitor.risk_flags.length > 0 && (
        <div>
          <div className="font-medium text-stone-600">
            可疑点（{visitor.risk_level} {visitor.risk_score}）
          </div>
          <ul className="mt-1 space-y-0.5">
            {visitor.risk_flags.map((f) => (
              <li key={f}>⚠️ {f}</li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <div className="font-medium text-stone-600">
          看过的会话（{visitor.threads_viewed.length}）
        </div>
        {visitor.threads_viewed.length ? (
          <ul className="mt-1 space-y-0.5">
            {visitor.threads_viewed.map((t) => (
              <li key={t}>· {t}</li>
            ))}
          </ul>
        ) : (
          <p className="text-stone-400">没有</p>
        )}
      </div>

      <div>
        <div className="font-medium text-red-600">
          下载过的材料（{visitor.materials_downloaded.length}）
        </div>
        {visitor.materials_downloaded.length ? (
          <ul className="mt-1 space-y-0.5">
            {visitor.materials_downloaded.map((m) => (
              <li key={m}>↓ {m}</li>
            ))}
          </ul>
        ) : (
          <p className="text-stone-400">没有下载过原始文件</p>
        )}
      </div>

      <div className="flex flex-wrap gap-3">
        <span>上传材料 {visitor.uploads}</span>
        <span>发起批改 {visitor.turns}</span>
        <span>登录成功 {visitor.logins_ok}</span>
        <span className={visitor.logins_failed ? 'text-amber-600' : ''}>
          登录失败 {visitor.logins_failed}
        </span>
        <span className={visitor.error_count ? 'text-amber-600' : ''}>
          错误响应 {visitor.error_count}
        </span>
      </div>

      {visitor.sensitive_probes.length > 0 && (
        <div>
          <div className="font-medium text-red-600">探测过的敏感路径</div>
          <ul className="mt-1 space-y-0.5">
            {visitor.sensitive_probes.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}

      {visitor.device_ids.length > 0 ? (
        <div>
          <div className="font-medium text-stone-600">识别到的设备</div>
          <ul className="mt-1 space-y-1">
            {visitor.device_ids.map((id) => (
              <li key={id} className="flex items-center gap-2">
                <span className="font-mono text-[11px]">{id.slice(0, 12)}</span>
                <button
                  onClick={() => onToggleTrust(id, !visitor.trusted_device)}
                  className="rounded-full bg-stone-100 px-2 py-0.5 text-[11px] text-stone-600 transition hover:bg-stone-200"
                >
                  {visitor.trusted_device ? '取消可信' : '标记可信'}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-stone-400">
          没有设备标识（加固前的老记录，或未带 X-Client-Id 的脚本）
        </p>
      )}

      <div>首次出现：{formatTime(visitor.first_seen)}</div>
      <div>
        样本路径：
        <ul className="mt-1 space-y-0.5">
          {visitor.top_paths.map((p) => (
            <li key={p}>· {p}</li>
          ))}
        </ul>
      </div>
    </div>
  )
}
