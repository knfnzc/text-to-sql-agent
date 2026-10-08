/**
 * Markdown 渲染。
 *
 * 为什么不能直接 `v-html` 一把梭：
 *    结论文本是模型生成的。模型输出会被用户输入影响，
 *    而 v-html 会把里面的 <script> 或 onerror 属性当代码执行，
 *    等于给了一条从输入框直通页面的 XSS 通道。
 *    所以必须先渲染再净化，两步都不能省。
 *
 * 用 marked 而不是自己写解析器：表格、代码块这些语法自己实现太容易出边界问题。
 */

import DOMPurify from 'dompurify'
import { marked } from 'marked'

// 全局配置一次即可。breaks: true 让单个换行也渲染成 <br>，
// 因为模型输出常把要点一行一条地写，不开启的话它们会挤成一段
marked.setOptions({
  breaks: true,
  gfm: true,
})

/**
 * 把 Markdown 文本渲染成可以安全插入页面的 HTML。
 *
 * @param source Markdown 源文本
 * @returns 净化后的 HTML 字符串
 */
export function renderMarkdown(source: string): string {
  if (!source) return ''

  const html = marked.parse(source, { async: false }) as string

  return DOMPurify.sanitize(html, {
    // 白名单式放行：只允许文档类标签，明确排除 form / input / iframe 等
    // 可能被用来伪造界面或加载外部资源的东西
    ALLOWED_TAGS: [
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'p', 'br', 'hr', 'blockquote',
      'ul', 'ol', 'li',
      'strong', 'em', 'del', 'code', 'pre',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'a', 'span', 'div',
    ],
    ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'class'],
  })
}
