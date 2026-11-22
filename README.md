# 天文 Agent

基于 LangChain 的天文研究助手：SIMBAD 查询、arXiv/ADS 文献检索、astropy 计算、
文件读写、shell 执行，配四档模式与三层上下文管理。

## 项目结构

```
天文agent/
├── main.py           Agent 主程序（四模式 + middleware 组装 + shell 审批 + 启动入口）
├── chatui.py         对话式主界面（消息流 + 思考过程折叠收纳，LM Studio 风格）
├── tools.py          工具集（SIMBAD / 角距 / 地平坐标 / arXiv / ADS / 上下文压缩 / shell）
├── viewer.py         终端查看器（/view 渲染 md·代码·csv·json，/tree 目录树）
├── browser.py        双栏文件浏览器（/browse：左目录树右预览，Textual 实现）
├── requirements.txt  Python 依赖
├── .env.example      API 配置模板
├── sessions.db       会话持久记忆（自动生成，删掉即重置历史）
└── results/          工具结果落盘目录 + 压缩块注册表（自动生成）
```

## 快速开始（conda，Python 3.12）

deepagents 要求 Python ≥3.11，因此用 conda 建项目内环境：

```bash
# 1. 创建环境并安装依赖
conda create -p .conda -y python=3.12
./.conda/python.exe -m pip install -r requirements.txt

# 2. 配置模型
cp .env.example .env             # 然后编辑 .env，填入你的 API key

# 3. 运行（默认对话式界面；--repl 切换为旧式逐行交互）
./.conda/python.exe main.py
```

## 模型配置（.env）

所有 OpenAI 兼容接口都能用，改三个值即可：

| 服务商 | MODEL_NAME | BASE_URL |
|---|---|---|
| OpenAI | `gpt-4o-mini` | 不填 |
| DeepSeek | `deepseek-chat` | `https://api.deepseek.com/v1` |
| 智谱 GLM | `glm-4-plus` | `https://open.bigmodel.cn/api/paas/v4` |

可选：`ADS_TOKEN=...` —— NASA ADS 文献检索（查正式期刊论文用）。
到 https://ui.adsabs.harvard.edu/user/settings/token 免费申请；不配置时 arXiv 检索照常可用。

## 四种模式（运行中随时切换）

| 模式 | 行为 | 工具子集 | 适用 |
|---|---|---|---|
| `agent`（默认） | 工具循环：调用 → 看结果 → 继续思考 | 全部 10 个 | 日常问答、查数据文献、执行命令 |
| `research` | 科研构思：**查新优先**（提 idea 前先检索 arXiv/ADS 确认新颖性），五要素结构化，主动指出漏洞 | 查询+文献+压缩（无 shell） | 讨论课题、构思论文 |
| `code` | 代码工程：文件工具优先（edit_file 精确替换）、最小修改、跑测试验证 | 全部 10 个 | 写脚本、调试、改本项目代码 |
| `chat` | 两阶段深度思考后一次性输出，引用文献带 arXiv ID/bibcode | 查询+文献（无 shell） | 理论问答、概念辨析 |

每个模式是独立 agent 实例（提示词/工具/记忆各自独立）。

**对话式界面（默认）**：消息按对话流排列，回答为 Markdown 渲染；思考过程默认收纳在
可展开的折叠块里——推理模型的 `reasoning_content`、chat 模式的两阶段内部草稿、
agent 的工具调用轨迹各成一块，点击展开。命令直接输入，`/browse` 以全屏打开
（escape 返回），shell 审批在对话内完成（y/n/替代命令），`ctrl+q` 退出。

交互命令：
`/mode research`、`/mode chat`、`/mode`（查看全部）、`/model glm`（切换模型）、
`/model`（查看模型档案）、`/view tools.py 10-50`（查看文件：代码高亮/md 渲染/csv 表格）、
`/tree`（目录树）、`/browse`（双栏浏览器：左目录树右预览，t 呼出终端面板直接跑命令，
q 退回对话）、`/help`；
启动指定模式：`./.conda/python.exe main.py --mode research`（`--chat` 为简写），
启动指定模型：`--model glm`，旧式逐行交互：`--repl`。

## 多模型热切换（/model）

`.env` 支持任意多个模型档案：主档案无后缀，附加档案加后缀（档案名 = 后缀小写）：

```env
MODEL_NAME=deepseek-chat
API_KEY=sk-xxx
BASE_URL=https://api.deepseek.com/v1

MODEL_NAME_GLM=glm-4-plus          # 附加档案 glm
API_KEY_GLM=sk-yyy                 # 缺省时沿用主档案 API_KEY
BASE_URL_GLM=https://open.bigmodel.cn/api/paas/v4
```

```
/model                # 列出所有档案与当前模型
/model glm            # 热切换到 glm 档案（历史保留）
```

**切换后新模型如何知道之前的进展？** 语言模型本身无状态，"记忆"就是每次请求重发的
完整消息历史——历史存在 sessions.db（按 thread_id），与用哪个模型无关。切换只是换一个
"读历史的人"：新模型第一次被调用时收到同一段消息流（含此前模型的回答、工具调用与结果），
像读交接文档一样接上工作；框架还会注入一条 `[系统提示]` 交接消息，说明模型已切换、
请保持结论一致。

两个真实代价（已处理）：① 换模型 = 换前缀，服务端 prompt cache 全部失效，第一条消息
重新全额计费，之后随对话追加恢复命中（append-only 设计正是为此）；② 新模型上下文窗口
若更小，由 SummarizationMiddleware 自动结构化摘要兜底。

## 上下文管理（三层体系）

结合 Manus"可恢复引用"与 ACP（ranxianglei/opencode-acp）"模型自主压缩"的工程实践：

**第一层（写入时，Manus 思想）**：SIMBAD/arXiv/ADS 的结果自动落盘到 `results/`，
上下文只保留"摘要前缀 + 文件路径"——即使之后被摘要，原始数据永远可在磁盘读回。
deepagents 的 `FilesystemMiddleware` 还会对超 20K token 的工具结果自动驱逐落盘。

**第二层（模型自主，ACP 思想）**：agent/research/code 模式配了
`compress` / `decompress` / `search_context` 三工具——模型自己决定何时压缩旧历史、
写什么进摘要；压缩块存入 `results/context_blocks.json`，可恢复原文、可不解压搜索。
实现遵循 append-only 约束（通过 RemoveMessage + 追加，绝不原地改写消息），保护 prompt cache。

**第三层（兜底）**：`SummarizationMiddleware` 在接近上下文窗口时自动做结构化摘要
（会话意图/工作摘要/产物/下一步），`ModelRetryMiddleware` 处理限流重试。

**持久记忆**：会话存 `sessions.db`（SqliteSaver），重启程序后同 thread 的历史还在；
删除该文件即重置全部记忆。

## 工具清单（10 个）

| 工具 | 用途 | 审批 |
|---|---|---|
| `query_object` / `angular_separation` / `altitude_azimuth` | SIMBAD 查询、角距、观测窗口 | 否 |
| `search_arxiv` / `search_ads` | arXiv / ADS 文献检索（结果落盘） | 否 |
| `compress` / `decompress` / `search_context` | 上下文压缩 / 恢复 / 块内搜索 | 否 |
| `run_powershell` / `run_bash` | shell 执行 | **是（y/n）** |
| 文件工具（read_file/write_file/edit_file/glob…，由 FilesystemMiddleware 注入） | 读写编辑项目内文件 | 否 |

## 附加提示词（所有模式可用，可有可无）

```
/prompt 聚焦系外行星课题，优先讨论宜居性     # 设置（对当前模式生效）
/prompt                                     # 查看
/prompt off                                 # 清除
```

启动预置：`./.conda/python.exe main.py --mode research --prompt "聚焦恒星形成区"`

## Shell 工具与安全模型

`run_powershell` / `run_bash` 执行前**必经人工审批**（y / n / 输入替代命令）。
另有被动保护：60 秒超时、工作目录锁定在项目内、输出超 4000 字符截断。
文件工具被 `FilesystemBackend(virtual_mode=True)` 限制在项目目录内。

> 以后接 fermitools 等仅限 Linux 的工具：装好 WSL2 后，把 `run_bash`
> 的执行前缀改为 `wsl -e bash` 即可。

## 已知问题与处理

- **SIMBAD 主站迁移**：CDS 主站迁移中，`tools.py` 已切换到官方哈佛镜像
  `simbad.harvard.edu`（备选 `simbad.cfa.harvard.edu`、`simbad.idia.ac.za`）。
- **IERS 警告**：已切换为 astropy 内置地球自转表（角秒级精度，天文问答足够）。

## 添加新工具

在 `tools.py` 里照抄现有模式写一个函数，加进 `get_tools()` 对应子集即可：

```python
@tool
def my_tool(param: str) -> str:
    """一句话说清这个工具干什么、参数是什么（模型靠这句话决定是否调用）。"""
    ...  # 你的逻辑，返回字符串；大结果用 _save_and_reference() 落盘
```
