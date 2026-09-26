# 提交前自检：在 `git commit` 之前跑一遍，回答一个问题 ——
# **我这次要提交的东西里，有没有不该提交的？**
#
# 用法（在仓库根目录）：
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\precommit-check.ps1
#
# ⚠️ 这个文件必须存成 **UTF-8 带 BOM**。Windows PowerShell 5.1 读不带 BOM 的 .ps1 时
# 会按系统 ANSI（中文机器上是 GBK）解码，中文注释与字符串会变成乱码并**直接语法报错**
# （本轮实测踩过：`'暂存区是空的：先 git add <文件>'` 被解码坏之后，`<` 被当成保留运算符）。
# 改这个文件时请用能保留 BOM 的编辑器，别让它退化成无 BOM。
#
# 它做三件事，都不联网、不花钱、秒级返回：
#   1. 列出**已暂存**的文件（git add 过、即将进提交的那些）
#   2. 按路径揪出不该进仓库的东西：环境文件、私钥证书、依赖目录、构建产物、压缩包、超大文件
#   3. 直接扫**暂存区内容**（不是工作区）找密钥：API Key、GitHub token、私钥头、明文口令
#
# ⚠️ 只打印「文件:行号」，**绝不回显命中的内容** —— 否则这份自检自己就把密钥打进了终端日志。
# ⚠️ 它扫的是暂存区，所以**必须先 `git add`**。跳过暂存直接 commit 的写法不覆盖。
#
# 退出码：0 = 没发现问题；1 = 有可疑项，此时**不要提交**。

$ErrorActionPreference = 'Stop'

# 中文路径默认会被 git 输出成 "\346\250\241..." 这种转义形式，读起来没法核对
$gitArgs = @('-c', 'core.quotepath=false')

# ---------------- 1. 已暂存的文件 ----------------
$staged = @(& git @gitArgs diff --cached --name-only --diff-filter=ACMR)
if ($LASTEXITCODE -ne 0) {
    Write-Host '不在 git 仓库里，或者 git 不可用。' -ForegroundColor Red
    exit 1
}
if ($staged.Count -eq 0) {
    Write-Host '暂存区是空的：先 `git add <文件>` 再跑这个脚本。' -ForegroundColor Yellow
    exit 0
}

Write-Host "本次将提交 $($staged.Count) 个文件：" -ForegroundColor Cyan
$staged | ForEach-Object { Write-Host "  $_" }

$problems = New-Object System.Collections.Generic.List[string]

# ---------------- 2. 路径检查 ----------------
# 每一条都是"曾经真的漏过"或"一漏就是事故"的位置。
# `.env.example` 是模板，必须能提交，所以单独排除。
$pathRules = [ordered]@{
    '环境文件（.env.example 除外）' = '(^|/)\.env$|(^|/)\.env\.|(^|/)[^/]*\.env$'
    '私钥与证书'                    = '\.(pem|key|p12|pfx)$|(^|/)id_(rsa|ed25519)'
    'Basic Auth 口令文件'           = 'htpasswd'
    '依赖 / 虚拟环境'               = '(^|/)(node_modules|\.venv|venv|\.pnpm-store|\.uv-cache)/'
    '构建产物与缓存'                = '(^|/)(dist|__pycache__|\.pytest_cache|\.ruff_cache)/'
    '压缩包（本机打包上传用的临时产物）' = '\.(tar\.gz|tgz|zip|7z)$'
}

foreach ($name in $pathRules.Keys) {
    $pattern = $pathRules[$name]
    foreach ($file in $staged) {
        if ($file -eq 'backend/.env.example') { continue }   # 模板，允许
        if ($file -match $pattern) { $problems.Add("路径可疑 [$name]：$file") }
    }
}

# 大文件：GitHub 单文件 100 MB 硬上限，超 2 MB 就该问一句"它该进仓库吗"
foreach ($file in $staged) {
    $item = Get-Item -LiteralPath $file -ErrorAction SilentlyContinue
    if ($item -and $item.Length -gt 2MB) {
        $problems.Add(("文件偏大：{0}（{1:N1} MB）" -f $file, ($item.Length / 1MB)))
    }
}

# ---------------- 3. 暂存区内容里的密钥 ----------------
# `--cached` = 扫已暂存的内容；`-I` = 跳过二进制；`-n` = 带行号。
$secretPatterns = @(
    'sk-[A-Za-z0-9_-]{16,}'                     # OpenAI / DeepSeek 风格的 Key
    'ghp_[A-Za-z0-9]{20,}'                      # GitHub 个人令牌（旧格式）
    'github_pat_[A-Za-z0-9_]{20,}'              # GitHub 细粒度令牌
    'AKIA[0-9A-Z]{16}'                          # AWS Access Key ID
    'BEGIN [A-Z ]*PRIVATE KEY'                  # 私钥文件内容
    'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.'  # JWT（本项目接口用 JWT，别把真令牌抄进去）
)

foreach ($pattern in $secretPatterns) {
    $raw = & git @gitArgs grep --cached -I -n -E $pattern 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
    foreach ($line in @($raw)) {
        # git grep 的输出是 `路径:行号:内容`；这里**只取前两段**，内容一律丢弃
        if ($line -match '^(?<path>.*?):(?<line>\d+):') {
            $problems.Add("疑似密钥 [$pattern]：$($Matches['path']):$($Matches['line'])")
        } else {
            $problems.Add("疑似密钥 [$pattern]：$line")
        }
    }
}

# ---------------- 结论 ----------------
Write-Host ''
if ($problems.Count -eq 0) {
    Write-Host '通过：这些文件里没有发现环境文件、私钥、依赖目录或密钥。' -ForegroundColor Green
    exit 0
}

Write-Host "发现 $($problems.Count) 处可疑项，**先别提交**：" -ForegroundColor Red
$problems | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
Write-Host ''
Write-Host '处理建议：' -ForegroundColor Yellow
Write-Host '  - 该忽略的：把规则加进 .gitignore，然后 `git rm --cached <文件>`（只从版本控制移除，不删本地文件）'
Write-Host '  - 真密钥：先去平台吊销重发，再 `git rm --cached` 并提交；如果已经推上去了，历史里仍然有，必须吊销'
exit 1
