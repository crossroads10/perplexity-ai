# Claude Code 的 Perplexity 模型适配器

> 2026-10-30：此方案已暂停推进并保存为实验分支。后续测试确认正文截断和附件读取不稳定，当前不能作为可靠的完整 Claude Code 后端。以 [实验保存点与最新限制](BRIDGE-EXPERIMENT-STATUS.md) 为准；下文保留实现及历史验收记录。

目标是在现有 Claude Code 中使用 Perplexity Ask 作为模型后端。工具执行、权限、项目规则、Skills、MCP 和会话管理仍由 Claude Code 负责。本适配器提供协议兼容，**没有取得 Perplexity 底层模型未经加工的原生响应**。

最新本机版本为 `perplexity-ai:chinese-input-20261030`：最新用户文字和最近工具结果直接随 query 传递，按用户要求选择回复语言，附件路径增加真实用户文字核验；见[中文输入修复与验收](/Users/wy/Documents/ChatGPT/perplexity/diagnostics/chinese-input-fix/README.md)。此前加固记录见 [RELIABILITY-UPDATE.md](/Users/wy/Documents/ChatGPT/perplexity/RELIABILITY-UPDATE.md)。下方历史章节保留当时的配置和测试结果，以最新部署为准。

## 当前调用链

```text
Claude Code → Anthropic Messages → Sub2API :8080（Anthropic 透传）
           → 本地适配器 :8000/v1/messages
           → Perplexity Ask：完整客户端上下文与工具 schema
           ← 严格 JSON 工具提案或最终回答
           ← 校验后转换为原生 Messages / SSE
Claude Code 执行工具 → tool_result → 新一轮 Ask
```

这次仅切换本机 Claude 专用的供应商与分组到 Anthropic 平台；Claude Code 的 `http://127.0.0.1:8080`、密钥和默认模型不变。原 OpenAI 供应商保留。没有重编或升级 Sub2API 镜像，也没有修改 Claude Code 全局权限。

## 协议与恢复

- 新增 `/v1/messages` 和 `/v1/messages/count_tokens`。鉴权接受 `x-api-key` 或 Bearer token。
- 原生请求作为完整上下文保留：system 内容、会话中间的 system 消息、工具调用 ID、`tool_result.is_error`、`tool_reference`、`defer_loading`、工具 schema、用户后续消息。图片/PDF 的 base64 经原有附件流程上传，提示词仅保留附件引用。
- 动态接收本轮客户端提供的自定义工具，校验名称、JSON Schema、`tool_choice`、并行限制及参数。服务器不执行客户端工具。上游服务专有工具类型明确拒绝，避免静默丢失。
- Ask 可能在完整 JSON 后追加数字引用标记（如 `[10][20]`）。只忽略这种精确限定的尾缀；其他尾部文本、额外 JSON 和不完整 JSON 均拒绝。
- Ask 输出必须是完整 JSON 提案；整批校验通过才返回工具调用。不从普通自然语言中猜测并执行命令。
- 新版源码默认进行两次格式纠正；携带完整原始请求、错误类型和至多 160 字符的相关错误片段，仍受原生成期限限制。`tool_choice=any`/强制工具却返回 final 也进入纠错。无效提案和缺失上下文确认的提案不会先行下发；运行配置可能覆盖默认值。
- 原生 SSE 在开始时及等待期间每 5 秒发送 `ping`；失败发送 `error`，不会包装成空成功。启用 `visible_progress` 时，准备、排队、上传、等待模型和校验阶段以 `[桥接状态]` 文本及时发出；模型答案及工具提案仍须完整生成并校验后才发送，**不是上游逐 token 透传**。
- 有会话标识的相同完整请求，或显式携带 `Idempotency-Key` 的请求，可复用持久化响应及工具 ID。同进程并发重试合并，单个消费者断线不会取消仍有消费者的请求。
- 回执保留最多 512 条、有效期 24 小时，位于 `data/bridge_receipts.sqlite3`（权限 0600）。无会话标识且没有显式幂等键的重复提问会重新生成。该机制减少重复提案，**不保证客户端工具恰好执行一次**。
- 每轮 Ask 独立结束，工具执行与人工批准不占用 Ask 等待槽。服务重启后，客户端完整历史及已保存回执可用于续接。

原 OpenAI `/v1/chat/completions` 桥接仍保留；由于旧网关格式转换可能吞掉上游流式错误，该兼容路径继续先完成生成，再返回 HTTP 200。原生 Messages 路径发送实时心跳和错误事件。

## 长上下文的实际问题与处理

2026-10-30 的真实验收发现：完整请求已保存在本地任务库，但大 query 的模型回复称正文在第 63 条附近结束，后半段指令无法取回；正常 Claude Code 完整工具清单下还出现重复读取。这是 Ask 路径存在输入截断的证据，不是适配器主动裁剪历史。确切上游上限未确认。

现在超过 `context_file_threshold_bytes` 的模型请求转为一个完整 UTF-8 文本附件，短 query 指向该附件。**这是传输方式切换，不是长度上限或摘要压缩**：角色、指令、工具 schema、全部消息仍保留。原生工具 schema 不再在提示词中重复一遍。

每份上下文附件的末尾含随机确认值。新版源码默认另在开头、中间消息、末尾及每个 tool_result 后插入随机确认点，要求 `context_ack` 和 `context_checks` 全部正确才接受提案。原始消息/工具结果逐字保留，标记只插入在消息或块之间，不拆开代码字符串。附件工具输出带随机边界并声明为不可信数据。`context_checkpoints: false` 可回到旧版单一尾部确认。

这些检查只能检测未返回确认点的情况，不能证明模型理解了全部中间内容，也不能证明相邻文本没有被检索跳过。附件解析/检索和模型上下文依然受 Perplexity 控制；不要把 Claude Code 显示的 1M 窗口视为已验证的 Ask 容量。整篇代码复核、重要跨文件修改仍应使用能保留原始上下文的后端；本地 Edit/Write 预检是另一道独立检查。

没有新增业务层字符数或消息数拒绝上限。原有 32 MiB HTTP 请求体、附件资源限制、账号上传额度和生成期限继续生效。长请求每轮需要上传上下文附件，会增加延迟及上传用量。同步与异步上传签名 API 已补充账号选择头；该头不会传给外部对象存储。

## 本机配置

`data/config/tool_bridge.json` 不应提交。以下配置已随 `perplexity-ai:reliability-20261030` 在本机启用，旧镜像不认识其中部分字段，回退时必须一起恢复配置：

```json
{
  "enabled": true,
  "transport": "json",
  "account_id": "local-account-1",
  "allowed_tools": ["*"],
  "run_timeout": 240,
  "max_runs": null,
  "max_waiting": 16,
  "queue_timeout": 2,
  "format_retries": 2,
  "visible_progress": true,
  "context_file_threshold_bytes": 30000,
  "context_checkpoints": true
}
```

`allowed_tools: ["*"]` 只接受客户端本轮声明的工具，不会赋予适配器本地执行权限。`run_timeout` 限制模型生成，不限制客户端工具时长。配置在第一次桥接请求时读取，变更后重启适配器。上下文附件阈值设为 0 可关闭附件切换，但会重新暴露长 query 截断风险。

`max_runs: null` 按绑定账号的 `max_concurrency` 与 `global_max_running` 的较小值计算，最高 32；显式整数可覆盖。当前桥接固定绑定一个账号，不乘以账号池总数。满载请求进入最多 `max_waiting` 个位置的 FIFO 队列；排队最多 `queue_timeout` 秒，取消会移除排队项。生成期限单独计时，总等待上限约为排队期限加生成期限。账号池自己的队列、速率及全局限制仍然生效。

原生请求先等待桥接准入，最多 2 秒。队列满或准入超时在 SSE HTTP 200 之前返回真正的 429、整数 `Retry-After` 和 `X-Should-Retry: true`；取得名额后才开始阶段提示和 ping。生成期间发生的失败仍通过 SSE error 返回。取消等待会释放队列项，共享请求的另一位消费者不受影响。只有已保存的成功回执可以重放，未完成的生成重试时仍须重新运行。

本机 Claude 专用网关供应商（账号 4）并发从 10 对齐到后端账号实际的 1，通过网关自身的等待队列协调多个会话，避免把本地容量不足的 429 误判为整个供应商暂不可用。其他供应商未改。网关等待使用其自己的期限和心跳；等待发生在网关时尚未进入适配器，因此桥接阶段文字不会提前出现。

日志记录每轮 `prompt_bytes`、`attachment_bytes`、确认点数量、纠错次数、响应中来源条目和数字引用尾缀计数，并按会话哈希记录新生成请求的 502 比率；最多保留 512 个会话统计，不记录原始会话 ID 或错误全文。来源条目也可能是上传附件，不能直接当作实际联网搜索次数。

JSON 模式无需 MCP 回调、域名或公网隧道；本机配置已删除旧回调密钥和 source ID。`transport: "mcp"` 实验模式仍保留，只有显式启用该模式时回调接口才开放。

## 2026-10-30 本地加固修改（历史记录，当时未部署）

以下是源码和配置变更；本次未重建或重启正在运行的适配器，也未安装全局 Claude Code Hook。

- JSON 模式向 Ask 传入 `sources: []`，包括格式纠正和上下文附件路径；底层任务队列区分 `None`（默认 web）和空列表（不请求搜索来源）。MCP 模式仍使用其指定来源。初始提示及纠错提示明确禁止模型添加引用标记，同时保留用户代码原有的方括号表达式。仍保留旧版数字尾缀兼容解析；空来源能否完全消除 Ask 自身追加引用，需要真实上游验证。
- 完整代码围栏通过首尾锚定的正则解包，支持 json/JSON、无语言标签及 LF/CRLF。拒绝半截围栏、外部说明和第二份 JSON，避免固定切片吃掉开头的 `{`。
- `max_runs` 默认值和本机配置由 8 调至 16。它限制桥接中同时存在的生成请求（含排队），不限制会话轮数。账号池的并发、速率和队列上限仍然生效，不会因此直接向 Ask 同时发出 16 个请求。
- 新增 `scripts/claude_file_preflight.py`，在实际执行 Read/Edit/Write 的电脑上校验文件，而非在适配器容器内访问同名路径。

本地预检使用 [Claude Code 官方 Hook 接口](https://code.claude.com/docs/en/hooks)：Read 成功后核对工具返回文本与磁盘对应范围，通过 `PostToolUse.additionalContext` 向下一轮上下文提供原始文件字节的 SHA-256、字节数及是否完整读取。该字段附在工具结果旁，不改写内置工具的 `tool_result` 内容。Edit/Write 前检查当前哈希；Edit 还检查原文是否精确匹配、是否重复、是否超出已读取范围；覆盖已有文件的 Write 要求完整读取。新文件 Write 正常允许。执行后的内容必须符合预检提案，才更新快照；失败或不识别的 Read 结果不会被当作有效快照。

按需要在目标项目启用（幂等合并 `.claude/settings.local.json`，原文件会备份；不改工具权限）：

```sh
.venv/bin/python scripts/claude_file_preflight.py --install /你的项目绝对路径
```

开启新的 Claude Code 会话后生效。状态只保存文件哈希和读取范围，按会话、子代理及 transcript 隔离，存放在 `~/.claude/bridge-preflight/`。Hook 不自动授予任何工具权限。仅安装到选择的项目；其他项目需要同样启用。

这个预检降低旧内容、部分读取和错误匹配造成的误改风险，但不是文件系统事务：检查到写入之间的外部并发修改仍有竞态，也不能证明模型理解了全文或识别所有语义上的截断。Bash、NotebookEdit 和第三方写文件工具不在此 Hook 范围内。对未知 Read 输出格式会要求重新读取；当前格式已用真实 Claude Code 验证。

独立复跑真实客户端的本地 Hook 验收（模型由 localhost 脚本模拟，不调用 Perplexity）：

```sh
.venv/bin/python scripts/smoke_file_preflight.py \
  --output-dir /Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/file-preflight
```

首次验收 `preflight-agsncotw`：8 次工具调用及结果，过期哈希 Edit 和部分读取后的 Write 均被阻止；合法 Edit、完整读取后的 Write、新建文件成功，哈希确实进入下一轮模型请求。本次完整离线测试为 **325 passed / 27 skipped**，两条已有 curl 取消警告。原有真实 Perplexity 验收仍对应下方历史记录，不能视为本次源码已部署的证明。

## 验收与复跑

```sh
# 离线协议、并发、错误、取消、回执和上下文附件测试
.venv/bin/python -m pytest -q

# 经当前真实网关：SSE 心跳、重试相同 ID、241 条消息及结构化输出
.venv/bin/python scripts/smoke_native_protocol.py \
  --output /Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/bridge-candidate/native-protocol.json

# 正常项目规则、Skill、Hook、ToolSearch、MCP、编码、压缩及恢复
.venv/bin/python scripts/smoke_claude_native.py \
  --output-dir /Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/native-bridge

# 先前基础编码与 25 秒命令测试
.venv/bin/python scripts/smoke_claude_coding.py \
  --output-dir /Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/model-bridge
```

正常会话脚本使用隔离的合成项目、项目 CLAUDE.md、项目 Skill、PreToolUse hook 和一个本地测试 MCP；不使用 `--bare` 或清空 settings sources。仅为测试进程允许指定工具，仅连接测试 MCP，并关闭子代理调用。它验证真实工具事件、随机 MCP 结果、项目与 Skill 标记、测试先失败后通过、测试文件未修改，以及实际 `/compact` 后恢复结果。每个阶段保留日志和机器可检查的摘要。

原生接入基线离线测试：**281 passed / 27 skipped**，两条已有 curl 取消警告。正常会话已完成 8 次工具调用、真实 `/compact` 与无工具恢复；241 条消息/149 KB 的首中尾标记和 25.15 秒命令均通过。最新真实验收结果见任务目录的 [BRIDGE-STATUS.md](/Users/wy/Documents/ChatGPT/perplexity/BRIDGE-STATUS.md)，失败尝试也保留记录。

## 仍需区分的能力

- 新版 `count_tokens` 和响应 usage 共用字符类别估算：ASCII 约 4 字符/token，CJK、假名及韩文约 1 字符/token，其他 Unicode 沿用 UTF-8 字节估算。它仍未对实际后端 tokenizer 校准，不能宣称中文一定更准或据此保证自动压缩阈值。旧算法一个常见汉字约 0.75 token，并不能因 UTF-8 用 3 字节就断言“严重高估”。参见 [Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)：计数取决于指定模型的 tokenizer。
- `output_config.format` 的 JSON Schema 会校验最终输出。`thinking`、effort、cache 等字段可以保存在请求上下文中，但不等于实现了 Anthropic 的签名思考、提示词缓存、服务端压缩或同等控制；`max_tokens` 等采样参数也不保证 Ask 原生执行。
- 附件转换通过离线测试；图片/PDF 真实理解仍未验收。子代理及所有 Claude Code 工具并未逐项验证。
- Skill、Hook、MCP、ToolSearch、压缩是 harness 能力。本项目只验证替代模型能否接住相关消息并正确继续。
- Perplexity 仍控制 Ask 提示词加工、模型路由和输出；复杂项目可靠性不能视为等同于原生 Anthropic API。

新版原生响应通过 `X-Bridge-Backend: perplexity-ask` 标识后端、`X-Usage-Estimated: true` 和 `X-Token-Estimator: unicode-heuristic-v1` 标记估算，并用 `X-Ignored-Params` 列出请求中提供但不能兑现原生语义的字段。`model` 保留 API 模型标识以兼容客户端，不把它改成客户端不认识的后端名称。

| 字段/能力 | 实际处理 |
|---|---|
| `thinking` | 保留输入；不生成伪造的带签名思考块 |
| `max_tokens`、`temperature`、`top_p`、`top_k`、`stop_sequences` | 无对应 Ask 原生控制；不承诺按这些参数截断或采样 |
| `effort`、`output_config.effort` | 保留输入；不保证模型遵循 |
| `cache_control` | 无 Anthropic prompt cache；响应回执重放不是前缀缓存 |
| `output_config.format` | 完整输出后校验 JSON Schema |
| 服务端工具 `web_search`、`code_execution` 等 | 显式拒绝，要求使用客户端 custom/function 工具 |

以下建议暂不启用：历史裁剪会改变完整历史设计；上传附件复用必须先确认账号隔离、引用有效期及上游重复引用能力，且逐轮变化的完整上下文本来不会命中同一内容哈希；final 提前发出会发生正文已显示、尾部完整性或 schema 校验才失败的情况，因此继续缓冲完整模型答案；按附件大小自动增加超时尚无可靠耗时模型，先保留显式期限和分阶段诊断。没有用服务器上的简单命令黑名单替代客户端权限。

## 工具权限边界

工具输出的随机边界及“作为数据”声明只是提示，不是提示注入防护证明。本地预检检查文件版本、读取范围和精确匹配，无法判断所有 Bash/MCP/Notebook 写操作，也无法保证检查到实际写入之间没有并发修改。使用这个 Ask 后端时保留 Claude Code 的逐工具权限控制，不使用 `bypassPermissions`、`--dangerously-skip-permissions` 或宽泛的写入/命令自动批准。

## 2026-10-30 后续源码验收：排队、确认点和纠错（历史记录，当时未部署）

本轮离线测试 **345 passed / 27 skipped**，两条已有 curl 取消警告。覆盖无换行/大写/CRLF 围栏、严格尾缀、全部确认点校验、强制工具纠错、FIFO 队列满载/取消/超时、容量释放、Retry-After、估算标记及会话统计。报告见 [hardening summary](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/hardening-20261030/summary.json) 和 [测试日志](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/hardening-20261030/tests-final.log)。

真实空来源测试：`sources: []` 被 Ask 接受，3.83 秒返回完整 JSON，无引用尾缀、无来源列表、无模型降级，显示模型 `claude55opus`。这是一次通过，不能外推为永不联网或永不加引用。

加载实时模型目录后的隔离验证：98,931 字节请求转成 116,245 字节附件，60 个工具结果、63 个确认点全部通过；首中尾随机结果正确、JSON Schema 通过，纠错次数为 0。来源条目计数为 1，不能据此认定实际发生联网。该检查仍不是全文理解证明。

早期隔离尝试未加载实时模型目录，触发本地自动回退到 Best，适配器追加的模型路由提示导致 JSON 校验失败；这些尝试不计入请求模型验收，也不能归因于 Ask 的格式能力。已补充 smoke 的目录前置检查；桥接遇到未知模型返回 400，明确上游降级返回 502，均不接受工具提案，避免静默使用另一模型。

本轮没有重建或重启运行服务，也没有把新字段写进运行配置。候选配置保存于 `data/bridge-candidates/hardening-1793345749577659000/tool_bridge.json`（0600，含账号标识，不应上传）。该轮结束时运行配置仍为显式 `max_runs: 16`、`format_retries: 1`、`visible_progress: true`；应用候选配置必须随兼容镜像部署一起进行。

## 部署与回退

当前镜像为 `perplexity-ai:reliability-20261030`。此前原生接入和阶段提示镜像分别为 `perplexity-ai:native-bridge-20261030` 与 `perplexity-ai:visible-progress-20261030`。代码仅在本地修改，未提交或推送。保留此前已有改动。以下命令记录历史基线部署，最新部署及回退见本文末尾。

```sh
docker build -f Dockerfile.local-overlay \
  --build-arg BASE_IMAGE=perplexity-ai:before-native-bridge-20261030 \
  -t perplexity-ai:native-bridge-20261030 .
docker tag perplexity-ai:native-bridge-20261030 perplexity-ai-perplexity-mcp:latest
docker compose up -d --no-build perplexity-mcp
```

回退本次原生接入必须同时恢复网关路由与适配器，不能只换镜像：

```sh
# 在本仓库目录执行。备份含凭据，不要打印或上传。
docker exec -i sub2api-postgres psql -U sub2api -d sub2api -v ON_ERROR_STOP=1 \
  < data/bridge-backups/20261030-143946/gateway-rollback.sql
cp data/bridge-backups/20261030-143946/data/config/tool_bridge.json data/config/tool_bridge.json
chmod 600 data/config/tool_bridge.json
docker tag perplexity-ai:before-native-bridge-20261030 perplexity-ai-perplexity-mcp:latest
docker compose up -d --no-build perplexity-mcp
docker restart sub2api
```

此回退恢复本次前的 JSON/OAI 桥接，不是更早的 Read-only MCP 实验。旧 MCP 写工具实验的 Ask 空答案问题未解决，不能据早期 Read 成功认定所有 MCP 转发可用。

协议参考：[Claude Code 网关兼容规范](https://code.claude.com/docs/en/llm-gateway-protocol)、[MCP 与 ToolSearch](https://code.claude.com/docs/en/mcp)、[Skills](https://code.claude.com/docs/en/skills)、[Hooks](https://code.claude.com/docs/en/hooks)。

## 2026-10-30 阶段提示与耗时诊断（历史部署）

本机已开启 `visible_progress: true`，源码默认关闭。阶段来自真实事件：准备上下文、入队、开始上传、开始等待 Ask、首次收到答案数据、校验、格式纠正。阶段变化立即唤醒 SSE；修正了等待所有任务结束及 yield 后清除通知导致阶段延迟的问题。`ping` 继续只承担连接保活。

提示明确带 `[桥接状态]`，不代表模型思考或工具已执行。普通原生 Messages 响应把提示放在首个文本块，实际答案/工具位于后续块。提示也进入持久化回执和客户端历史；相同请求重放相同文本及 ID。启用 JSON Schema 的结构化响应不加入提示。工具提案仍在整批校验通过后发送；失败保留 SSE error，不能因已经显示提示而算作成功。

任务快照的 `transport_events` 记录上传、等待模型、首个答案、上游结束的相对秒数；任务创建/开始时间单独计算排队。诊断未记录密钥或额外的模型内部推理。

实测记录位于 [visible-progress](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/visible-progress)：

- 同一约 50 KB 合成请求，基线第一段文字在 9.92 秒；首次更新后 0.157 秒显示提示、总耗时 10.017 秒。此结果改善可见性，不能宣称模型生成变快。
- 修正阶段唤醒后的请求在 0.069 秒显示提示。该轮同时存在其他请求，排队 36.664 秒，上传约 3.612 秒，等待首个模型答案约 6.536 秒，总耗时 47.087 秒；不同负载下总耗时不能直接横比。
- 真实 Claude Code 在 0.357 秒收到提示，10.111 秒调用 Read。工具实际读取正确，但上游一次空答案重试后的最终文字错误转述了文件值，该轮整体验收标记为失败（`claude-acf55d4c12`）。此前 `claude-32f4883acf` 的完整 Read/回复通过。阶段显示不能保证模型忠实复述工具结果。
- 当前账号 `max_concurrency: 1`；多个会话会排队，本次未修改该限制。`max_runs: 16` 是桥接准入上限，不等于账号能并发执行 16 个请求。
- 部署前离线测试 **326 passed / 27 skipped**，两条已有 curl 取消警告，见 [tests-progress-final.log](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/bridge-candidate/tests-progress-final.log)。实时阶段、错误、断线/并发重试和结构化响应均覆盖。
- 部署后 `normal-ab776ff3c2` 正常会话回归通过：8 次工具调用 / 8 个结果，编码 57.9 秒；实际 `/compact` 加无工具恢复 40.0 秒。4 项测试通过、测试文件未改，项目规则、Skill、Hook、ToolSearch、MCP 及随机结果保留；[摘要](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/visible-progress/normal-ab776ff3c2/summary.json)。
- 最终协议回归通过：短请求首提示 0.017 秒、回答 4.424 秒；重放消息 ID 和完整文本块一致。241 条消息 / 149 KB 的首中尾随机标记与 JSON Schema 校验通过，结构化响应不含阶段文本；[报告](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/visible-progress/native-protocol.json)。

复跑（会消耗真实 Ask 请求；避免同时启动多套验收）：

```sh
.venv/bin/python scripts/smoke_bridge_progress.py --phase after --claude-probe \
  --output-dir /Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/visible-progress-new
```

该轮镜像为 `perplexity-ai:visible-progress-20261030`。该轮工作区还有后续未部署的桥接修改，不能把当时所有本地源码都视作已上线；本次实际镜像和部署源码哈希保存在 [deployment.json](/Users/wy/Documents/ChatGPT/perplexity/mcp_probe/.runtime/visible-progress/deployment.json)。部署源码快照及修改前备份在 `data/bridge-backups/20261030-151653-progress/`，回退镜像标签为 `perplexity-ai:before-visible-progress-20261030`。本次未修改网关路由或升级 Sub2API。

只关闭阶段提示：将配置 `visible_progress` 改为 `false`，等待当前请求结束后执行 `docker compose up -d --no-build --force-recreate perplexity-mcp`；继续使用已部署镜像，避免把其他未验收源码一起重建上线。配置内容不要打印或上传。

## 2026-10-30 最新部署：可靠性加固

已部署 `perplexity-ai:reliability-20261030`，SHA-256 为 `acf38b47cf6888e92286c1ac8096220d25e961c7a590d7f8acb22ae70b98e1ca`。本机已启用本文顶部的新配置，以及 `/Users/wy/Documents/ChatGPT/perplexity` 项目文件预检 Hook。Claude Code 不需重装；新会话加载项目 Hook。未安装全局 Hook，未修改全局工具权限，未升级 Sub2API。

本轮新增工具结果内容摘要元数据、客户端文件读取行号范围、SSE 200 前的准入与标准 429、等待取消清理。网关透传兼容性响应头，Claude 专用供应商并发通过调度更新事件从 10 对齐到实际后端的 1。较早章节的 `max_runs: 16` 和“未部署”描述均为历史快照。

离线测试 **347 passed / 27 skipped**；真实 Claude Code 11 次工具调用和主动压缩/恢复通过；实际模型上下文含文件哈希及读取范围。真实网关两个并发请求均完成；适配器满载请求在 2.012 秒返回 HTTP 429、`Retry-After: 1`、`X-Should-Retry: true`。完整证据、失败记录、性能边界和复跑命令见 [RELIABILITY-UPDATE.md](/Users/wy/Documents/ChatGPT/perplexity/RELIABILITY-UPDATE.md)。阶段提示不是原生 token 流，这次没有消除完整上下文重复上传或提高模型推理速度。

本次回退点为 `perplexity-ai:before-reliability-20261030`，备份位于 `data/bridge-backups/reliability-20261030-153802/`。等待在途请求结束后，恢复备份的适配器配置、网关配置和 `gateway-concurrency-rollback.sql`，再恢复旧镜像；其中 SQL 包含调度缓存更新事件。不要将新配置与旧镜像混用，也不要使用前面原生接入章节的回退点来仅撤销这次加固。备份含凭据，勿打印或上传。
