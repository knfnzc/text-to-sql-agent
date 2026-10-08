/**
 * 后端接口的调用封装。
 *
 * 为什么要把 fetch / EventSource 集中在这一个文件：
 *    1. 前端组件只关心"数据是什么形状"，不关心 URL 怎么拼、错误怎么归一化。
 *       将来后端加版本前缀、加鉴权头，改动只落在这里。
 *    2. 错误处理统一。HTTP 非 2xx 时后端返回的是 FastAPI 的
 *       `{"detail": ...}` 结构，detail 可能是字符串也可能是校验错误数组，
 *       逐个组件去解析一定会写歪，集中处理最省事。
 *
 * 关于 SSE：
 *    浏览器的 EventSource 只支持 GET，所以流式问答走
 *    `GET /api/query/stream?question=...&with_answer=...`。
 *    后端给每个事件都起了名字（stage / trace / result / error / done），
 *    带名字的事件不会触发 onmessage，必须用 addEventListener 逐个订阅。
 *    这一点很容易踩坑：只写 onmessage 的话会一条都收不到。
 */

import type {
  HealthResponse,
  IndexStatsResponse,
  LLMConfigResponse,
  LLMConfigUpdateRequest,
  MetaResponse,
  QueryRequest,
  QueryResponse,
  RebuildIndexResponse,
  StreamHandlers,
  TableInfo,
  TraceItem,
  StageEvent,
} from '@/types/api'

/** 接口前缀。默认走 Vite 代理，部署到别处时用 VITE_API_BASE 覆盖 */
export const API_BASE: string = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '')

/** 强制离线演示模式：不请求后端，直接用内置样例数据 */
export const FORCE_DEMO: boolean = import.meta.env.VITE_DEMO === 'true'

/**
 * 把后端返回的错误体归一化成一条可读的消息。
 *
 * FastAPI 的错误体有两种形态：
 *   业务错误  {"detail": "问题不能为空"}
 *   校验错误  {"detail": [{"loc": [...], "msg": "...", "type": "..."}]}
 * 两种都要能读，否则界面上只会显示 "[object Object]"。
 */
function extractErrorMessage(status: number, body: unknown, fallback: string): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail

    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }

    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            const record = item as { msg?: unknown; loc?: unknown }
            const location = Array.isArray(record.loc)
              ? record.loc.filter((part) => part !== 'body').join('.')
              : ''
            return location ? `${location}: ${String(record.msg)}` : String(record.msg)
          }
          return String(item)
        })
        .filter(Boolean)

      if (messages.length) {
        return messages.join('；')
      }
    }
  }

  return `${fallback}（HTTP ${status}）`
}

/** 统一的请求入口：拼 URL、检查状态码、解析 JSON */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response

  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
      ...init,
    })
  } catch (error) {
    // fetch 只在网络层失败时 reject（后端没启动、跨域被拦、断网）。
    // 这里补一句人话，否则界面上只会显示 "Failed to fetch"
    throw new Error(
      `无法连接后端服务（${API_BASE}）。请确认后端已启动，` +
        `或在 backend/ 目录执行 uvicorn app.main:app --reload --port 8000。` +
        `原始错误：${(error as Error).message}`,
    )
  }

  const text = await response.text()
  let parsed: unknown = null

  if (text) {
    try {
      parsed = JSON.parse(text)
    } catch {
      // 后端理论上不会返回非 JSON，但代理层出错时会（比如返回一段 HTML 错误页）。
      // 保留原文便于定位，比抛一个 "Unexpected token < in JSON" 有用得多
      parsed = { detail: text.slice(0, 300) }
    }
  }

  if (!response.ok) {
    throw new Error(extractErrorMessage(response.status, parsed, `请求失败：${path}`))
  }

  return parsed as T
}

// ===============================================================
// 元信息接口
// ===============================================================

/** 健康检查：服务与依赖是否正常 */
export function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}

/** 配置与限制信息，用于侧边栏展示 */
export function fetchMeta(): Promise<MetaResponse> {
  return request<MetaResponse>('/meta')
}

/** 表结构与字段业务说明，用于数据字典面板 */
export function fetchTables(): Promise<TableInfo[]> {
  return request<TableInfo[]>('/tables')
}

/** Schema 向量索引统计 */
export function fetchIndexStats(): Promise<IndexStatsResponse> {
  return request<IndexStatsResponse>('/index/stats')
}

/**
 * 重建 Schema 向量索引。
 *
 * 注意：真实模式下这一步会调用向量化接口并产生费用，
 * 所以调用方必须先做二次确认，不要一进入页面就自动触发。
 */
export function rebuildIndex(force = true): Promise<RebuildIndexResponse> {
  return request<RebuildIndexResponse>('/index/rebuild', {
    method: 'POST',
    body: JSON.stringify({ force }),
  })
}

// ===============================================================
// 运行时模型配置
// ===============================================================

/** 读取当前生效的模型配置。密钥只返回脱敏形式 */
export function fetchLLMConfig(): Promise<LLMConfigResponse> {
  return request<LLMConfigResponse>('/llm/config')
}

/**
 * 更新模型配置，下一次提问生效。
 *
 * 只提交改动过的字段：后端按"未提交即不修改"处理，
 * 所以不要为了图省事把整个表单都塞进来。
 */
export function updateLLMConfig(payload: LLMConfigUpdateRequest): Promise<LLMConfigResponse> {
  return request<LLMConfigResponse>('/llm/config', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 清空界面上的覆盖，恢复为 .env 里的配置 */
export function resetLLMConfig(): Promise<LLMConfigResponse> {
  return request<LLMConfigResponse>('/llm/config/reset', { method: 'POST' })
}

// ===============================================================
// 问答接口
// ===============================================================

/** 一问一答（阻塞式）。适合脚本与接口联调，界面交互请用流式版本 */
export function askQuestion(payload: QueryRequest): Promise<QueryResponse> {
  return request<QueryResponse>('/query', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 流式问答返回的取消句柄 */
export interface StreamController {
  /** 主动断开连接。组件卸载时必须调用，否则连接会一直挂着 */
  close: () => void
}

/**
 * 以 SSE 流式方式提问，边执行边把过程推给界面。
 *
 * 事件契约（后端 app/api/service.py 里定义）：
 *   stage   某个节点完成 / 开始，前端据此更新步骤条
 *   trace   一条执行轨迹，追加到时间线
 *   result  最终结果（完整 QueryResponse）
 *   error   业务失败
 *   done    流结束标记。后端保证无论成功失败最后一定发它，
 *           所以前端只要收到 done 就能安全地结束 loading 状态
 *
 * @param question   自然语言问题
 * @param withAnswer 是否生成结论与图表配置
 * @param handlers   各类事件的回调，全部可选
 * @returns 取消句柄
 */
export function streamQuestion(
  question: string,
  withAnswer: boolean,
  handlers: StreamHandlers,
): StreamController {
  const params = new URLSearchParams({
    question,
    with_answer: String(withAnswer),
  })
  const url = `${API_BASE}/query/stream?${params.toString()}`

  const source = new EventSource(url)

  // 用一个标记保证 done / error 只回调一次。
  // 原生 EventSource 在服务端正常关闭后会触发 onerror 并自动重连，
  // 不拦住的话界面会在收到 done 之后又报一次"连接中断"，很误导人
  let finished = false

  const finish = (): void => {
    if (finished) return
    finished = true
    source.close()
    handlers.onDone?.()
  }

  source.addEventListener('stage', (event) => {
    handlers.onStage?.(JSON.parse((event as MessageEvent).data) as StageEvent)
  })

  source.addEventListener('trace', (event) => {
    handlers.onTrace?.(JSON.parse((event as MessageEvent).data) as TraceItem)
  })

  source.addEventListener('result', (event) => {
    handlers.onResult?.(JSON.parse((event as MessageEvent).data) as QueryResponse)
  })

  // 用 addEventListener 而不是 onerror 来订阅 error，原因是这两者在浏览器里
  // 是同一个事件名的两个订阅入口，同时使用会导致一次故障回调两遍。
  //
  // 而 error 这个名字同时承担两种含义，必须靠结构区分：
  //   服务端发来的业务错误 -> MessageEvent，带 data（JSON 字符串）
  //   连接层故障（后端挂了）-> 普通 Event，没有 data
  // 判据就是 data 存不存在。
  source.addEventListener('error', (event) => {
    if (finished) return
    finished = true
    source.close()

    const raw = (event as MessageEvent).data

    if (typeof raw === 'string' && raw) {
      let message = '执行失败'
      try {
        message = (JSON.parse(raw) as { message?: string }).message || message
      } catch {
        message = raw
      }
      handlers.onError?.({ message })
    } else {
      handlers.onTransportError?.(
        new Error('与后端的连接中断。请确认后端服务仍在运行，然后重试。'),
      )
    }

    handlers.onDone?.()
  })

  source.addEventListener('done', () => {
    finish()
  })

  return { close: finish }
}
