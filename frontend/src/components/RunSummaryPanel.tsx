/**
 * 完成后的折叠统计（纯展示）。默认收起，只留一行「本次生成：47s · 约 34k tokens」。
 *
 * 展开给的是**可核对**的信息：总耗时、约 tokens（输入/输出）、调用次数、是否自动修订，
 * 以及可复制的 `run_id` + `request_id` —— 出问题时用户能把这一串给维护者，不用整段复述。
 * 原始日志链接、token 百分比、context window 这些都不给（规格里明确"不给用户"）。
 *
 * ## 分片（一次产物生成 = 一次 run）
 *
 * 接口文档 / 提示词套件是**分片发多次请求**生成的，所以：
 * - `part_total > 1` 时收起行必须写明「（3 片合计）」—— 不然用户以为看到的是整份，
 *   而实际上这串数字随时可能只是**已经并进来的那几片**（更早的版本里干脆只有最后一片，
 *   实测用户等了 2 分钟、面板写着 12s）；
 * - `complete === false` 时展开区明说"只并入了 2/3 片" —— **不假装完整**；
 * - run id 与各片 request id 都给出来：`request_id` 只能查到**那一片**，
 *   `run_id` 才是能一次查全整份的键，文案里写清楚这个区别。
 */

import { useState } from 'react'

import type { RunSummary } from '../types'

export function formatTokens(count: number | undefined): string {
  if (!count || count <= 0) return '0'
  if (count >= 1000) return `约 ${Math.round(count / 1000)}k`
  return String(count)
}

/**
 * 人话时长。
 *
 * ⚠️ 加了"分"这一档是有原因的：整份生成实测要两三分钟，只写到秒的话面板上会出现
 * 「本次生成：161s」—— 用户得自己换算才知道等了多久（分片合计后这个数字只会更长）。
 */
export function formatDuration(ms: number | undefined): string {
  if (!ms || ms <= 0) return '0s'
  if (ms < 1000) return `${ms}ms`
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`
}

interface RunSummaryPanelProps {
  summary: RunSummary | null
  defaultCollapsed?: boolean
  /**
   * 这一趟**是否还在生成中**。
   *
   * 区分它是因为"片数没并完"有两种完全不同的含义：生成中的分片是**正常的中间态**
   * （第 1 片的帧里就只有 1/3），跑完还不足片数才是真的有一片没并进来。
   * 不区分的话，用户每看到第 1 片的帧就会读到一句"有一片没并进来"—— 那是误报。
   */
  streaming?: boolean
}

export default function RunSummaryPanel({
  summary,
  defaultCollapsed = true,
  streaming = false,
}: RunSummaryPanelProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  const [copied, setCopied] = useState(false)
  if (!summary) return null

  const tokens = (summary.total_input_tokens ?? 0) + (summary.total_output_tokens ?? 0)
  // 分片数优先取本帧报的 `part_total`；它缺（老后端）时退回已经并进来的片数，
  // 这样"看得出是多片"这件事在缺字段时也不会丢。
  const partTotal = summary.part_total ?? summary.parts_seen ?? 1
  const partsSeen = summary.parts_seen ?? partTotal
  const multiPart = partTotal > 1
  /**
   * 整份是否已经并完。
   *
   * ⚠️ 缺 `complete` 时按 `true`（相信后端说完了）—— 老后端不发这个键，
   * 一律按"没完成"显示的话，每份产物下面都会挂一句"只并入了 X/Y 片"，那是误报。
   * 判定不靠 `complete` 一个键：片数对不上也说明没并完（`complete` 只是后端的话）。
   * 生成中（`streaming`）的"片数不足"是**正常中间态**，交给上面的中间态文案，不算没并进来。
   */
  const incomplete = !streaming && (summary.complete === false || partsSeen < partTotal)
  /** 当前这一片的耗时。老后端只给 `total_duration_ms`（那时它就是本片墙钟），所以兜底取它。 */
  const partDuration = summary.part_duration_ms ?? summary.total_duration_ms
  /** run id 缺（老后端回滚、或非分片预览）时退回这一片的 request_id，至少让用户能复制点东西。 */
  const runId = summary.run_id ?? summary.request_id
  const requestIds = summary.request_ids?.length ? summary.request_ids : [summary.request_id]

  const copy = () => {
    // 复制的是**一整块可检索文本**而不是裸 id：用户把这段贴给维护者时，
    // 顺手说明了"这两个 id 各是干什么的"，省一轮来回（run_id 查整份、request_id 查单片）。
    void navigator.clipboard?.writeText(
      [
        `run_id（拿这串能查到整份生成）：${runId}`,
        'request_id（各分片）：',
        ...requestIds.map((id) => `  ${id}`),
      ].join('\n'),
    )
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div data-testid="run-summary" className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs">
      <button
        type="button"
        onClick={() => setCollapsed((prev) => !prev)}
        className="flex w-full items-center gap-2 text-left text-slate-600"
      >
        <span aria-hidden>{collapsed ? '▸' : '▾'}</span>
        <span>
          {multiPart ? `本次生成（${partTotal} 片合计）：` : '本次生成：'}
          {formatDuration(summary.total_duration_ms)} · {formatTokens(tokens)} tokens
        </span>
      </button>

      {!collapsed && (
        <dl className="mt-2 grid gap-1 text-slate-500">
          {multiPart && (
            <div className="flex flex-wrap gap-2">
              <dt>分片</dt>
              <dd className="text-slate-700">
                {streaming
                  ? `已并入 ${partsSeen}/${partTotal} 片（第 ${summary.part_index ?? partsSeen} 片刚跑完，后面的片还没发出或还在跑）`
                  : incomplete
                    ? `只并入了 ${partsSeen}/${partTotal} 片（有一片没并进来）—— 下面的数字是这几片的合计，不是整份。`
                    : `${partTotal}/${partTotal} 片（整份已并完）`}
              </dd>
            </div>
          )}
          <div className="flex gap-2">
            <dt>{multiPart ? '整份总耗时' : '总耗时'}</dt>
            <dd className="text-slate-700">{formatDuration(summary.total_duration_ms)}</dd>
          </div>
          {multiPart && (
            <div className="flex gap-2">
              <dt>本片耗时</dt>
              <dd className="text-slate-700">
                第 {summary.part_index ?? '?'} 片 · {formatDuration(partDuration)}
              </dd>
            </div>
          )}
          <div className="flex gap-2">
            {/* 分片时数字随进度变：第 1 片的帧只累到第 1 片，最后一片才是整份合计 */}
            <dt>{multiPart ? 'tokens（已并入各片）' : '约 tokens'}</dt>
            <dd className="text-slate-700">
              输入 {formatTokens(summary.total_input_tokens)} / 输出 {formatTokens(summary.total_output_tokens)}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt>模型调用</dt>
            <dd className="text-slate-700">{summary.llm_call_count} 次</dd>
          </div>
          <div className="flex gap-2">
            <dt>自动修订</dt>
            <dd className="text-slate-700">{summary.revision_applied ? '改过一轮' : '未触发'}</dd>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <dt>run ID</dt>
            <dd className="font-mono text-slate-700">{runId}</dd>
            <dd>（拿这串能查到整份生成）</dd>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <dt>请求 ID</dt>
            <dd className="font-mono text-slate-700">
              {multiPart ? `${requestIds.length} 个：${requestIds.join('、')}` : summary.request_id}
            </dd>
            <dd>（一片一个，查单片日志用）</dd>
            <button
              type="button"
              data-testid="copy-request-id"
              onClick={copy}
              className="rounded border border-slate-300 px-1.5 py-0.5 text-xs text-slate-600 transition hover:bg-white"
            >
              {copied ? '已复制' : '复制 run ID 与请求 ID'}
            </button>
          </div>
        </dl>
      )}
    </div>
  )
}
