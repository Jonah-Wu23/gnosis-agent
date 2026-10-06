// 组件静态权限扫描：组件必须自包含单文件，只允许经参数拿数据。
import ts from 'typescript-legacy'

export interface PermissionViolation {
  rule: string
  line: number
  column: number
  detail: string
}

const FORBIDDEN_GLOBALS = [
  'process',
  'globalThis',
  'fetch',
  'Deno',
  'Bun',
  'Function',
  'eval',
  'require',
  'Reflect',
  'global',
  'module',
  'exports',
  '__dirname',
  '__filename',
]

// 属性面：constructor/__proto__ 链是取得 Function 构造器的已知通道（({}).constructor.constructor），
// getPrototypeOf 等价。字面量元素访问 obj['constructor'] 同样拦截。
const BLOCKED_PROPERTIES = ['constructor', '__proto__', 'getPrototypeOf', 'setPrototypeOf', '__defineGetter__', '__lookupGetter__', 'watch', 'observe']

// 标识符只在其作为“值引用”时算逃逸面：属性名、类型位置、声明名不改动运行时能力。
function isNonReference(node: ts.Identifier): boolean {
  const parent: ts.Node | undefined = node.parent
  if (parent === undefined) return false
  if (ts.isPropertyAccessExpression(parent)) return parent.name === node
  if (ts.isPropertyAssignment(parent)) return parent.name === node
  if (ts.isQualifiedName(parent)) return parent.right === node
  if (ts.isBindingElement(parent)) return parent.propertyName === node || parent.name === node
  if (ts.isLabeledStatement(parent) || ts.isBreakStatement(parent) || ts.isContinueStatement(parent)) {
    return parent.label === node
  }
  if (
    ts.isVariableDeclaration(parent) ||
    ts.isParameter(parent) ||
    ts.isFunctionDeclaration(parent) ||
    ts.isFunctionExpression(parent) ||
    ts.isClassDeclaration(parent) ||
    ts.isClassExpression(parent) ||
    ts.isInterfaceDeclaration(parent) ||
    ts.isTypeAliasDeclaration(parent) ||
    ts.isModuleDeclaration(parent) ||
    ts.isMethodDeclaration(parent) ||
    ts.isMethodSignature(parent) ||
    ts.isPropertyDeclaration(parent) ||
    ts.isPropertySignature(parent) ||
    ts.isGetAccessorDeclaration(parent) ||
    ts.isSetAccessorDeclaration(parent) ||
    ts.isEnumDeclaration(parent) ||
    ts.isEnumMember(parent)
  ) {
    return parent.name === node
  }
  return false
}

export function scanPermissions(source: string, fileName = 'component.ts'): PermissionViolation[] {
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    /*setParentNodes*/ true,
    ts.ScriptKind.TS,
  )
  const violations: PermissionViolation[] = []

  const report = (node: ts.Node, rule: string, detail: string): void => {
    const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile))
    violations.push({ rule, line: line + 1, column: character + 1, detail })
  }

  const check = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node)) {
      report(node, 'import', '禁止 import 声明：组件必须自包含单文件')
      return
    }
    if (ts.isImportEqualsDeclaration(node)) {
      report(node, 'import', '禁止 import = require 形式：组件必须自包含单文件')
      return
    }
    if (ts.isExportDeclaration(node) && node.moduleSpecifier !== undefined) {
      report(node, 'import', '禁止 re-export 外部模块：组件必须自包含单文件')
      return
    }
    if (ts.isCallExpression(node)) {
      const callee = node.expression
      if (callee.kind === ts.SyntaxKind.ImportKeyword) {
        report(node, 'dynamic-import', '禁止动态 import()')
        return
      }
      if (ts.isIdentifier(callee)) {
        if (callee.text === 'require') {
          report(node, 'require', '禁止 require 调用')
          return
        }
        if (callee.text === 'eval') {
          report(node, 'eval', '禁止 eval 调用')
          return
        }
        if (callee.text === 'Function') {
          report(node, 'Function', '禁止 Function 构造器')
          return
        }
      }
      return
    }
    if (ts.isNewExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'Function') {
      report(node, 'Function', '禁止 Function 构造器')
      return
    }
    if (ts.isPropertyAccessExpression(node) && BLOCKED_PROPERTIES.includes(node.name.text)) {
      report(node, 'property', `禁止访问属性 ${node.name.text}（原型链/构造器逃逸面）`)
      return
    }
    if (
      ts.isElementAccessExpression(node) &&
      ts.isStringLiteral(node.argumentExpression) &&
      BLOCKED_PROPERTIES.includes(node.argumentExpression.text)
    ) {
      report(node, 'property', `禁止以字面量下标访问属性 ${node.argumentExpression.text}`)
      return
    }
    if (ts.isIdentifier(node) && FORBIDDEN_GLOBALS.includes(node.text) && !isNonReference(node)) {
      report(node, 'global', `禁止访问全局标识符 ${node.text}`)
    }
  }

  const visit = (node: ts.Node, typePosition: boolean): void => {
    const inType = typePosition || ts.isTypeNode(node)
    if (!inType) check(node)
    ts.forEachChild(node, (child) => {
      visit(child, inType)
    })
  }
  visit(sourceFile, false)

  return violations.sort((a, b) => a.line - b.line || a.column - b.column)
}
