import type { EngineInterface, Register } from 'claude-code'

// 累计的 token 用量（四类分开记，命中率 = 缓存读取 / 全部输入）
type Totals = {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
}

// 桌宠的状态（悬浮窗程序按这个名字换表情）
type PetState = 'thinking' | 'tool' | 'reading' | 'waiting' | 'celebrate' | 'error' | 'aborted' | 'stone'

type ProjectTotals = Totals & { costUsd: number; sessions: number }

type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}

// 本会话的记账：totals 是累计用量，lastCost 是上次记账时的会话花费（美元），用来算项目累计的增量
// 存在 $.store 里按会话 id 分开（不用 $.state，那样 plugin.json 就不需要 types 字段，旧版本也能装）
type SessionRecord = { totals: Totals; lastCost: number }
const EMPTY: Totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }

// 读资料类工具 → 记笔记表情；其余 → 敲键盘
const READING = new Set(['Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'NotebookRead'])

// 会话级的变量（重载 mod 时会在 session.start 里重新算）
let runDir = ''
let petFile = ''
let project = ''
let cwd = ''
let sessionKey = ''
let toolErrors = 0

const fmt = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : `${n}`

const hitRate = (t: Totals) => {
  const all = t.input + t.cacheRead + t.cacheWrite
  return all === 0 ? '—' : `${Math.round((t.cacheRead / all) * 100)}%`
}

const baseName = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p

// 工具名 → 气泡里的一句话
const describe = (tool: string, input: Record<string, unknown>) => {
  const file = typeof input.file_path === 'string' ? baseName(input.file_path) : ''
  if (tool === 'Bash' || tool === 'PowerShell') return '在跑命令…'
  if (tool === 'Edit' || tool === 'Write') return file ? `在改 ${file}` : '在改文件…'
  if (tool === 'Read') return file ? `在读 ${file}` : '在翻文件…'
  if (tool === 'Grep' || tool === 'Glob') return '在找东西…'
  if (tool === 'WebSearch' || tool === 'WebFetch') return '在上网查…'
  if (tool === 'Agent' || tool === 'Task') return '派小助手干活中…'
  if (tool.startsWith('mcp__')) return describeMcp(tool)
  return `在用 ${tool}…`
}

// MCP 工具名形如 mcp__<服务名>__<工具名>，服务名有时是一串 id，直接显示不好看
const describeMcp = (tool: string) => {
  const [, server = '', name = ''] = tool.split('__')
  if (/browser|chrome|playwright|puppeteer/i.test(server)) return '在操作浏览器…'
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(server) || !server) return name ? `在用 ${name}…` : '在用扩展工具…'
  return `在用 ${server}…`
}

async function readSession($: EngineInterface) {
  return ((await $.store.get(sessionKey)) as SessionRecord | undefined) ?? { totals: EMPTY, lastCost: 0 }
}

// 写状态文件给悬浮窗读；sound 让悬浮窗播放提示音（引擎在 Windows 上没有播放器）
// 出错也不能影响 Claude 干活
async function pet($: EngineInterface, state: PetState, text: string, sound?: 'done' | 'error') {
  if (!petFile) return
  try {
    const ts = await $.clock.now()
    await $.fs.write(petFile, JSON.stringify({ state, text, project, ts, sound }))
  } catch {}
}

async function launchPet($: EngineInterface) {
  try {
    // 引擎不按 PATH 找程序，交给 Windows 自带的 cmd.exe 去找 python
    // （cmd.exe 的路径必须用反斜杠，用正斜杠时 cmd 会把 /Windows 当成开关）
    const ran = await $.process.run(
      ['C:\\Windows\\System32\\cmd.exe','/d', '/c', 'python', `${$.plugin.root}/pet/launch.py`, `${$.plugin.root}/pet/pet.pyw`, runDir],
      { timeoutMs: 15000 },
    )
    if (ran.exitCode !== 0) {
      await $.fs.write(`${runDir}/mod-launch.log`, `exit=${ran.exitCode}\n${ran.stdout}\n${ran.stderr}`)
    }
  } catch (err) {
    $.ui.log(`桌宠启动失败：${String(err)}`, { to: 'debug' })
    await $.fs.write(`${runDir}/mod-launch.log`, `启动 python 失败：${String(err)}`).catch(() => {})
  }
}

// 状态栏的中英文
type Lang = 'zh' | 'en'
const STATUS_TEXT = {
  zh: { session: '当前会话', input: '输入', output: '输出', cacheHit: '缓存命中', context: '上下文', fiveHour: '5h额度' },
  en: { session: 'Session', input: 'Input', output: 'Output', cacheHit: 'Cache hit', context: 'Context', fiveHour: '5h Limit' },
}

// 系统语言是中文就用中文，否则用英文
const systemLang = (): Lang => {
  try {
    return /^zh/i.test(Intl.DateTimeFormat().resolvedOptions().locale) ? 'zh' : 'en'
  } catch {
    return 'zh'
  }
}

// 语言：桌宠右键菜单里选的（存在 settings.json 的 lang），没选过就跟随系统
async function statusLang($: EngineInterface): Promise<Lang> {
  try {
    const text = await $.fs.read(`${runDir}/settings.json`)
    const lang = typeof text === 'string' ? JSON.parse(text).lang : undefined
    if (lang === 'zh' || lang === 'en') return lang
  } catch {}
  return systemLang()
}

async function refreshStatus($: EngineInterface) {
  const t = (await readSession($)).totals
  const usage = await $.session.usage()
  const s = STATUS_TEXT[await statusLang($)]
  const parts = [
    `${s.session} ${s.input} ${fmt(t.input + t.cacheRead + t.cacheWrite)}`,
    `${s.output} ${fmt(t.output)}`,
    `${s.cacheHit} ${hitRate(t)}`,
  ]
  if (usage.context.percent !== undefined) parts.push(`${s.context} ${usage.context.percent}%`)
  const fiveHour = usage.rateLimits.find(r => r.kind === 'five_hour')
  if (fiveHour) parts.push(`${s.fiveHour} ${fiveHour.percentUsed}%`)
  $.ui.status(parts.join(' · '))
}

// 把一次请求的用量记进会话累计和项目累计（项目累计跨会话保存）；u 为空时只更新花费
async function account($: EngineInterface, u: Usage | undefined) {
  const { totals: t, lastCost } = await readSession($)
  const add: Totals = u
    ? { input: u.input_tokens, output: u.output_tokens, cacheRead: u.cache_read_input_tokens, cacheWrite: u.cache_creation_input_tokens }
    : EMPTY
  const usage = await $.session.usage()
  const cost = usage.cost?.usd ?? 0
  const record: SessionRecord = {
    totals: {
      input: t.input + add.input,
      output: t.output + add.output,
      cacheRead: t.cacheRead + add.cacheRead,
      cacheWrite: t.cacheWrite + add.cacheWrite,
    },
    lastCost: cost,
  }
  await $.store.set(sessionKey, record)

  const key = `project:${cwd}`
  const p = ((await $.store.get(key)) as ProjectTotals | undefined) ?? { ...EMPTY, costUsd: 0, sessions: 1 }
  await $.store.set(key, {
    input: p.input + add.input,
    output: p.output + add.output,
    cacheRead: p.cacheRead + add.cacheRead,
    cacheWrite: p.cacheWrite + add.cacheWrite,
    costUsd: p.costUsd + Math.max(0, cost - lastCost),
    sessions: p.sessions,
  })
}

// 每次模型请求结束就记一次账并刷新状态栏。记账排队执行：子代理并行时几个请求可能同时结束，
// 同时读-改-写 $.store 会互相覆盖
let accounting: Promise<void> = Promise.resolve()
function recordUsage($: EngineInterface, u: Usage | undefined) {
  accounting = accounting
    .then(() => account($, u))
    .then(() => refreshStatus($))
    .catch(err => $.ui.log(`用量统计失败：${String(err)}`, { to: 'debug' }))
  return accounting
}

async function statsText($: EngineInterface) {
  const t = (await readSession($)).totals
  const p = (await $.store.get(`project:${cwd}`)) as ProjectTotals | undefined
  const usage = await $.session.usage()
  const line = (name: string, x: Totals) =>
    `${name}：输入 ${fmt(x.input + x.cacheRead + x.cacheWrite)}（缓存读取 ${fmt(x.cacheRead)}、缓存写入 ${fmt(x.cacheWrite)}、未缓存 ${fmt(x.input)}） · 输出 ${fmt(x.output)} · 缓存命中 ${hitRate(x)}`
  const rows = [
    line('本会话', t) + (usage.cost ? ` · $${usage.cost.usd.toFixed(2)}` : ''),
    p ? line(`本项目（${project}，${p.sessions} 个会话）`, p) + ` · $${p.costUsd.toFixed(2)}` : '本项目：还没有记录',
    ...usage.rateLimits.map(r =>
      `额度 ${r.kind === 'five_hour' ? '5 小时' : r.kind === 'seven_day' ? '7 天' : r.kind}：已用 ${r.percentUsed}%` +
      (r.resetsAt ? `，${new Date(r.resetsAt).toLocaleString('zh-CN', { hour12: false })} 重置` : ''),
    ),
    '（项目累计从装上这个 mod 后开始算）',
  ]
  return rows.join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    cwd = e.cwd
    project = baseName(e.cwd)
    // 运行目录放在 ~/.claude/desk-pet，不放进 mod 文件夹（避免触发热重载）
    const home = ((await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '').replace(/\\/g, '/')
    runDir = `${home}/.claude/desk-pet`
    const sessionId = await $.session.id()
    petFile = `${runDir}/sessions/${sessionId}.json`
    sessionKey = `session:${sessionId}`

    // 新会话在项目累计里记一次
    if ((await $.store.get(sessionKey)) === undefined) {
      await $.store.set(sessionKey, { totals: EMPTY, lastCost: 0 })
      const key = `project:${cwd}`
      const p = (await $.store.get(key)) as ProjectTotals | undefined
      if (p) await $.store.set(key, { ...p, sessions: p.sessions + 1 })
    }

    await $.command.register({ name: 'token-stats', description: '查看本会话和本项目累计的 token 用量、缓存命中率' })
    await $.command.register({ name: 'pet', description: '重新召唤桌宠悬浮窗' })

    await pet($, 'waiting', '我来啦～')
    void launchPet($)
    void refreshStatus($).catch(() => {})
    return started
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      toolErrors = 0
      await pet($, 'thinking', '思考中…')
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const isMain = e.agentId === undefined
    if (isMain) {
      const input = e as unknown as Record<string, unknown>
      await pet($, READING.has(e.tool) ? 'reading' : 'tool', describe(e.tool, input))
    }

    const ran = await next(e)

    if (isMain) {
      if (ran.deny === undefined && ran.isError === true) {
        toolErrors += 1
        // 连续出错 3 次：石化彩蛋
        if (toolErrors >= 3) await pet($, 'stone', '石化了…')
        else await pet($, 'error', '出错了…')
      } else {
        toolErrors = 0
        await pet($, 'thinking', '思考中…')
      }
    }
    return ran
  }).catch(($, e, next) => next(e)) // 桌宠这边出任何错，都照常放行工具调用

  // 一轮里的每次模型请求（主循环和子代理都算）：响应完整收到后立刻记账，状态栏不用等整轮结束
  // 只读结果、不改内容；记账出错只写调试日志，不影响模型回复
  on('turn.step', async function* ($, e, next) {
    const response = yield* next(e)
    await recordUsage($, response.usage ?? undefined)
    return response
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    // 用量已经在 turn.step 里按请求记过了（一轮的用量就是各次请求之和），这里只刷新额度和上下文
    await recordUsage($, undefined)

    if (e.agentId !== undefined) return done

    if (e.reason === 'aborted') {
      await pet($, 'aborted', '被打断了…')
    } else if (e.reason === 'error' || e.reason === 'refusal') {
      await pet($, 'error', '出错了，来看看？', 'error')
    } else if (e.durationMs > 180_000) {
      // 干了 3 分钟以上的大活：撒花
      await pet($, 'celebrate', '搞定啦！轮到你～', 'done')
    } else {
      await pet($, 'waiting', '轮到你啦！', 'done')
    }
    return done
  })

  on('command.run', { command: 'token-stats' }, async ($, e, next) => ({ text: await statsText($) }))

  on('command.run', { command: 'pet' }, async ($, e, next) => {
    await launchPet($)
    await pet($, 'waiting', '我在这儿～')
    return { text: '桌宠已召唤（如果它本来就开着，就不会再开第二个）。' }
  })
}
