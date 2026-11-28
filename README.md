# 天文 Agent（桌面移植版）

基于 LangChain 的天文研究助手：SIMBAD 查询、arXiv/ADS 文献检索、astropy 计算、
文件读写、shell 执行，配四档模式与三层上下文管理。

- 上游项目：https://github.com/SiruisAB/siriusab-astro-agent （单提交，无 License）
- 本目录：移植到桌面 + 修复 5 处实测缺陷后的可运行版本

## 快速开始

```bat
启动.bat                 :: 打开本地工作台
启动.bat --port 8766     :: 使用其他工作台端口
启动.bat --no-warmup     :: 只加载界面，首次发送时再初始化引擎
启动.bat --tui           :: 打开原来的终端界面（Ctrl+Q 退出）
启动.bat --repl          :: 旧式逐行交互
启动.bat --mode research :: 直接进科研构思模式
启动.bat --model glm     :: 指定模型档案
```

环境已建好在 `.conda\`（Python 3.12.11）。若需重建：

```bat
E:\anaconda3\python.exe -m venv .conda
.\.conda\Scripts\python.exe -m pip install -r requirements.txt
```

### 工作台

启动后，左侧可以切换**任务**和 **Chat**，中间查看流式会话，右侧查看执行记录、处理命令审批或浏览工作区文件。

- **任务**提供通用 Agent、科研构思和代码工程三种模式。任务会话按模式保存在 `.workbench/`。
- **Chat**直接读取原来 `sessions.db` 中的聊天记录。可以在右侧打开工作区文件，把支持的文件附加到下一条消息。
- 默认用 Microsoft Edge 应用窗口打开：按鼠标所在屏幕的可用区域居中，宽约 88%、高约 86%，最大 1440×960 逻辑像素；避开任务栏，不自动全屏或最大化。窗口仍可手动缩放、最大化。找不到 Edge 时用系统默认浏览器，窗口大小由浏览器决定。工作台只监听 `127.0.0.1:8765`。也可以运行 `启动.bat --port 8766` 指定端口。
- 界面、会话列表先启动，任务引擎在后台加载；输入框下方会显示加载状态。天文计算库只在调用相关工具时加载，不拖慢文件、代码和普通对话任务。
- 重复双击启动脚本会复用同一项目的已运行服务，不再初始化一套引擎；不会关闭其他进程。修改代码或 `.env` 后，请先在原启动窗口按 Ctrl+C 停止服务，再重新启动。
- 拖动分栏线调整宽度，双击还原默认宽度。打开文件会显示可放大的代码高亮预览，窄窗口下使用覆盖面板。
- 工作台使用天琴座星空照片作为侧栏、欢迎页和浏览器图标，操作按钮与文件类型使用统一的 SVG 图标。照片来源及授权见 [图像说明](workbench_ui/assets/CREDITS.md)。
- 输入框下显示最近一次模型调用的平均输出速率、输入 token 和缓存读取。可在 `.env` 设置 `CONTEXT_WINDOW` 或 `CONTEXT_WINDOW_<档案名>` 来显示上下文占比。如果兼容端点拒绝流式用量参数，可为对应档案设置 `STREAM_USAGE=0` 或 `STREAM_USAGE_<档案名>=0`。

### 停止与中途补充

- 生成期间输入框旁显示 **停止**，会请求关闭模型流并停止后续步骤；已开始的工具需安全退出，
  不强制杀进程、不回滚文件、不撤回已经发出的网页内容。界面区分“停止中”和“已停止”，
  不会在旧任务尚未退出时并发重启。已收到的部分回答按未完成内容保留，Qwen 的请求锁仍由实际 HTTP 流退出释放。
- 生成期间直接输入并点击 **补充指令**（或 Enter），消息会显示“提交中 → 已接收 → 已进入任务上下文”。
  补充在下一次安全模型步骤进入当前任务，不修改已经发出的模型请求。
  尚未执行的旧工具计划会取消；旧审批会按拒绝处理，不把插话当作授权。
  原问题的选项没有被自动选择，缺少必要信息时 Agent 应重新提问。每轮最多 20 条、合计 48000 字符。
- **停止并补充** 会先请求停止，收到后台停止确认后，用输入框内容作为同一会话的下一轮继续。
  未确认停止或任务仍需处理时不会自动重发，补充留在输入框；普通“停止”不会自动开始新一轮。
  不能即时打断已开始的远程操作或保证服务端立刻停止计算。
- 中止时尚未应用的补充会标注并保留；断线后的未决审批仍可按原流程恢复，显式停止则取消本轮，不自动恢复旧工具。

### 模型与端口设置

点击左侧或右上角的“设置”，可以添加、编辑和移除模型档案，填写显示名称、模型 ID、
API URL、API Key、接口类型，以及可选的上下文窗口和流式用量统计开关。URL 通常以 `/v1` 结尾；
留空使用 OpenAI 官方地址。本地免鉴权服务可勾选“无需 Key”。首次启动工作台不必先创建 `.env`。
自定义模型建议填写服务端的实际上下文上限；没有可用上限时，任务引擎按消息条数触发自动压缩，仍可使用 `/compact`。

接口可选择 **Chat Completions**（`/chat/completions`）、**Responses**（`/responses`）或**自动识别**。
可以粘贴基础 URL 或完整接口 URL；设置中会预览最终请求地址，程序会移除已填写的接口后缀，避免重复拼接。
自动识别依据完整 URL 的后缀，没有后缀时使用 Chat；不会联网探测或失败后自动切换协议。
请按服务商实际支持的接口选择。Responses 支持流式回复、工具调用和任务恢复，默认不启用服务端响应存储。

- 模型配置保存后用于下一次请求，不需要重启工作台。待恢复任务的原模型 ID、URL 和实际接口协议不允许被更改或移除，可更新 Key。
- 启动端口保存后，需要停止旧服务并重新启动才生效；`启动.bat --port 8766` 可以临时覆盖保存的端口。
- Key 不会回显；留空保留原值，新模型可沿用默认档案的 Key。关闭设置会丢弃尚未保存的修改。
- 设置覆盖原有 `.env` 档案，但不会修改 `.env`。配置保存在被 Git 忽略的 `.workbench/settings.json`；Key 是本地明文存储，请勿共享该文件。

### 启动性能

本机实测服务及会话列表就绪约 4.19 → 0.71 秒，浏览器恢复测试会话约 1.1 秒；
任务引擎继续在后台初始化，约 3.3 秒就绪，立即发送消息时会等待初始化完成。

### 本地 Qwen coder390 接入

模型 ID 为 `qwen3.8-27b-coder390` 时，自动启用专用适配；URL 可填
`http://100.106.113.79:8088/v1`，接口选择 Chat Completions，本地免鉴权可勾选“无需 Key”。
其他模型的调用行为不变。本项目不会启动、停止或切换外部推理服务。

- **模型请求并发固定为 1**：主 Agent、科研子 Agent、Chat、草稿和摘要共用服务地址的请求名额；
  同一台机器上本项目的多个进程也共用文件锁。只锁住模型 HTTP 请求，收到完成响应并关闭流后释放，
  不占用名额等待工具或人工审批。不能替其他程序或其他机器上的客户端限制并发。
- 即使内部调用 `invoke`，线上也使用流式响应；不自动重试。排队最多 180 秒、首 token 最多 120 秒、
  token 空闲最多 20 秒、包含排队的单请求总时限 180 秒；科研子 Agent 单请求总时限 20 秒。
  取消或超时会关闭响应流，直到读取线程实际退出才释放名额；服务端是否立即停止计算仍由服务端决定。
- 默认 `temperature=0`、`reasoning_effort=none`、`max_tokens=1024`，输出硬上限 8192。
  输入预算包含消息、工具定义和格式约束，按 UTF-8 字节及模板余量保守估算，再钳制输出；
  这不是精确 tokenizer 计数，可能提前要求 `/compact`。超过预算不自动删原文。
- 输入框旁显示“无思考 · 并发 1”，可选择“深度复核 · 本轮”（`xhigh`）；发送后恢复无思考。
  `/compact` 固定无思考；待审批任务恢复时保留原请求强度。程序调用也可用
  `model.bind(reasoning_effort="xhigh")` 单请求升档。
- 有工具时固定 `tool_choice="auto"`，不要求并行工具；无工具时不传工具选择参数。
  完整校验结构化工具名和参数后才交给现有工具及审批流程，不会从正文解析命令并执行。
  请求只保留开头一条 `system`：安全规则与开头的系统/开发者提示合并，历史中途的系统说明降为原位置的上下文备注。
  每 12 个 assistant 回合在完整工具结果后用普通上下文提醒重锚，避免严格 Jinja 模板的
  `System message must be at the beginning` 错误；原始历史不改写，缓存命中率不作保证。
- 仅在无思考续答中确认孤立 `</think>` 后为空或重复首段时清理显示；普通标签文本和 `xhigh` 不截断。
  原始响应（包括失败请求的部分内容）单独归档，已知密钥脱敏；界面正文不包含被清除的重复尾段。
- Qwen 的 Chat 默认单阶段，使用累积会话历史；显式设置 `CHAT_TWO_STAGE=1` 可保留原两阶段模式。
  该服务按文本接口接入，附件使用文本或 `PDF_MODE=text`，不发送图片块。

### Agent Skills 技能

点击工作台顶部的“技能”，可导入、审阅、启用和选择技能。兼容
[Agent Skills 格式](https://agentskills.io/specification)；参考
[Anthropic 官方技能仓库](https://github.com/anthropics/skills)，无需 Anthropic 账号或更换模型。

- 技能放在本项目 `skills/<name>/SKILL.md`，支持 `scripts/`、`references/`、`assets/` 和其他包内资源。
  `SKILL.md` 必须包含 YAML `name`、`description`；目录名须匹配 `name`。
  支持 `license`、`compatibility`、字符串 `metadata` 和 `allowed-tools`，后者仅展示，不作为免审批授权。
- 可读取固定 Anthropic 仓库的目录并导入所选技能，也可上传本地 ZIP。
  多技能仓库 ZIP 需填写技能名，例如 `pdf`；直接包含 `SKILL.md` 的单技能 ZIP 可自动识别名称。
  CLI：`.\.conda\Scripts\python.exe scripts/import_skill.py --official pdf`，或
  `.\.conda\Scripts\python.exe scripts/import_skill.py --zip D:\downloads\skills.zip --name pdf`。
- 导入保留包内文件和许可证/NOTICE，默认禁用，不执行脚本、安装依赖或覆盖同名目录。
  官方下载先使用 ZIP，失败时尝试无钩子/过滤器的 Git 稀疏下载；网络受限可用本地 ZIP。
  ZIP 最大 32 MiB，每个技能最多 512 个文件、16 MiB；`SKILL.md` 最大 64 KiB，过长会拒绝而非截断。
- 审阅完整说明、来源、许可和环境要求后，启用当前包的 SHA256 版本。
  任意资源变更都会使新加载失效，需要重新审阅。项目状态保存在 `.workbench/skills.db`，
  官方来源记录下载包 SHA256，Git 路径还记录实际提交；不把可变 `main` 分支当作固定提交。
- 启用后，模型先看到名称/用途目录，通过 `list_skills`、`load_skill` 和 `read_skill_resource`
  按需读取完整说明、引用和脚本源码。可勾选最多 3 个技能显式用于下一条消息，发送后清除选择；
  正文注入本轮模型输入，不替换系统安全规则，也不写成用户聊天记录。所有模式共享这一能力。
- 每轮最多加载 6 个技能；资源长文本分页读取，二进制只返回路径和校验信息。
  人工审批恢复沿用本轮已加载说明的快照，不自动替换为新版；资源变更或禁用后会拒绝新读取。
  原始聊天、已加载快照和工具原文保留，技能样例不会被当作新的科研知识证据。
- **技能不是代码沙箱，也不会创造新工具权限。** Agent/Code 可按既有文件/Shell 工具与审批运行脚本；
  Chat/Research 不会因启用技能而获得 Shell，隔离科研子 Agent 也不继承父 Agent 的技能或文件能力。
  Claude 专有插件、MCP 工具注册和环境依赖不会自动迁移；依赖缺失时需要明确说明并经审批处理。

首次使用前请重启工作台后台服务；仅关闭浏览器窗口不会刷新已加载的任务引擎。

#### 工程技能包（Addy Osmani）

已从用户提供的本地 ZIP 采用 [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)
的 6 个工程技能：上下文管理、系统调试、测试驱动开发、代码审查、官方文档核验、性能优化。
名称及来源校验记录见 [skills/ADOPTION.md](skills/ADOPTION.md)。在“技能”中刷新即可查看，
可以勾选最多 3 个用于下一条消息；未勾选时模型仍可根据任务按需加载已启用的技能。

- 基于原工作流本地化冲突条款，保留包内共享检查表和 MIT 许可证；不安装上游 hooks、插件命令、浏览器 MCP 或依赖。
- 不常驻注入所有技能正文，不叠加 `using-agent-skills` 路由或第二套记忆系统。
- 审查/诊断不因技能建议而修改源码；变异测试、二分定位、提交、发布与回滚仍受任务授权和审批约束。
  现有未提交改动必须保留；仅自部署 Qwen Coder390 的请求按服务地址保持并发 1。
  其他模型渠道没有新增全局单并发锁，仍遵守服务商限额；工作区写操作的原有串行/审批保护不取消。
- 已显式选择并提供完整正文的技能不会重复注入；`load_skill(reload=True)` 可在确有需要时重新获取。
- Windows 导入临时目录继承项目 ACL，避免导入者与工作台进程不是同一用户时，移动后的包变成不可读。

这些是模型工作指南，不是强制通过的 CI 门禁；实际安全仍由现有工具权限、审批和运行预算实施。

#### 文档与前端技能（Anthropic 本地适配）

从本地 `anthropics/skills` 保留必要资源并适配新增 `docx`、`pdf`、`pptx`、`xlsx`、
`frontend-design`，另独立编写 `technical-docs`；与原 6 个工程技能并存，共 12 个。
主题与浏览器验收方法合入前端技能，不引入 Claude 专属接口、React 初始化器或重复记忆管理。
详细取舍与本地提交记录见 [skills/ADOPTION.md](skills/ADOPTION.md)。

在“技能”中刷新后用于新消息。入口精简，格式细节按需读取；Office/PDF 的辅助脚本、
渲染和公式重算仍使用当前模式的真实工具与审批，不自动安装依赖或宣称工具已预装。
本次没有恢复已移除的测试目录；生成测试提案的运行功能仍保留。

#### 本项目的网页顾问（第一版）

这项能力属于 `astro-agent`，不需要 Codex，也不把网页 AI 设为主控。当前选择的
本地/API 模型继续运行 LangGraph；Code/Agent 可调用 `ask_web_advisor(provider, task, source_files)`
请求网页 ChatGPT/Claude/Gemini 的顾问意见（`provider` 为 `chatgpt`、`claude`、`gemini`）。
默认关闭，没有自动联网、浏览器启动或依赖安装。

使用步骤：

1. 在“设置 → 网页顾问”选择浏览器类型和代理并保存（可以先保持未启用）。
   普通模式运行项目根目录的 `启动网页顾问.bat`，打开单独的 Chrome/Edge 浏览器配置。
   此启动器仅在你主动运行时打开交互窗口，不停止已有进程；CDP 端口占用时会退出。
   指纹模式则手动启动自己的 Chromium 指纹浏览器配置，将其本机 HTTP CDP 地址填到设置中；
   启动器只展示操作提示，不擅自打开普通浏览器，也不调用具体厂商 API。
2. 在这个浏览器中手动登录 ChatGPT/Claude/Gemini、选择网页模型，并核对网站条款、额度。
3. 在工作台“设置 → 网页顾问”确认专用浏览器并启用，CDP 默认 `http://127.0.0.1:9222`，
   单次超时默认 180 秒。只允许本机回环地址；这个确认不代表后台证明了浏览器配置隔离。
4. 使用 Code/Agent 模式，例如：“请让 ChatGPT 网页顾问审查 `approval_policy.py` 的审批规则，
   仅发送该文件的源码快照，返回建议，由本工作台决定如何修改。”
5. 每次先展示目标网站、完整冻结文本、文件 SHA256，只有你批准后才发送。
   拒绝不会连接浏览器；不支持在审批中修改参数，修改任务应重新申请审批。

侧边栏实时查看与登录：

- 左侧“网页 AI · 实时查看”或右侧“网页 AI”标签页提供 **打开专用浏览器登录**、**检查连接**、
  当前会话任务、完整冻结发送文本和实时网页回复。不需要 Codex 或 MCP；当前是工作台直接 CDP 接入。
- 默认工作台端口 `8765` 显示界面，浏览器 CDP 端口 `9222` 用于本机控制；代理端口由你的代理软件决定。
  **无需额外 OAuth/登录回调端口**：在专用 Chrome/Edge 官网窗口手动登录 ChatGPT/Claude/Gemini，
  登录会话保存在这个浏览器配置中。CDP 连接成功不代表账号已登录。指纹模式由用户手动打开配置。
- “打开这次官网对话”只激活该任务已登记的标签页，不创建请求或重发问题；
  成功后的任务标签页也保留，方便查看原始官网对话。关闭标签页后需自行在专用浏览器历史中查看。
- “显示官网画面”是经用户确认、约每 2 秒刷新的 **只读镜像**，可放大；不是可交互 iframe 或内嵌浏览器。
  官网的跨域限制通常不允许直接 iframe 嵌入。输入、滚动、登录与验证码处理请在实际官网窗口进行。
  镜像只允许读取本次任务登记的官网标签页；密码/验证码输入区域或跳转到登录域名时会暂停。
  画面不保存、不归档、不发送给模型；关闭侧栏、切换会话或工作台标签页不可见时停止镜像。
  网页文本实时预览仍是未核验建议，断开时保留已收到的部分并标为结果未知，不视为完整答案。

浏览器与代理：

- **选择哪个浏览器**：启动器按 `ASTRO_WEB_BROWSER`（显式 exe 路径）→ Thorium → Chromium →
  ungoogled-chromium → Chrome → Edge 的顺序取第一个存在的。Edge 排最后，它捆绑的后台服务最多。
  想固定用某个：`setx ASTRO_WEB_BROWSER "D:\thorium\thorium.exe"`（新开窗口生效；清空即恢复默认）。
- **启动参数**：专用浏览器固定加 `--disable-extensions --disable-component-update
  --disable-background-networking`。顾问 profile 只需要登录 Cookie，扩展是纯开销：
  实测同一台机器、同一个 Gemini 页面，加载 7 个扩展时为 11 进程 / 1484 MB，
  禁扩展后为 9 进程 / 809 MB，省约 675 MB。
- **关于"轻量浏览器"**：换成 ungoogled-chromium 实测 9 进程 / 823 MB，与禁扩展的 Edge 基本持平
  （差异在波动范围内）。单个 Gemini 标签页的渲染进程就占 647 MB（约 78%），所以换内核省不下多少；
  真正有效的是禁扩展，以及**不用时就关掉顾问浏览器**。ungoogled-chromium 无自动更新，
  承载 Google 登录时需自行跟进版本。
- Chromium 系 profile 可跨浏览器产品复用（Edge 建的 profile 可直接被 ungoogled-chromium 读取，
  实测登录态保留）；但来回切换前要确保同一时刻只运行一个实例，否则 profile 会被占用。
- **跟随系统代理**：普通浏览器不添加代理覆盖参数，使用 Chromium 的系统代理配置；
  不等于自动读取工作台的 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。
- **直连**：普通启动器传入 `--no-proxy-server`，不使用浏览器代理；无法绕过操作系统 TUN/VPN 路由。
- **指定本机代理**：填写实际监听地址，例如 `http://127.0.0.1:7890` 或 `socks5://127.0.0.1:7891`，
  支持 HTTP/HTTPS/SOCKS5 回环代理，不允许账号密码或远程地址。启动器先确认端口可连接，
  再传入 `--proxy-server`；不可连接时不启动，也不自动回退直连。端口可连接不证明代理协议或网站访问成功。
- 浏览器已启动时不能靠保存工作台设置改变其代理；关闭自己的专用浏览器再重新运行启动器。
  本机 CDP 控制连接始终回环直连，不经过代理，其他模型渠道的代理配置不变。
- **指纹浏览器**：通用接入要求该独立配置提供标准 Chromium CDP `/json/version` 和浏览器 WebSocket；
  并非自动安装、伪造指纹或保证支持每款产品。代理必须在指纹浏览器配置内应用并重启，
  再勾选“我已应用代理”才能启用。工作台仅记录用户确认，不验证实际出站路径；
  改动浏览器类型、CDP 地址或代理时，界面会清除确认。不会修改指纹、导出登录凭证或绕过网站风控。

限制与安全：

- 最多 3 个显式项目 UTF-8 文本文件，单文件 32 KiB、合计 48 KiB，完整外发文本最多 64 KiB；
  不允许隐藏目录、凭证、原文档案或二进制文件。不从提问里的路径自动读文件，不使用附件上传。
- 凭证模式和已知 Key 检查只是辅助，不能保证识别所有私人数据；批准前仍须核对完整文本。
- 每个顾问任务创建新的网页标签页，并记录标签页标识和网页会话位置；不复用已有聊天、
  覆盖用户草稿、导出 Cookie、操作网页存储、绕过验证码或执行网页回答中的命令。
- 网页模型由网页设置决定，工具不声称 API 模型 ID 生效。只按网页完成标记/生成状态与文本稳定
  收取回答，不把它当作服务端输出已验证或科研事实。
- 文本可能在点击发送前被网页观察/保存；从写入输入框开始发生断开/超时就标为“结果未知”。
  保留自己的标签页（浏览器仍运行时）和队列，禁止自动重发。到“设置 → 查看当前会话的网页顾问任务”
  核实后解除队列，不会再次发送；当前工作台任务尚未结束时先完成或放弃该任务。
- 崩溃后尚在执行的记录需超过原超时加 30 秒才允许人工解除，避免误解锁仍运行的任务。
- 网页顾问按站点/CDP 会话独立排队，不改变本地 Qwen 单并发或其他 API 模型规则。
  任务和回复留在 Python 后端，不增加另一套压缩/记忆系统，网页建议不能直接作为科研证据。

浏览器配置保存在被 Git 忽略的 `.workbench/web-advisor-profile/`，其中包含真实登录信息，
请勿提交、打包或分享。咨询记录保存在 `.workbench/web_advisor.db`，按工作台模式/会话隔离。
这次实现未向真实网页账号发送内容；网页选择器、登录、实际模型和网站额度仍须本机实测。
后台已运行时需要重启本项目工作台后台加载新增工具，不需要重启 Qwen 服务。

#### 独立测试编写子 Agent

Code/Agent 中可请求“让测试子 Agent 根据指定源码编写回归测试”，对应 `delegate_tests`。
它跟随当前选择的模型渠道，拥有独立上下文，只能读取 1–6 个显式指定的源码快照，
不会继承整段历史、已选技能、任意文件权限、Shell、SSH 或递归委派能力。

返回一个新的 `tests/test_*.py` 文件提案、源码 SHA256、语法/结构检查及明确的“未执行”状态。
它不覆盖现有测试，也不运行生成代码；主 Agent 审阅来源和提案后，通过原有文件审批应用，
再通过 Shell 审批运行 pytest。应先确认 RED，再实施修复并确认 GREEN；语法通过不代表测试通过或代码安全。

每轮共用最多 3 个子任务的预算；测试子任务最多 6 次模型调用、8 次只读工具调用，
源码单文件最多 128 KiB、总计 256 KiB，提案最多 32 KiB。大文件应分拆需求或缩小范围。
完成提案保存在会话的“子任务”记录中，恢复审批不会再次生成；重放缓存时若源文件变化，返回的提案标记为过期。
此阶段只支持 pytest 文件提案，不是任意代码执行沙箱，也不自动跨模型路由。

### 人工交互与任务恢复

任务模式（agent / research / code）支持 `ask_user`：模型需要你选择服务器、数据范围或分析方案时，
会暂停并显示候选答案，也支持直接填写回答。回答会记录到历史并用于后续任务。

- 审批按操作逐项进行，可批准、修改参数后执行或填写原因拒绝；同批多项操作选好后提交全部决策。
- 文件写入和修改显示差异预览。源码修改、已有文件覆盖需要审批；新建 `results/` 中的分析产物可直接写入。
  文件在审批过程中被其他程序修改时，会重新展示差异。
- 项目内明确的单条只读命令可直接执行。复合命令、未知命令、工作区外的读取以及写入操作需要审批。
  服务器只读查看优先使用 `ssh_read(host, path, operation)`，只提供 `list`、`read`、`stat`，不会执行任意远程命令。
- 重新打开工作台会恢复上次模式和会话。有待处理任务的会话显示提示点，点击“继续处理”恢复审批或回答。
  终端界面和 REPL 可输入 `/resume` 恢复当前模式的中断。
- 检查点和任务记录保存在 `.workbench/tasks.db`，内置命令与文件工具的完成结果保存在
  `.workbench/tool_results.db`，同批其他工具中断时不会再次执行已完成的写操作。
  未保存完整结果的写操作不会自动重跑；“放弃本轮”保留历史和检查点，也不会撤销已执行的操作。

恢复针对等待审批或回答的任务。执行途中断开且没有可确认的暂停点时，应先核实实际结果，再开始新操作。
原有工具调用缺少结果的历史由 `PatchToolCallsMiddleware` 补上明确的未完成记录。
实现依据：[Deep Agents 人在回路文档](https://langchain-doc.cn/v1/python/deepagents/human-in-the-loop.html#interrupts-within-tool-calls)。
当前验证环境为 LangChain 1.5.2、LangGraph 1.2.24、Deep Agents 0.7.41；依赖中已声明条件审批和类型化中断所需版本。

## 配置（.env）

当前主档案指向**局域网推理后端**（OpenAI 兼容、无需密钥）：

| 档案 | MODEL_NAME | BASE_URL |
|---|---|---|
| `default`（当前） | `qwen3.8-27b-coder390` | `http://100.106.113.79:8088/v1` |
| `relay`（原中转站，保留） | `gpt-6-thinking` | `https://chat01.ai/v1` |

`CONTEXT_WINDOW=262144` 已随主档案设置：既是工作台上下文占比的显示上限，
也是 agent 装配的必需项——`SummarizationMiddleware` 的 fraction 触发器要求模型
携带 `profile={"max_input_tokens"}` 元数据，langchain 只对注册表里的知名模型自动
补齐，自定义模型名必须由 `build_model` 显式传入，否则四模式装配直接抛
`ValueError`（2026-11 修复）。

原三方案配置仍然有效：

| 服务商 | MODEL_NAME | BASE_URL |
|---|---|---|
| OpenAI | `gpt-4o-mini` | 不填 |
| DeepSeek | `deepseek-chat` | `https://api.deepseek.com/v1` |
| 智谱 GLM | `glm-4-plus` | `https://open.bigmodel.cn/api/paas/v4` |

运行中 `/model <档案名>` 热切换（档案用 `MODEL_NAME_<名字>` 定义）。

### 浏览器版（textual-serve）

把对话界面投到浏览器（Cherry Studio 的"壳"：窗口、鼠标点侧栏切会话）：

```bat
.\.conda\python.exe serve.py              :: http://localhost:8000
.\.conda\python.exe serve.py --port 8080 --mode research
```

每个浏览器标签是独立的界面进程，会话数据共享 sessions.db；流式渲染、
思考折叠块、shell 审批、/file 附件在浏览器里行为与终端一致。

### 会话级采样参数（/param）

```bat
/param                          :: 查看当前会话参数
/param temp 0.7                 :: temperature 0~2
/param top_p 0.9                :: 0~1
/param max_tokens 4096          :: 输出上限
/param reset                    :: 重置（恢复全局 temperature=0）
```

只对 chat 模式生效，随会话保存；agent/research/code 是工具循环，
保持 temperature=0 以保证数据与代码的确定性。

## 四种模式

| 模式 | 工具子集 | 适用 |
|---|---|---|
| `agent`（默认） | 全部 | 日常问答、查数据文献、执行命令 |
| `research` | 查询+文献+压缩（无 shell） | 讨论课题、构思论文 |
| `code` | 全部 | 写脚本、调试、改代码 |
| `chat` | 查询+文献（无 shell） | 理论问答、概念辨析 |

## 上传文件给模型（chat 模式）

chat 模式原本没有任何文件通道——它的循环不走 agent graph，只能收纯字符串提问。
现在用 `/file` 补上：

```bat
/file paper.pdf                 :: 附加 PDF（自动提取文本）
/file notes.md data.csv fig.png :: 一次附加多个
/file                           :: 查看已附加
/file -1                        :: 移除第 1 个
/file clear                     :: 全部清除
```

附加后直接提问即可，下次请求会一并发送。

支持类型：

| 类型 | 处理方式 | 说明 |
|---|---|---|
| 文本 / 源码 / CSV / JSON / LaTeX / bib | 原文拼进 HumanMessage | 单文件上限 6 万字符，一次总量 12 万，超出截断并注明 |
| 图片 png/jpg/webp/gif | 转 base64 `image_url` 多模态 block | 最多 8 张，单张 ≤12MB；模型需支持视觉 |
| PDF（默认） | pypdf 提取文本 | 需装 pypdf；扫描版需先 OCR；数值/编号是精确字符串 |
| PDF（`PDF_MODE=file`） | 作为 `file` 内容块直接发送 | 仅 OpenAI 官方等实现了 file 块的端点 |
| PDF（`PDF_MODE=image`） | 每页渲染成 PNG 走视觉通道 | **通用**：中转站也能用，能读扫描件/表格/公式 |
| 任意文件（`SANDBOX_MODE=1`） | 包成 PNG+原字节 polyglot 送进对方沙箱 | 模型用 python 自己解；不占上下文 token |
| 压缩包 zip / tar / tar.gz / tgz | 本地解包后逐个分发：文本注入、图片转多模态块 | 二进制/噪声目录/嵌套包跳过；清单告诉模型包里有什么 |
| Office docx / xlsx / pptx（独立或压缩包内） | python-docx / openpyxl / python-pptx 提取段落、表格、单元格、幻灯片文本 | 本质是 zip，按内部结构（word/ xl/ ppt/）判定，改名也能处理；缺库时提示安装 |

### 上传方向：polyglot 投递（`SANDBOX_MODE=1`）

`file` 内容块在这类转发端点上**完全不登记**（见下方 7 次实验），但图片附件会落到
对方沙箱的 `/mnt/data/`，而模型自带 `bash` + `python`。所以可以把任意文件伪装成
一张合法 PNG 投递：

```
原文件 → 合法 PNG 头 + 原字节追加在 IEND 之后 → image_url
```

PNG 解码器读到 IEND 就停，尾部数据被忽略但仍留在文件里；ZIP 的中央目录在末尾，
所以同一份字节**对图片解码器是 PNG，对 zipfile 是 ZIP**。

**端到端实测**（28404 字节的 zip，内含 `1.png` / `DOCX 文档.pdf` / `Text Document.txt`）：

```
原文件    : 28404 bytes
polyglot  : 34175 bytes（PNG 画布 + 原字节追加）
本地验证  : PIL 认定 PNG 通过 / zipfile.is_zipfile 通过
```

模型侧收到的证据——它在沙箱里实际执行了：

```python
bash -lc file /mnt/data/image_82f7cddf....jpg && python - <<'PY'
print('ZIP local header:', [i for i in range(len(b)) if b.startswith(b'PK\x03\x04', i)][:12])
print('ZIP EOCD:', b.rfind(b'PK\x05\x06'))
PY
```

**真值命中 4/4**：

| 内容 | 真值 | 结果 |
|---|---|---|
| 图内文字 | `rtwrretvcb` | 命中 |
| PDF 文本 | `zxccznmbn` | 命中 |
| txt 内容 | `213123qwe` | 命中 |
| 文件名 | `New Text Document` | 命中 |

优点：文件进沙箱后模型按需读取，**不占本地上下文 token**，适合大包。
代价：文件会离开本机，且沙箱产出物落在公开 URL（见下方安全提示）。

### 下载方向：抓取模型产物

模型在沙箱里生成的文件会以**公开 URL** 回传，形如：

```
https://files.chat01.ai/python-generations/<uuid>/out.zip
```

回答后会自动扫描这类链接并下载到 `results/artifacts/`，终端/对话界面显示：

```
[已下载 1 个产物] out.zip(7.3KB)
```

实测：模型执行 `zip -q -j out.zip out.txt out.png` 后回传的压缩包
**7525 bytes，`testzip()` 无损坏**，解出 `out.txt`（内容一致）与 `out.png`
（合法 PNG 900×240，图上文字正确）。

开关：`.env` 设 `ARTIFACT_AUTO_FETCH=0` 可关闭。

> ⚠️ **安全提示**：该链接**无需鉴权即可下载**（实测不带 Authorization 也返回 200）。
> 路径中的 UUID 是唯一屏障。任何在对话里生成的产物都暴露在公网，
> 敏感数据不要走这条路。

只抓已知产物主机（`_ARTIFACT_HOSTS`），正文里的普通链接（arXiv、示例站）不会被误抓。
文件名经过清洗，`../`、URL 编码穿越、query string 都会被剥掉。

### 通道实测矩阵（同一份 zip，7 次实验）

| # | 提交方式 | 模型 | 结果 |
|---|---|---|---|
| 1 | `file` + `application/zip` | gpt-5-6 | 静默丢弃，模型说「没有文件」 |
| 2 | `file` + `text/plain`（改名 .txt） | gpt-5-6 | 静默丢弃 |
| 3 | 内容改为纯 base64 文本 + `text/plain` | gpt-6-thinking | 静默丢弃 |
| 4 | 同上，重复一次 | gpt-6-thinking | 静默丢弃 |
| 5 | `file` × 6 种扩展名同时提交 | gpt-6-thinking | **6/6 未送达**，模型 `/files/list` 返回 `No files are present` |
| 6 | `image_url` + zip 字节冒充 PNG | gpt-6-thinking | **`Attachment preparation failed`** |
| 7 | **`image_url` + 合法 PNG/ZIP polyglot** | gpt-6-thinking | 解出全部内容，**4/4 命中** |

结论：

- `file` 块**不是**被过滤或格式白名单拦掉——连文件名都到不了，说明根本没有登记进附件系统
- 图片通道**有**真实的附件准备流水线（会解码校验），所以伪装必须真的合法
- 一旦通过校验，文件进入 `/mnt/data/`，模型的 shell 就能操作它

附带发现：该端点 `POST /files` 返回 404、`input_file` 返回 400、`code_interpreter`
声明被接受但输出无 `code_interpreter_call`——**触发代码执行靠的是图片附件进沙箱，
不是声明工具**。


### 扩展名与内容不符（改名的 zip）

zip 被改名成 `.txt` 上传很常见（打包工具、邮件网关、上传白名单都会这么干）。
程序**不信任扩展名**，按文件头魔法字节判定真实类型：

| 文件头 | 判定 |
|---|---|
| `PK\x03\x04` | zip |
| `\x1f\x8b` / `BZh` / `\xfd7zXZ` | gzip / bzip2 / xz |
| `%PDF-` | PDF |
| `\x89PNG` / `\xff\xd8\xff` / `GIF8` / `RIFF…WEBP` | 图片 |

实测 `_fake_zip.txt`（真 zip 改名）与 `_real.zip` 注入结果完全一致，
note 里会注明「按文件头识别：扩展名 .txt 与内容不符」。

**但这只解决"本地能否识别"，不解决"模型能否解压"。** 改扩展名不会给端点造出
解压器——除非端点上真有代码执行环境（本项目的这条链路没有，见上）。

### 压缩包（zip / tar.gz）

没有任何 LLM API 能直接吃 zip，所以 `/file data.zip` 会**在本地解包**，
再按内容类型分发。模型看到的是「清单 + 各文件正文 + 包内图片」：

```
===== 压缩包 _z.zip（共 10 个条目：注入 4 个文本 / 1 张图片，跳过 5）=====
  ✗ ../evil.txt（路径越界）
  ✗ .git/config（噪声目录）
  ✓ data/spec.csv（文本 36 字符）
  ✓ figs/plot.png（图片 70B）
  ✗ bin/model.pkl（二进制，跳过）
  ✗ nested.tar.gz（嵌套压缩包，不递归）
===== 清单结束 =====
```

安全与预算限制（全部有测试覆盖）：

| 防护 | 阈值 | 说明 |
|---|---|---|
| zip slip | — | `../` 与绝对路径直接拒绝，且**从不落盘**（纯内存读取） |
| zip bomb | 解压后 ≤256MB | 读任何字节**之前**用 `infolist()` 声明尺寸预检 |
| 压缩包本体 | ≤64MB | |
| 条目数 | ≤2000 | |
| 注入文件数 | ≤60 | 超出的在清单里标 `✗` |
| 单文件 | ≤20k 字符 | 超出截断并注明 |
| 总注入量 | ≤120k 字符 | 与单个附件共用预算 |
| 图片 | ≤8 张，单张 ≤12MB | 包内图片与直接上传共用上限 |

跳过的内容一律在清单里写明原因（`二进制` / `噪声目录` / `路径越界` / `嵌套压缩包`），
模型据此知道包里还有哪些东西没看到，不会误以为已经读全。

`tar` / `tar.gz` / `tgz` / `tar.bz2` / `tar.xz` 同样支持，符号链接与硬链接跳过。

### PDF 三种传输模式

```bat
:: .env
PDF_MODE=text      :: 默认：pypdf 提取文本，最省 token
PDF_MODE=file      :: 原生 file 内容块，仅 OpenAI 官方等支持
PDF_MODE=image     :: 每页渲染成 PNG 走视觉通道，通用
PDF_MODE_GLM=image :: 按模型档案单独设置
```

**为什么需要 `image` 模式**：中转站/纯转发端点往往既不支持 `file` 内容块，也不支持
`input_file`。实测某中转站（chat01.ai）结果：

| 探测 | 返回 |
|---|---|
| `GET /models` | 200（转发层正常） |
| `POST /files` | **404**（无文件存储） |
| `POST /responses` | 200（Responses 形状存在） |
| `input_file`（data URL / 裸 base64 / 带 detail，三种写法） | **全部 400** |
| `code_interpreter` 声明 | 200 但输出无 `code_interpreter_call` → **容器不存在** |
| `input_image`（base64 PNG） | **200，正确读出图中文字** ✅ |

结论：这类端点唯一的文件通道是**视觉**。所以 `PDF_MODE=image` 把 PDF 每页用
pypdfium2 渲染成 PNG，作为 `image_url` 块发送——绕过 `file`/`input_file` 限制。

实测链路（chat/completions + image_url）：

```
PDF(TOKEN: ABC789 / energy 1e40 erg) → PNG → image_url
模型回答：ABC789 / 10⁴⁰ erg   ✅
```

**选择建议**

| 场景 | 用哪个 |
|---|---|
| 正式期刊 PDF（有文字层）、想省 token | `text` |
| 扫描件、老论文、复杂表格/公式 | `image` |
| OpenAI 官方端点、想保留完整版式 | `file` |

`image` 的代价：每页约 17KB PNG → 23KB base64，token 成本明显高于提文本。
默认限 10 页（`_PDF_IMAGE_MAX_PAGES`）、长边 1600px 自动缩放。

需要 `pypdfium2`：`.conda\Scripts\python.exe -m pip install pypdfium2`

### 关于「模型支持图片，为什么不能直接传 PDF」

这是两条不同的输入通道，不能混用：

| 塞进 `image_url` 的内容 | 服务端结果 |
|---|---|
| `data:image/png;base64,...` | ✅ 合法 |
| `data:application/pdf;base64,...` | ❌ 400 —— `image_url` 只接受 `image/*` |

PDF 在 OpenAI 协议里是**独立的 `file` 块**：

```json
{"type": "file", "file": {"file_data": "data:application/pdf;base64,...", "filename": "x.pdf"}}
```

所以能不能直接传 PDF，取决于**端点是否实现 `file` 块**，跟"支不支持图片"是两件事：

```bat
:: .env
PDF_NATIVE=1              :: 全局开启原生 PDF
PDF_NATIVE_GLM=1          :: 只对 glm 档案开启
```

默认 `text` 模式的原因不是能力限制，而是**通用性**：DeepSeek 之类的纯文本端点、
以及多数 OpenAI 兼容中转，都不实现 `file` 块，开了会直接 400。程序会把这类错误
翻译成可操作提示（告诉你去关掉 `PDF_NATIVE`），不用自己猜 400 是什么意思。

天文学场景下 `text` 模式通常更合适：坐标 `RA=279.23473`、`bibcode 2019PhR...821....1P`
这类要精确引用的字符串，提取文本比让模型"看页面"保真度更高，token 也更省。
需要读表格、扫描件、复杂版式时再开 `PDF_NATIVE=1`。

两个设计取舍：

- **附件全文只进入本轮请求，不留在对话历史**。历史里只记 `[本轮附加了文件：a.pdf]`，
  否则每轮重发全文会让 token 成本随轮次线性膨胀。
- **两阶段流程里附件只在草稿阶段发送**。终答阶段拿到的是已消化附件的草稿，
  不重复塞原文和图片。

非 chat 模式提示「附件只在 chat 生效」——那些模式本来就有 `read_file` 工具可直读。

## 交互命令

输入 `/compact` 可手动压缩当前会话上下文；也可用 `/compact 保留服务器路径和待办事项` 指定保留重点。
任务与 Chat 都支持。模型会摘要早期历史，保留最近两轮完整对话；成功后显示压缩前后的字符数。
完整原始对话不会被删除，工作台历史仍可查看；摘要会保存并用于后续请求与重启后的会话。
历史不足三轮、摘要没有缩短上下文或压缩失败时，原上下文保持不变。

### 会话记忆与摘要版本（第一阶段）

重新启动工作台，打开已有会话后，点击顶部的“记忆”：

- 查看 `/compact` 生成的所有摘要版本、父版本差异，以及所覆盖的原文消息。
- 修改摘要后点击“保存修订并启用”，创建新版本，旧摘要不被覆盖。
- “恢复所选版本”只切换下一轮使用的摘要；版本之后的消息仍完整进入上下文，不会删掉后续历史。
- “使用完整上下文”停用摘要，但保留所有版本；长对话可能超出模型上限，可再次 `/compact`。
- 查看完整工具结果和模型请求档案，包括 Chat 的中间工具调用；它们不会被添加为额外的可见聊天轮次。

原文使用稳定消息 ID 和修订号，摘要保存覆盖范围、SHA-256 来源校验、模型、时间与父版本。
修改原文只追加修订；受影响的旧摘要仍可查看，但不能恢复为当前上下文。并发压缩、编辑或回退
会校验历史与当前版本，冲突时保留原文并要求刷新。审批或提问尚未完成时，只能查看记忆，不能切换摘要。

数据仍保存在对应会话 SQLite 库：Chat 为 `sessions.db`，工作台任务为 `.workbench/{mode}.db`。
原文、工具档案和摘要均由 Python 后端维护；`chat_context` 仅是兼容缓存，不再是唯一摘要记录。
旧库首次打开时迁移已有消息和当时仍保存的摘要；以前已经覆盖掉的摘要无法追溯重建。
旧线程归档在该任务会话首次运行时按精确会话范围导入，旧归档库不会被修改。
`search_context()` 可跨同一会话的不同运行检索；`decompress()` 支持块 ID 和 `m消息ID@修订号` 来源引用。
每次运行仍使用独立 checkpoint 线程，已有任务恢复与执行去重不变。

删除可见会话不会清除原文档案或版本，且被删除会话不再通过工作台 API 访问；这不是隐私数据擦除操作。
档案可能包含附件原文和完整工具输出，请备份并保护这些本地数据库。此前未保存的中间请求不能补回。
终端 Chat 的 `/compact` 也保存版本，可从工作台 Chat 会话查看；终端任务模式仍使用原来的独立 checkpoint，
工作台“恢复摘要”不会恢复或改写终端任务 checkpoint。

### 本地检索、科研知识与分层提示词（第二阶段）

在“记忆”面板展开“历史检索与科研知识”：

1. 搜索 `M31`、`快速射电暴`、`1e40 erg` 或 DOI，点击“查看原文”核对完整内容。
2. 点击“用作知识来源”，填写标题和触发词；原文摘录必须逐字存在于来源中，解释和推断写入备注。
3. 保存后，提问命中天体名称、别名或科研主题时，会按优先级注入相关摘录；可编辑或停用条目。
4. 默认只用于来源会话。“项目共享”需要明确勾选，其他会话只能获得该条目摘录，不会自动获得来源会话完整历史。
5. 原文修订、删除会话或来源校验失败后，对应条目不再自动注入。“重建索引”只重建派生检索数据，不改动原文和摘要。

历史检索使用本地 SQLite FTS5：中文短词、英文词/天体标识和多词查询，结合字面命中排序。
结果带消息/块引用和 SHA-256 校验，每次返回前重新对照原文；默认不召回已经过时的消息修订，
手动勾选“包含历史修订和旧归档”可查看旧版本。索引在首次检索时增量创建，不进入启动加载路径；
不支持 FTS5 的 SQLite 会退回字面搜索。重复工具快照、系统提示词、图片/base64 和派生记忆输出不作为独立知识重新索引。

知识条目及修订位于 `.workbench/knowledge.db`。提示词保留稳定的规则/工具前缀，将带来源的检索资料
放在当前用户问题之前的数据层，不混入可见用户历史。工作台 Chat、任务模式和终端 Chat 支持此流程；
默认注入最多 6,000 字符，配置了上下文上限时进一步收紧预算。执行记录会显示采用的来源；待审批任务保持
初次注入的资料，恢复时不重新检索，存在待处理任务时禁止修改知识条目。

Chat 可用只读 `read_memory_source()` 核验当前会话原文，任务可用 `decompress()`；来源支持
`m消息ID@修订号` 与 `b原文块ID#消息序号`。原文一致不是科学结论已核实，尤其历史 AI 回答仍需核验。
本阶段是**全文和字面检索**，不是向量语义检索；别名需要通过知识条目明确填写，不会自动生成未经核验的天文事实。

### 受限科研子 Agent（第三阶段）

主 Agent 和 Chat 新增两种委派工具，可用自然语言请求：

- “让文献子 Agent 检索 FRB 宿主星系相关工作，给出实际文献标识与尚未解决的问题。”
- “让核验子 Agent 检查这两条说法，并以指定的 `m消息ID@修订号` / `b块ID#消息序号` 为证据。”

`delegate_literature(task)` 只开放 arXiv / NASA ADS 只读检索；`verify_evidence(claims, references)`
只允许读取**本次明确传入的原文引用**，另可查询公开文献和 SIMBAD 天体坐标。每次最多 8 条说法、8 个引用。
ADS 使用服务端已有的 `ADS_TOKEN`；没有配置时明确提示并可改查 arXiv，不会把 token 交给模型。
外部查询仅使用固定 HTTPS 服务，不接收自定义 URL；重定向不能切换到其他主机或非标准端口。

子 Agent 使用独立的有限工具循环：不复制父对话、不继承文件或 Shell 工具、不挂载默认 DeepAgents 文件中间件，
也不能再委派或读取其他会话。只有明确传入的任务和证据可见。能力隔离发生在工具/上下文层，**不是操作系统沙箱**；
后台仍会保存任务元数据和原文档案，但子 Agent 没有任意文件写入工具。

每轮主任务最多 3 个子任务；每个子任务最多 4 次模型调用、6 次工具尝试，模型输出上限 1,600 token，
小上下文模型会收紧输出和保守的输入预算。运行有 90 秒软预算，单次模型请求/文献 HTTP 请求限时 20 秒；
软预算不强行终止已进行的只读请求，但到期后不会继续调用。

子任务返回带真实来源的辅助报告：结构化来源里的文献 ID/链接只能来自实际查询结果，证据摘录必须逐字匹配原文；
伪造引用、改写数值或没有证据的肯定判断会被拒绝或标记为不足。历史 AI 回答不能作为独立确认。
检索和核验范围是**标题、摘要、元数据及指定原文的一致性**，不表示已读论文全文，也不构成独立科学证明。
模型综述始终标注未独立核实，报告不会自动转换为知识库事实；派生报告也不会重新作为独立原文索引。

在“记忆 → 子 Agent 执行记录”查看子任务、来源、审查结果、允许工具和预算；右侧执行记录会显示进度。
子 Agent 的内部输出不进入主聊天流。控制记录保存在 `.workbench/subagents.db`，取得的资料与工具轨迹进入
对应会话的原文档案，已知服务密钥会被隐藏。已完成的同一委派调用在审批中断/重启恢复时复用结果，不重复查询；
未保存完整结果的委派不会自动重跑，可创建新的任务。终端任务没有工作台档案绑定时只能查询公开资料，不能读取档案引用。

后续阶段：可选向量检索、可选 SillyTavern 接口。
当前没有引入 SillyTavern 代码或外部向量服务，也没有开放新的远程聊天接口。

`/mode <名>`、`/model <档案>`、`/view <文件> [起始-结束]`、`/file <路径>`（附加文件给模型）、
`/unzip <zip路径>`（落盘解压到 results/unzipped/）、
`/session`（chat 多会话）、`/resume`（恢复任务中断）、
`/tree [深度]`、`/browse`（双栏浏览器，按 t 呼出终端）、`/prompt <内容>`、`/help`、`ctrl+q` 退出。

## chat 多会话（Cherry Studio / LM Studio 式）

对话界面左侧有会话栏（按 `s` 收起/展开，点击条目切换）；每条会话独立存储在
sessions.db，**重启程序历史不丢**，标题取首句提问（可用 `/session rename` 改）：

```
/session                会话列表（当前会话标 ▶）
/session new [标题]     新建并切换（侧栏快捷键 n）
/session <编号>         切换（点侧栏条目等价）
/session rename <标题>  重命名当前会话
/session del <编号>     删除（删当前会话会自动切到最近的）
```

切回旧会话时完整历史重新渲染；思考过程折叠块不混入可见聊天历史，新增模型请求和工具原文可在工作台“记忆”中查看。
侧栏条目里 `/` 开头的编号即 `/session <编号>` 的编号，最近更新的会话排最前。

## 相对上游的修复（均已实测验证）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| P0 | `main.py:642` `chatui.py:228` | shell 审批恢复用 `Command(resume=[{type:accept}])`，新版 middleware 要求 `{"decisions":[...]}` 且类型为 `approve/reject/edit`，**按 y 必崩** | 改为正确格式；替代命令走 `edit` + `edited_action` |
| P1 | `main.py` | `SummarizationMiddleware(model=model)` 无 trigger，新版默认 None → `_should_summarize` 恒 False，**兜底永不触发** | 换 DeepAgents 版并显式 `trigger=("fraction",0.8)` |
| P2 | `main.py` | `FilesystemMiddleware` 默认注入 `execute` shell 工具，不在审批列表 | 限定 `tools=[ls,read_file,write_file,edit_file,glob,grep]` |
| P2 | `tools.py:182` | ADS 用 **POST**，官方端点只接受 GET → **405**，ADS 检索实际不可用 | 改 `requests.get` + query params |
| P3 | `viewer.py:39` | `parse_range('abc')` 抛 ValueError，`/view x.py abc` 直接崩 | 返回 `None`，由调用方提示用法 |
| P3 | `requirements.txt` | `langchain-core`、`requests` 未直接声明（靠传递依赖） | 显式加入 |
| P4 | `main.py` `build_model` | 自定义模型名（如局域网后端）无内置 profile 元数据，`SummarizationMiddleware(trigger=("fraction",…))` 装配即抛 `ValueError` | `CONTEXT_WINDOW`（及 `CONTEXT_WINDOW_<档案>`）存在时传 `profile={"max_input_tokens"}` |
| P4 | `workbench_ui/` | 提交时 14 处中文字符串乱码为 `?`（aria-label、用量面板标签、预览按钮） | 全部还原（字符数与界面用语逐一吻合） |
| P4 | 环境 | `aiohttp` 未安装 → 默认入口 `启动.bat`→`workspace.py` 导入即崩；`pytest` 未装 → 2 个测试文件无法运行 | 已安装并补入验收说明 |
| P4 | `启动.bat` | 文件是 LF（Unix）换行 + UTF-8，cmd 按字节回读错位，双击后整段脚本碎裂报错一闪而过 | 以 CRLF 重写（逻辑不变，加 `setlocal`），并加 `.gitattributes` 锁定 `*.bat eol=crlf`。另注意 `main.py` 无 `--help` 参数，未知参数会直接进 TUI |

## 上下文压缩：无损归档改造

原实现有 5 个会导致**原始信息真实丢失**的问题，已全部重做：

| 原问题 | 后果 | 现方案 |
|---|---|---|
| `str(m.content)[:3000]` | 超 3000 字符的长回答恢复时缺 70% | `messages_to_dict` 原样序列化，零截断 |
| 只存 `type` + `content` | 工具调用参数、`tool_call_id`、元数据全丢 | 标准消息对象完整往返 |
| 全局 `context_blocks.json` | agent/research 互相检索到对方压缩历史 | SQLite 按 `thread_id` 隔离 |
| `msgs[:-8]` | 可能拆开 AI 工具调用与其结果 | 按**用户轮次**保护 + 切点回退对齐 |
| `decompress()` 返回拼接文本 | 无法按原结构恢复 | 输出带角色/条数/字符数，含工具调用明细 |

新架构三层：

```
完整历史 ──无损归档──► results/context_blocks.db
                       原始消息 JSON + SHA-256 校验 + thread_id 隔离
         ──有损摘要──► 结构化摘要进入上下文（几百 token）
         ──按需恢复──► decompress(block_id) 取全文 / search_context 免解压检索
```

关键保障：

- **归档失败绝不删消息**：先归档成功才 `RemoveMessage`；失败则原样报错、历史不动
- **只归档「能删掉的」消息**：无 id 的消息跳过，避免摘要与原文在上下文里重复
- **写入即校验**：序列化往返校验 + SHA-256，篡改或损坏会明确报错而非静默使用
- **自动摘要层同样落盘**：`SummarizationMiddleware` 换成 DeepAgents 版并传入 backend，被驱逐历史原文写到 `conversation_history/<session>.md`，摘要里写明路径，模型可 `read_file` 取回
- **工具结果文件名加 uuid**：原先秒级时间戳，同一秒两次同名查询会互相覆盖

### 验收记录

测试脚本、联网探针、测试缓存和界面验收产物已从项目中移除。运行代码、
模型配置、会话数据和生成测试提案的子 Agent 功能保留。

## 已验证可用的功能

- SIMBAD 哈佛镜像：M31 → `M  31`, RA 0:42:44.3
- 角距：Vega–M31 = 66.526°
- 地平坐标：Vega @ 北京 → 高度角 82.1°
- arXiv 检索 + 结果落盘 `results/`
- ADS 检索：FRB → `2019PhR...821....1P`（修复后）
- Git Bash 定位：`C:\Program Files\Git\bin\bash.exe`
- TUI 启动默认 push BrowserScreen
- 四模式 agent 全部装配成功，17 个工具（execute 已移除）

## 已知限制

- `browser.py` 内嵌终端直接调 PowerShell，**不走审批**，与主审批链独立
- 无 License、无测试、无 CI；上游仅 1 次提交

## 文件结构

```
astro-agent/
├── 启动.bat         启动脚本
├── main.py          四模式 + middleware 装配 + shell 审批 + 入口
├── workspace.py     本地工作台服务
├── web_advisor.py   冻结文本审批、网页顾问任务与防重发队列
├── web_browser.py   本机 CDP 文本咨询适配器（不自动启动浏览器）
├── web_browser_controls.py 用户主动登录入口、任务官网标签页与只读实时镜像
├── 启动网页顾问.bat 专用登录浏览器的手动启动入口
├── workbench_ui/    工作台界面
├── chatui.py        Textual 对话界面（流式、折叠块、审批内联、多会话侧栏）
├── chatstore.py     多会话存储与原文修订同步
├── memory_store.py  原文档案、摘要版本、来源校验和回退
├── memory_retrieval.py 可重建 FTS5 索引与原文检索
├── knowledge_store.py 科研知识条目、触发词和来源校验
├── prompt_layers.py 固定规则与有预算的检索资料层
├── research_subagents.py 隔离的文献/证据核验子 Agent
├── research_queries.py 子 Agent 专用只读公共查询适配器
├── subagent_store.py 子任务记录与完成结果复用
├── conversation_archive.py 模型请求与完整工具结果归档中间件
├── tools.py         10 个工具 + 三层上下文管理
├── viewer.py        /view /tree 渲染
├── browser.py       双栏浏览器 + 终端面板
├── requirements.txt 依赖
├── .env             配置（含 API_KEY，勿外传）
├── .conda/          Python 3.12.11 环境
├── sessions.db      会话记忆（自动生成，删掉即重置）
├── results/         工具结果落盘 + context_blocks.db 归档库（自动生成）
└── conversation_history/  自动摘要 offload 的历史原文（自动生成）
```
