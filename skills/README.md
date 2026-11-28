# 项目技能目录

将完整技能放到 `skills/<name>/SKILL.md`，然后在工作台顶部“技能”中刷新、查看并启用。
不要将符号链接、目录联接、密钥或隐藏配置文件放入技能包。

```text
skills/
  my-skill/
    SKILL.md
    references/
    scripts/
    assets/
```

最低格式：

```markdown
---
name: my-skill
description: 描述工作流程以及应在什么任务中使用它。
---

# 工作步骤

先读取所需资料，再使用工作台已有的工具完成任务。脚本和写操作仍需原有审批。
```

支持本地 ZIP 和固定 [Anthropic 官方仓库](https://github.com/anthropics/skills) 导入。
导入默认禁用，不会自动执行代码或安装依赖；具体步骤见项目 README 的“Agent Skills 技能”。

已采用的技能与来源校验记录见 [ADOPTION.md](ADOPTION.md)：保留 6 个工程技能，增加
Word、PDF、PPT、表格、前端设计和技术文档 6 个技能，共 12 个。主题和浏览器验收方法
合入前端技能，避免重复路由。已审阅的包版本在本机启用；在工作台“技能”中刷新后可选择。
后续导入仍默认禁用，资源变更仍需重新审阅。辅助脚本不会自动执行，Office 等依赖不视为已预装。
