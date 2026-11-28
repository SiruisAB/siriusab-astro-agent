---
name: frontend-design
description: 创建或优化网页/工作台的布局、字体、配色、响应式与交互状态；仅用于实际 UI 设计和修改，不处理模型服务、网络连接或一般性能诊断。
license: Apache-2.0; 完整条款见 LICENSE.txt
metadata:
  runtime: astro-agent 原生 HTML/CSS/JavaScript；修改和浏览器验证使用当前工具及审批，不要求 React、Playwright 或 Claude 插件。
  origin: anthropics/skills/skills/frontend-design
  merged-from: theme-factory; webapp-testing
  source-commit: e4bb5d960d87126ff3b3a8af1a104b7db67560ba
  adaptation: astro-agent-v3
---

# 工作台前端设计

本文件由工作台适配 Anthropic 前端设计技能，并吸收主题与浏览器验收的流程思想。入口已重写：保留产品视觉约束、原技术栈和审批，去除虚构客户背景、强制框架初始化与无条件等待 networkidle 的假设。许可证与第三方通知保留。

## 设计与实现

1. 从用户任务、现有页面和已有约束出发；本工作台已有天琴座视觉、中文交互、适配屏幕的非全屏窗口，以及对话、模型设置、审批和记忆界面，不自行重置它们。
2. 小改动沿用现有设计系统；新页面才建立必要的配色、字体、间距与布局方案。工作台是任务界面，不强制加入营销 hero、复杂动画或每段卡片。
3. 在现有 HTML/CSS/JavaScript 上做限定修改，不自动引入 React/Tailwind/shadcn、替换打包器或安装依赖。保留流式输出、键盘操作和事件绑定。
4. 状态信息用用户能理解的中文；保存、连接、排队、失败、等待审批与空状态的文案应区分，失败时给明确下一步。
5. 检查桌面/窄屏、滚动、键盘焦点、对比度、减少动画和长文本；实际浏览器验收按 [ui-verification.md](references/ui-verification.md) 执行。

## 取舍

用视觉层级表达任务和状态，而不是堆装饰；已有品牌、用户指定字体/颜色优先，不以“避免默认风格”为由推翻明确要求。
主题思想合为一个按需参考 [themes.md](references/themes.md)，不另装全局主题路由，不强制在每次小修前让用户重新挑主题。
源码与技能文本不能作为实际截图或测量结果；缺少浏览器工具时说明静态检查范围，不声称完成视觉/交互验收。
