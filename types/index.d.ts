// 累计的 token 用量（四类分开记，命中率 = 缓存读取 / 全部输入）
export type Totals = {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
}

declare module 'claude-code' {
  interface PluginState {
    // session：本会话累计；lastCost：上次记账时的会话花费（美元），用来算项目累计的增量
    'desk-pet': { session: Totals; lastCost: number }
  }
}
