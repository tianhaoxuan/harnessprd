/**
 * 「导入 PRD + 接口文档」子视图（需求 §五.4）。
 *
 * ## 为什么它只是 `ImportPrdPanel` 的一层薄壳
 *
 * 需求给这个子视图画了两颗按钮（「进入当前环节」「直接生成提示词」）与**第二个**
 * 可选输入框（接口文档）。但按本次已确认的范围（"照现状搬：单粘贴框 + 现有两个按钮"），
 * 这两个子视图**在交互上完全一致** —— 改造前它们本来就是**同一块**粘贴区
 * （`entryMode === 'prd-shortcut'` 与 `'prompts-debug'` 只换文案），往下走也由同一个
 * `handleImportPrdAsBaseline` 按 `entryMode` 分流。
 *
 * 所以这里**不复制一份 JSX**：两个面板长得一样、只差文案，抄一份出来就是两处要同步
 * 维护的粘贴区（而这类"两份几乎相同的 UI"必然长歪）。真需要接口文档输入框时，
 * 改 `ImportPrdPanel` 一处即可，两个子视图同时生效。
 *
 * 接口文档那一份仍然可用：它取的是**当前会话里的 `apiDocsContent`**
 * （`handleImportPrdAsBaseline` → `jobGen.startPrompts(prdContent, apiDocsContent)`），
 * 所以 `import-prompts` 的说明里点明了这一点，而不是让用户找个不存在的输入框。
 */

import ImportPrdPanel, { type ImportPrdPanelProps } from './ImportPrdPanel'

export type ImportPromptsPanelProps = Omit<ImportPrdPanelProps, 'title' | 'description'>

export default function ImportPromptsPanel(props: ImportPromptsPanelProps) {
  return (
    <ImportPrdPanel
      {...props}
      title="导入 PRD + 接口文档：两份都贴进来"
      description="跳过前面的全部流程。提示词套件必须吃 PRD（后端缺 prd_content 会 422）；接口文档会取当前会话里已有的那一份，没有也能继续（后端填「（尚无）」）。"
    />
  )
}
