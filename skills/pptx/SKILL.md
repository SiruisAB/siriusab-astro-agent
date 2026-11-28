---
name: pptx
description: 阅读、创建或编辑 PowerPoint 文件（.pptx/.potx），包括组会报告、研究汇报、模板、图表和讲稿；用于幻灯片文件任务，不用于一般报告写作或 UI 改版。
license: Apache-2.0; 完整条款见 LICENSE.txt
metadata:
  runtime: astro-agent Windows；实际可用的 python-pptx 或 pptxgenjs；生成、渲染和校验使用既有审批工具。
  origin: anthropics/skills/skills/pptx
  source-commit: e4bb5d960d87126ff3b3a8af1a104b7db67560ba
  adaptation: astro-agent-v3
---

# 研究汇报与 PowerPoint

本文件由工作台适配 Anthropic PPTX 技能：保留文件与版式经验，移除预装环境、固定装饰和强制外部子 Agent 假设。原辅助源码、许可证与第三方通知保留。

## 工作流程

1. 根据现有信息确定听众、时长、中文/英文、模板和主要结论；只询问会影响结果的缺失信息。
2. 阅读已有 PPT 时保留页序、讲稿、图表与来源。`scripts/slide_text.py` 可辅助提取，但文本提取不代替看图。
3. 新建时先安排证据与叙事，每页有清晰主旨；页数、文字密度和图形按用途决定，不强制每页添加装饰或固定套用标题/总结页。
4. 使用实际可用的 `python-pptx` 或 `pptxgenjs`。先读 [layout-and-ooxml.md](references/layout-and-ooxml.md) 再处理模板、复杂关系或图表。
5. 写入新文件，重读页序、图表数据、图注和讲稿；若可渲染，检查每页的溢出、遮挡、字体替代与来源位置，修复实际问题后重新渲染。

## 科研与编辑质量

- 论文图表、观测数据、误差条与 DOI 必须保留来源，不重画成不准确的装饰图。
- 可编辑表格和常规图表优先保持可编辑；复杂科学图、用户要求的原图或原生图表不能准确表达的结果可以使用清晰图片。
- 中文字体需确认本机和接收方环境；图中文字、数学符号、单位和小数位不能因适配版式而改变。
- 修改模板时保留已有视觉规范、讲稿和未涉及的内容；不把科研模板强行换成通用商业风格。
- 视觉检查工具缺失时可交付已检查内容/结构的文件，但明确“视觉排版未验证”。不能假称做了独立读者或子 Agent 审查。

## 工具说明

辅助脚本只在有授权的 Code/Agent 模式按需使用。`scripts/thumbnail.py` 要给独立输出前缀，避免覆盖别的预览；模板校验的 `scripts/office/validate.py` 可带 `--original`。
这些工具不能证明 PowerPoint 一定不修复文件；Windows 的 LibreOffice/Poppler、Node 库与字体均须实际检查，不自动安装或执行 Linux 环境修复。
