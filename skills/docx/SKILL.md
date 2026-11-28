---
name: docx
description: 阅读、创建或修改 Word 文档（.docx/.dotx），处理格式、表格、批注与修订；用于 Word 文件交付，不用于一般文字润色、PDF 或表格任务。
license: Apache-2.0; 完整条款见 LICENSE.txt
metadata:
  runtime: astro-agent Windows/Python；写文件及运行脚本使用 Code/Agent 已有审批工具，依赖按实际环境检查。
  origin: anthropics/skills/skills/docx
  source-commit: e4bb5d960d87126ff3b3a8af1a104b7db67560ba
  adaptation: astro-agent-v3
---

# Word 文档

本文件由工作台适配：基于 Anthropic 的 DOCX 技能，精简入口，移除预装依赖、Linux 命令和自动安装假设。原许可证与第三方通知保留。

## 工作流程

1. 明确是阅读、创建还是修改，以及用户要求的文件、版式、修订方式和输出位置。仅阅读/审查不写文件。
2. 阅读时保留标题层级、段落、表格、脚注和图注的位置，不把提取文本当成完整的视觉版式。
3. 创建时选择实际可用的 `python-docx` 或 `docx`（Node）库；修改已有文档优先保留其样式与关系，不从纯文本重建复杂模板。
4. 输出新文件，默认不覆盖原件。涉及批注、修订或 OOXML 编辑，先读 [editing.md](references/editing.md)。
5. 重新读取输出确认内容与结构；有可用且已获授权的渲染工具时转 PDF/图片检查分页、表格和中文字体。没有渲染能力时说明“内容/结构已检查，视觉排版未验证”。

## 容易丢失的信息

- Word 会把一句话拆成多个文本 run；直接字符串替换可能漏改，整体替换段落又可能丢失格式。
- 不把字面项目符号、空格或换行用作列表、对齐和分页；使用对应的文档结构。
- 表格宽度、页边距、单元格宽度应一致；为图片写明类型和替代文本。
- 修订与批注需要关系和内容类型注册；删除段落的修订不等于删除全部文本 run。
- 科研数值、单位、误差范围、公式和来源原样核验，不因排版改变数值精度或补造引用。

## 工具边界

包内 `scripts/` 是按需使用的源码，不是自动动作。先查看实际路径、用途及影响范围，再经现有审批运行；不自动执行宏、安装库或上传文档。
`scripts/office/validate.py` 可辅助结构检查；`scripts/office/soffice.py` 含特定沙箱的处理，不能假定已在本机验证。
Windows 上先确认 LibreOffice/Poppler 是否存在，不自动编译 Linux shim、修改 Office 用户配置或把缺失工具报告成验证通过。
