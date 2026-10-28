import type { EngineInterface, Register } from 'claude-code'

import type { Totals } from '../types'

// 桌宠的状态（悬浮窗程序按这个名字换表情）
type PetState = 'thinking' | 'tool' | 'reading' | 'waiting' | 'celebrate' | 'error' | 'aborted' | 'stone'

type ProjectTotals = Totals & { costUsd: number; sessions: number }

type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}

const SESSION = { plugin: 'desk-pet', key: 'session' } as const
const LAST_COST = { plugin: 'desk-pet', key: 'lastCost' } as const
const EMPTY: Totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }

// 读资料类工具 → 记笔记表情；其余 → 敲键盘
const READING = new Set(['Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'NotebookRead'])

// 会话级的变量（重载 mod 时会在 session.start 里重新算）
let runDir = ''
let petFile = ''
let project = ''
let cwd = ''
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
  if (tool === 'Agent') return '派小助手干活中'
  if (tool.startsWith('mcp__')) return `在用 ${tool.split('__').pop()}`
  return `在用 ${tool}`
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

async function refreshStatus($: EngineInterface) {
  const { value: t = EMPTY } = await $.state.get(SESSION)
  const usage = await $.session.usage()
  const parts = [
    `📊 本会话 输入 ${fmt(t.input + t.cacheRead + t.cacheWrite)}`,
    `输出 ${fmt(t.output)}`,
    `缓存命中 ${hitRate(t)}`,
  ]
  if (usage.cost) parts.push(`$${usage.cost.usd.toFixed(2)}`)
  if (usage.context.percent !== undefined) parts.push(`上下文 ${usage.context.percent}%`)
  const fiveHour = usage.rateLimits.find(r => r.kind === 'five_hour')
  if (fiveHour) parts.push(`5h额度 ${fiveHour.percentUsed}%`)
  $.ui.status(parts.join(' · '))
}

// 把本轮用量记进会话累计和项目累计（项目累计跨会话保存）
async function account($: EngineInterface, u: Usage | undefined) {
  const { value: t = EMPTY } = await $.state.get(SESSION)
  const add: Totals = u
    ? { input: u.input_tokens, output: u.output_tokens, cacheRead: u.cache_read_input_tokens, cacheWrite: u.cache_creation_input_tokens }
    : EMPTY
  await $.state.set(SESSION, {
    input: t.input + add.input,
    output: t.output + add.output,
    cacheRead: t.cacheRead + add.cacheRead,
    cacheWrite: t.cacheWrite + add.cacheWrite,
  })

  const usage = await $.session.usage()
  const cost = usage.cost?.usd ?? 0
  const { value: lastCost = 0 } = await $.state.get(LAST_COST)
  await $.state.set(LAST_COST, cost)

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

async function statsText($: EngineInterface) {
  const { value: t = EMPTY } = await $.state.get(SESSION)
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
    petFile = `${runDir}/sessions/${await $.session.id()}.json`

    // 新会话在项目累计里记一次
    const { value: seen } = await $.state.get(SESSION)
    if (seen === undefined) {
      await $.state.set(SESSION, EMPTY)
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
      await pet($, 'thinking', '思考中')
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
        await pet($, 'thinking', '思考中')
      }
    }
    return ran
  }).catch(($, e, next) => next(e)) // 桌宠这边出任何错，都照常放行工具调用

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    try {
      await account($, e.usage)
      await refreshStatus($)
    } catch (err) {
      $.ui.log(`用量统计失败：${String(err)}`, { to: 'debug' })
    }

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
