#!/usr/bin/env node
/**
 * 拦一个**已经犯过三次**的错误：在 JSX 文本里写 Markdown 记号。
 *
 *     <p>题目**必须**由服务端下发</p>      ← 用户看到的就是带星号的这句话
 *
 * JSX 文本节点不是 Markdown，`**` 不会被渲染成加粗。这和"把 AI 回复的原始 JSON
 * 直接渲染出来"（`HANDOFF.md` §4 坑 #15）是**同一类错误**：面向人的文本带着记号原样出现。
 *
 * 为什么要有这个脚本而不是写进 README 让人自觉：本项目**已经在 5 处踩到**（其中一处是
 * 从旧代码继承来的），最后一次是刚写完这条 README 说明之后又踩的。规则记在文档里没用，
 * 得让它在 `pnpm build` 里失败。
 *
 * ## 判据
 *
 * 去掉注释之后，源码里**不该再出现 `**`**：
 *
 * - 要加粗 → 写 `<strong className="font-medium">`
 * - 纯字符串里（如三元表达式的文案）→ 去掉强调，别指望它渲染
 * - 注释里随便写 → 注释会被剥掉，不误报
 *
 * 刻意**不剥字符串字面量**：那种地方的 `**` 恰恰是重灾区（`'重新生成会**覆盖**它'`），
 * 必须报出来。
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = new URL('../src/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

function walk(dir) {
  const found = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) found.push(...walk(full))
    else if (/\.tsx?$/.test(entry)) found.push(full)
  }
  return found
}

// 去掉三种注释：JSX 的注释块（大括号斜杠星号）、块注释、行注释。
// ⚠️ 这里不能把 JSX 注释的字面写法写进注释里 —— 它内部的结束符会提前终止本行注释。
function stripComments(text) {
  return text
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '')
}

const offences = []
for (const file of walk(SRC)) {
  const raw = readFileSync(file, 'utf8')
  stripComments(raw)
    .split('\n')
    .forEach((line, index) => {
      if (line.includes('**')) {
        offences.push({ file: relative(SRC, file), line: index + 1, text: line.trim() })
      }
    })
}

if (offences.length > 0) {
  console.error('[FAIL] JSX/TS 文本里出现了 Markdown 记号 `**`（不会被渲染，会原样显示给用户）：\n')
  for (const { file, line, text } of offences) {
    console.error(`  src/${file}:${line}`)
    console.error(`      ${text.slice(0, 120)}`)
  }
  console.error('\n要加粗请写 <strong className="font-medium">；纯字符串里请去掉强调。')
  process.exit(1)
}

console.log('[PASS] 没有 Markdown 记号泄漏到纯文本（`**`）')
