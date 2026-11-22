# 天文 Agent（桌面移植版）

基于 LangChain 的天文研究助手：SIMBAD 查询、arXiv/ADS 文献检索、astropy 计算、
文件读写、shell 执行，配四档模式与三层上下文管理。

- 上游项目：https://github.com/SiruisAB/siriusab-astro-agent （单提交，无 License）
- 本目录：移植到桌面 + 修复 5 处实测缺陷后的可运行版本

## 快速开始

```bat
启动.bat                 :: 默认对话式界面（ctrl+q 退出）
启动.bat --repl          :: 旧式逐行交互
启动.bat --mode research :: 直接进科研构思模式
启动.bat --model glm     :: 指定模型档案
```

环境已建好在 `.conda\`（Python 3.12.11）。若需重建：

```bat
E:\anaconda3\python.exe -m venv .conda
.\.conda\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置（.env）

已从 `.env.example` 生成，`ADS_TOKEN` 已填入。还需你补 `API_KEY`：

| 服务商 | MODEL_NAME | BASE_URL |
|---|---|---|
| OpenAI | `gpt-4o-mini` | 不填 |
| DeepSeek | `deepseek-chat` | `https://api.deepseek.com/v1` |
| 智谱 GLM | `glm-4-plus` | `https://open.bigmodel.cn/api/paas/v4` |

运行中 `/model <档案名>` 热切换（档案用 `MODEL_NAME_<名字>` 定义）。

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

`/mode <名>`、`/model <档案>`、`/view <文件> [起始-结束]`、`/file <路径>`（附加文件给模型）、
`/tree [深度]`、`/browse`（双栏浏览器，按 t 呼出终端）、`/prompt <内容>`、`/help`、`ctrl+q` 退出。

## 相对上游的修复（均已实测验证）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| P0 | `main.py:642` `chatui.py:228` | shell 审批恢复用 `Command(resume=[{type:accept}])`，新版 middleware 要求 `{"decisions":[...]}` 且类型为 `approve/reject/edit`，**按 y 必崩** | 改为正确格式；替代命令走 `edit` + `edited_action` |
| P1 | `main.py` | `SummarizationMiddleware(model=model)` 无 trigger，新版默认 None → `_should_summarize` 恒 False，**兜底永不触发** | 换 DeepAgents 版并显式 `trigger=("fraction",0.8)` |
| P2 | `main.py` | `FilesystemMiddleware` 默认注入 `execute` shell 工具，不在审批列表 | 限定 `tools=[ls,read_file,write_file,edit_file,glob,grep]` |
| P2 | `tools.py:182` | ADS 用 **POST**，官方端点只接受 GET → **405**，ADS 检索实际不可用 | 改 `requests.get` + query params |
| P3 | `viewer.py:39` | `parse_range('abc')` 抛 ValueError，`/view x.py abc` 直接崩 | 返回 `None`，由调用方提示用法 |
| P3 | `requirements.txt` | `langchain-core`、`requests` 未直接声明（靠传递依赖） | 显式加入 |

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

### 验收测试

```bat
.conda\Scripts\python.exe tests\test_context_archive.py
.conda\Scripts\python.exe tests\test_attachments.py
.conda\Scripts\python.exe tests\test_archive.py
```

三个测试文件共 83 项断言，全部离线可跑（不依赖网络与真实 API key）：

| 文件 | 断言数 | 覆盖 |
|---|---|---|
| `test_context_archive.py` | 30 | 无损归档 7 场景 + 切点安全/轮次边界/文件名唯一性 |
| `test_attachments.py` | 29 | 文本/图片/PDF 两模式、image_url 不能传 PDF 的对照实验、错误提示 |
| `test_archive.py` | 24 | 解包分发、zip slip、zip bomb、截断、嵌套包、tar 支持、预算 |
| `test_pdf_modes.py` | 22 | PDF 三模式、模式优先级、页数上限、缺依赖提示 |
| `test_disguised_files.py` | 15 | 改名 zip/pdf/png 仍正确识别、误判防护、边界输入 |

**当前 120/120 通过。**

另有两个联网探针（需真实 key，不计入离线回归）：
`probe_openai_file_input.py`（文件输入边界）、`probe_relay_endpoints.py`（中转站端点普查）。

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

- `chat` 模式历史存在内存，**重启即丢**（另三模式走 `sessions.db` 持久）
- `browser.py` 内嵌终端直接调 PowerShell，**不走审批**，与主审批链独立
- 无 License、无测试、无 CI；上游仅 1 次提交

## 文件结构

```
astro-agent/
├── 启动.bat         启动脚本
├── main.py          四模式 + middleware 装配 + shell 审批 + 入口
├── chatui.py        Textual 对话界面（流式、折叠块、审批内联）
├── tools.py         10 个工具 + 三层上下文管理
├── viewer.py        /view /tree 渲染
├── browser.py       双栏浏览器 + 终端面板
├── requirements.txt 依赖
├── .env             配置（含 API_KEY，勿外传）
├── .conda/          Python 3.12.11 环境
├── tests/           归档 / 附件 / 压缩包 验收测试
├── sessions.db      会话记忆（自动生成，删掉即重置）
├── results/         工具结果落盘 + context_blocks.db 归档库（自动生成）
└── conversation_history/  自动摘要 offload 的历史原文（自动生成）
```
