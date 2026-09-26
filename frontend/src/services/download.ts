/**
 * 触发浏览器下载。
 *
 * 放在 `services/` 而不是组件里：它是一次**副作用**（创建 Blob URL、插临时节点、点它），
 * 与 `storage.ts` 的 localStorage 封装同一性质 —— 组件只管"点了要做什么"。
 *
 * ## 为什么不用 `fetch` + `Content-Disposition`
 *
 * 产物正文本来就在前端手上（生成时流下来的），再打一次后端接口把它取回来纯属多余；
 * 而且 `/sessions/{id}/documents/{kind}` 现在还是 501。所以直接用 Blob。
 *
 * ## 三个容易写错的地方
 *
 * 1. **临时 `<a>` 必须挂进文档再 `click()`。** 游离节点在 Firefox 里不触发下载。
 * 2. **`URL.revokeObjectURL` 不能紧接着调。** Blob URL 是下载**开始时**才被读取的，
 *    立刻释放会让下载被取消（在老一些的 Safari / Firefox 上尤其明显）。
 *    这里延后 `REVOKE_DELAY_MS` 再释放 —— 不释放则是内存泄漏（Blob 一直被引用）。
 * 3. **不加 UTF-8 BOM。** 加了会让文件开头多出 `\ufeff`，Markdown 的第一个标题会被
 *    解析器带上它（git diff 里也是一处噪声）。现代编辑器（VS Code / Win10 1903+ 的
 *    记事本）都能正确识别无 BOM 的 UTF-8。⚠️ 若以后有用户反馈"老记事本打开是乱码"，
 *    改法是在 Blob 前加 `'\ufeff'` 一行 —— 那是一个**明确的取舍**，不是遗漏。
 */

/** 释放 Blob URL 的延迟。见上面第 2 条：太早会取消下载，不释放会泄漏。 */
const REVOKE_DELAY_MS = 1000

/**
 * 把任意文本变成**安全的文件名片段**。
 *
 * 为什么必须有：产物文件名里要放**用户填的产品名**，而它可能含 `/ \ : * ? " < > |` ——
 * 这些在 Windows 上非法，在某些浏览器里还可能被当成路径分隔符（从而写到别的地方去）。
 *
 * 规则：
 * - 非法字符与控制字符 → `-`
 * - 连续的空白 / 连字符折叠成一个 `-`（免得出现 `a---b`）
 * - 去掉首尾的点与连字符（Windows 会悄悄吃掉结尾的点，导致"下载的名字和实际不符"）
 * - 截断到 60 字符（Windows 全路径上限 260，留足余量）
 * - 清理后为空 → 用 `fallback`
 */
export function safeFileName(text: string, fallback = 'document'): string {
  const cleaned = text
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '-')
    .replace(/[\s-]+/g, '-')
    .replace(/^[.\-]+/, '')
    .replace(/[.\-]+$/, '')
    .slice(0, 60)
    // 截断可能正好截在结尾的点/连字符上，再清一次
    .replace(/[.\-]+$/, '')
  return cleaned || fallback
}

/**
 * 弹出一次下载。
 *
 * @param filename 建议的文件名（**应当是安全的** —— 用 `safeFileName()` 清过）
 * @param content 文件内容（文本，按 UTF-8 编码写出）
 * @param mimeType MIME 类型。只影响浏览器对类型的判断，不影响字节内容
 */
export function downloadFile(
  filename: string,
  content: string,
  mimeType = 'text/markdown;charset=utf-8',
): void {
  const blob = new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)

  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  // 不能 `display: none` 之外再加别的骚操作；挂进 body 是为了兼容 Firefox
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)

  window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS)
}
