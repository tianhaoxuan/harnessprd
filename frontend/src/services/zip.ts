/**
 * 纯前端打 zip（**不引第三方依赖**）。
 *
 * ## 为什么不用 jszip / 让后端打
 *
 * - **不引 jszip**：整个仓库至今没有为"造一个 zip"加过依赖，而 zip 的 store 格式本来就简单
 *   （本地头 + 中央目录 + EOCD）。多一个依赖就多一份供应链与体积，收益只是少写这一百来行。
 * - **不走后端**：产物正文本来就在前端手上（生成时流下来的），再传回去打包纯属绕路；
 *   而且打包是**纯本地**动作，离线环境下也必须能用。
 *
 * ## 压缩
 *
 * 用浏览器的 `CompressionStream('deflate-raw')` —— 它是原生实现，没有 JS 依赖，
 * 压缩率与 zlib 同级。**不支持时回落到 store（不压缩）**：zip 允许混合方法，
 * 文件照样能正常解开，只是体积大些。宁可"能用但没压"，也不要打包失败。
 *
 * ## 三处必须写对的细节
 *
 * 1. **CRC32 算的是"未压缩"的数据**（不是压缩后的），且是 zip 规定的那个多项式
 *    （`0xEDB88320` 反射式）。算错的话解压工具会报"数据损坏"。
 * 2. **文件名要走 UTF-8 且置通用位 11（`0x0800`）**。不置位的话，Windows 资源管理器
 *    会按本地代码页解释中文文件名 —— 打开是乱码（`ÄãºÃ.md` 这种）。
 * 3. **时间戳用 DOS 格式**（自 1980 年起，秒精度 2 秒）。用 Unix 时间戳会得到
 *    1970 年或溢出成未来时间。
 */

/** zip 里的一项。`path` 可含 `/`（会自动建成目录）。 */
export interface ZipEntry {
  path: string
  content: string
}

/** DOS 时间（两个 16 位字段：时间 + 日期）。 */
function dosDateTime(date: Date): { time: number; date: number } {
  const year = Math.max(1980, date.getFullYear())
  return {
    time:
      (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
    date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  }
}

/** CRC32 表（只算一次，之后每次查表 —— 逐位算在几 MB 的产物上会明显卡）。 */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let i = 0; i < 256; i += 1) {
    let value = i
    for (let bit = 0; bit < 8; bit += 1) {
      value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1
    }
    table[i] = value >>> 0
  }
  return table
})()

/** zip 规定的 CRC32。注意：**喂进来的是未压缩数据**。 */
export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff
  for (let i = 0; i < data.length; i += 1) {
    crc = CRC_TABLE[(crc ^ data[i]) & 0xff] ^ (crc >>> 8)
  }
  return (crc ^ 0xffffffff) >>> 0
}

/** 能拿到原生 deflate 就用它，拿不到返回 `null`（调用方回落 store）。 */
async function deflateRaw(data: Uint8Array): Promise<Uint8Array | null> {
  if (typeof CompressionStream === 'undefined') return null
  try {
    const stream = new Blob([data as BlobPart])
      .stream()
      .pipeThrough(new CompressionStream('deflate-raw'))
    return new Uint8Array(await new Response(stream).arrayBuffer())
  } catch {
    // 部分浏览器/内核没有 deflate-raw（只有 'deflate' 带 zlib 头，zip 不认）
    return null
  }
}

class ByteWriter {
  private parts: Uint8Array[] = []
  private length = 0

  bytes(data: Uint8Array): void {
    this.parts.push(data)
    this.length += data.length
  }

  u16(value: number): void {
    this.bytes(new Uint8Array([value & 0xff, (value >>> 8) & 0xff]))
  }

  u32(value: number): void {
    this.bytes(
      new Uint8Array([
        value & 0xff,
        (value >>> 8) & 0xff,
        (value >>> 16) & 0xff,
        (value >>> 24) & 0xff,
      ]),
    )
  }

  get size(): number {
    return this.length
  }

  toUint8Array(): Uint8Array {
    const out = new Uint8Array(this.length)
    let offset = 0
    for (const part of this.parts) {
      out.set(part, offset)
      offset += part.length
    }
    return out
  }
}

/**
 * 把若干文本文件打成一个 zip（未压缩数据用 UTF-8 编码）。
 *
 * 空条目（`path` 为空或文件名为空）会被跳过 —— 少一个文件好过生成一个打不开的包。
 */
export async function buildZip(entries: ZipEntry[], now = new Date()): Promise<Uint8Array> {
  const encoder = new TextEncoder()
  const { time, date } = dosDateTime(now)
  const central = new ByteWriter()
  const local = new ByteWriter()
  const offsets: number[] = []
  const prepared: {
    nameBytes: Uint8Array
    raw: Uint8Array
    data: Uint8Array
    crc: number
    method: number
  }[] = []

  for (const entry of entries) {
    const path = entry.path.replace(/\\/g, '/').replace(/^\/+/, '')
    if (!path || path.endsWith('/')) continue
    const nameBytes = encoder.encode(path)
    const raw = encoder.encode(entry.content)
    const deflated = await deflateRaw(raw)
    // 压不小就别压：store 反而更小（deflate 对已压缩/极短内容会略微变大）
    const useDeflate = deflated !== null && deflated.length < raw.length
    prepared.push({
      nameBytes,
      raw,
      data: useDeflate && deflated ? deflated : raw,
      crc: crc32(raw),
      method: useDeflate ? 8 : 0,
    })
  }

  prepared.forEach((item, index) => {
    offsets.push(local.size)
    local.u32(0x04034b50) // 本地文件头签名
    local.u16(20) // 解压所需版本
    local.u16(0x0800) // 通用位：文件名是 UTF-8
    local.u16(item.method)
    local.u16(time)
    local.u16(date)
    local.u32(item.crc)
    local.u32(item.data.length)
    local.u32(item.raw.length)
    local.u16(item.nameBytes.length)
    local.u16(0) // 扩展字段长度
    local.bytes(item.nameBytes)
    local.bytes(item.data)

    central.u32(0x02014b50) // 中央目录签名
    central.u16(20) // 生成程序版本
    central.u16(20) // 解压所需版本
    central.u16(0x0800)
    central.u16(item.method)
    central.u16(time)
    central.u16(date)
    central.u32(item.crc)
    central.u32(item.data.length)
    central.u32(item.raw.length)
    central.u16(item.nameBytes.length)
    central.u16(0) // 扩展字段
    central.u16(0) // 注释
    central.u16(0) // 起始磁盘号
    central.u16(0) // 内部属性
    central.u32(0o100644 << 16) // 外部属性：普通文件 -rw-r--r--
    central.u32(offsets[index])
    central.bytes(item.nameBytes)
  })

  const out = new ByteWriter()
  out.bytes(local.toUint8Array())
  const centralOffset = out.size
  out.bytes(central.toUint8Array())
  out.u32(0x06054b50) // EOCD
  out.u16(0)
  out.u16(0)
  out.u16(prepared.length)
  out.u16(prepared.length)
  out.u32(central.size)
  out.u32(centralOffset)
  out.u16(0) // 注释长度
  return out.toUint8Array()
}

/** `=== FILE: <相对路径> ===` 这一行的识别（提示词套件的多文件分隔行）。 */
const FILE_MARKER = /^===\s*FILE:\s*(.+?)\s*===$/

/**
 * 把提示词套件按 `=== FILE: <相对路径> ===` 拆成单文件。
 *
 * 拆不出来（没有分隔行）时返回**空数组**，由调用方决定是整份当一个文件，还是报错 ——
 * 这里不猜（猜错会生成一堆名字毫无意义的文件）。
 */
export function splitPromptSuite(text: string): ZipEntry[] {
  const files: ZipEntry[] = []
  let current: { path: string; lines: string[] } | null = null
  for (const line of text.split(/\r?\n/)) {
    const matched = FILE_MARKER.exec(line.trim())
    if (matched) {
      if (current) files.push({ path: current.path, content: current.lines.join('\n').trim() })
      current = { path: matched[1], lines: [] }
      continue
    }
    if (current) current.lines.push(line)
  }
  if (current) files.push({ path: current.path, content: current.lines.join('\n').trim() })
  return files
}
