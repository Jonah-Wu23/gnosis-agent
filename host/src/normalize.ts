// 组件归一化：AST 去注释 + Printer 打印 + sha256。空白、注释、行尾差异不构成有效变异。
import ts from 'typescript-legacy'
import { createHash } from 'node:crypto'

const printer = ts.createPrinter({
  removeComments: true,
  newLine: ts.NewLineKind.LineFeed,
})

// NodeArray 的 hasTrailingComma 与节点/数组的 multiLine 都是解析期的排版产物，
// 一律归一到“多行、无尾逗号”；否则换行风格与尾逗号会被误判成有效变异。
const resetLayout = (nodes: ts.NodeArray<ts.Node>): void => {
  const array = nodes as unknown as { multiLine?: boolean; hasTrailingComma?: boolean }
  if (array.multiLine !== undefined) array.multiLine = true
  if (array.hasTrailingComma !== undefined) array.hasTrailingComma = false
}

const nodesVisitor = ((
  nodes: ts.NodeArray<ts.Node> | undefined,
  visitor: ts.Visitor,
  test?: (node: ts.Node) => boolean,
  start?: number,
  count?: number,
) => {
  const result = ts.visitNodes(nodes, visitor, test, start, count)
  if (result !== undefined) resetLayout(result)
  return result
}) as typeof ts.visitNodes

export function normalizeSource(source: string, fileName = 'component.ts'): string {
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    /*setParentNodes*/ true,
    ts.ScriptKind.TS,
  )
  const transformed = ts.transform(sourceFile, [
    (context) => {
      const visit = (node: ts.Node): ts.Node => {
        const visited = ts.visitEachChild(node, visit, context, nodesVisitor) as ts.Node
        const meta = visited as unknown as { multiLine?: boolean }
        if (meta.multiLine !== undefined) meta.multiLine = true
        // pos/end 置 -1 后节点被 Printer 当作合成节点，完全按 AST 重新排版而不是复用原文
        return ts.setTextRange(visited, { pos: -1, end: -1 })
      }
      return (node: ts.Node) => ts.visitNode(node, visit) as ts.SourceFile
    },
  ])
  const printed = printer.printFile(transformed.transformed[0]!)
  transformed.dispose()
  return printed.replace(/\r\n/g, '\n')
}

export function normalizeSha256(source: string, fileName = 'component.ts'): string {
  return createHash('sha256').update(normalizeSource(source, fileName), 'utf8').digest('hex')
}
