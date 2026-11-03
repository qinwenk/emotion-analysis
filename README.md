# emotion analysis

用户指定联系人，获取电脑微信已同步的聊天，自动导入后进行有依据的关系分析。

这是一个可安装的 Codex skill，包含本地执行程序、参考资料和处理脚本。默认使用电脑端数据库；已有导出或 ChatLab 会话直接续用。官方手机备份是独立可选来源，不是必经步骤。

## 电脑端自动流程 v1

**同步所需聊天到电脑 → 检查环境与账号 → 取钥 → 稳定快照与认证解密 → 核对实际范围、解析目标会话 → 导出 → 验收导入 ChatLab → 由 agent 分析。**

首版只启用 **Windows 微信 4.1.15.56、64 位 Python 3.10+、ChatLab CLI 0.37.7**。用户需要登录并完成所需聊天同步；程序不自动操作手机，不保证电脑已有手机全部历史。准确消息数量和起止时间在解密后核对。联系人重名、多账号、不支持的版本或空时间窗会明确停止，不猜选。

- 自动发现常见账号目录、支持自定义路径；按备注/昵称/完整账号匹配联系人并核对本人身份。
- 只读、限时的运行时取钥，逐库验证；密钥只在内存传递。动态枚举消息分库，原数据不修改。
- 时间筛选、稳定消息 ID、原始字段溯源；按双方账号复用唯一 ChatLab 会话，校验、预览、导入和聚合验收。
- 保存任务阶段，支持恢复；已导出任务先验证文件哈希。新同步记录用新任务获取，再增量导入。
- `analysis_ready` 表示待分析；agent 完成证据检索、报告与台账后才记录 `complete`。单独执行 Python 不会自动调用大模型或生成情感报告。

入口与完整参数见[电脑端自动流程](skills/chat-relationship-workflow/references/desktop-automation.md)。新版取钥适配器沿用此前验证过的算法，完成合成内存测试；**本次发布没有重新读取真实微信内存或用户聊天，不把合成通过当作所有电脑兼容保证。**

## 本轮重点：关键场景分析 v1

2026-11-03 的这一轮改进围绕一条主线：**关系关键节点与卡点 → 用户情绪变化 → 关键场景深析 → 针对当前困境的行动建议**。

- **找变化发生在哪里**：以完整事件判断靠近、退回、停滞或修复，保留前后依据；平静的边界、承诺和现实条件也属于候选节点。
- **补齐事件上下文**：时间浏览与主题检索结合，核对分页、截断、引用和跨天后续，记录实际阅读覆盖；不把关键词第一页当成全量证据。
- **选择值得深析的场景**：分别看关系意义、用户情绪变化和证据充分度，结合本人日常表达及补充，不靠消息数量或标点生成情绪分。
- **给对应困境的办法**：还原事件、理解、需要、双方回应与后续；围绕主要卡点提出可执行的一步，说明对方需要怎样参与、观察什么反馈及何时调整或停止。

这版增强的是 agent 使用现有 ChatLab 工具检索、核对和分析的流程，**没有新增自动情绪检测模型或向量检索服务，也不承诺找全所有节点或读取未表达的内心**。合成案例用于检验行为，真实场景中的漏检和误报仍需持续评估。详见[关键场景检索与证据](skills/chat-relationship-workflow/references/key-scenes-and-evidence.md)。

2026-11-03 补充优化：保留先给直接判断、用关键场景解释意愿与适配、找主要卡点并给下一步的输出思路，篇幅与结构随问题调整。用户仍在判断是否想恋爱时，先帮助了解自己的需要；用户纠正感受后，追溯更新依赖该事件的模式、卡点、建议和本次分析文件。撤下负面推论不等于增加恋爱意愿的证据。详见[关系判断与互动表达](skills/chat-relationship-workflow/references/analysis-and-dialogue.md)。

## 获取与分析如何衔接

1. **同步并检查环境**：用户登录并同步所需记录，程序检查版本和账号目录；已有导出或会话直接续用。
2. **取钥、解密、解析与导入**：处理稳定只读快照，确认联系人和双方身份，核对实际时间、数量及类型，再经 ChatLab 校验、预览、导入与验收。官方备份分支另按格式和密钥处理。
3. **情感分析与必要补问**：寻找关键节点和主要卡点，补齐关键场景的前因后果；深入分析与当前困境相关的情绪变化，补问最能改变建议的信息。
4. **输出结果**：用证据解释节点、卡点与关键场景，给出针对困境的办法、局限及观察反馈。需要一句回复时先给一句，不强制完整报告。

表达可以直接，也可以适度调侃，但结论跟着证据走。亲近、恋爱意愿、相处适配和持续经营分别判断，不把消息数量、MBTI 或评分换算成“恋爱成功率”。希望来自真实互惠和可验证的行动；明确拒绝后停止推进。

## 当前支持范围

| 来源 | 本包能做什么 | 前提与限制 |
| --- | --- | --- |
| ChatLab 支持的明文导出或已有会话 | 按实际 CLI 契约验证、导入、核对并查询分析 | 先确认双方身份和覆盖范围；陌生格式需要独立适配，不能仅改扩展名。 |
| 微信 RMFH 备份 | 检查容器；使用已有有效密钥进行 AES-GCM 认证解密；提供解密后 protobuf 解析器 | **不包含 RMFH 备份密钥提取器。没有已验证的 iPhone RMFH 取钥方法，也没有真实 iPhone RMFH 到 ChatLab 的端到端兼容验证。** protobuf 中的身份字段仍需核实，输出不是可直接导入的 ChatLab 标准格式。 |
| Windows 微信 4.1.15.56 桌面数据库 | 自动只读取钥、联系人解析、范围筛选、认证读取稳定数据库/WAL 快照、目标导出与 ChatLab 验收导入 | **运行时适配严格限定该版本。** 需要本机登录和所需记录已同步；未知版本、权限不足或未知 schema 停止。电脑记录可能少于手机历史，桌面密钥不能解 RMFH 备份。 |
| Finder/iTunes 设备备份、其他版本或未知格式 | 识别候选来源、说明缺口 | 本包没有通用设备备份导出器，也不承诺适配所有微信版本。 |

备份完成、解密成功、解析完成、导入验收和完成分析是不同状态。合成测试通过不能证明真实备份兼容；媒体记录存在也不代表语音已转写、图片已理解或附件已恢复。

## 安装

需要可加载 skill 的 Codex 环境，以及：

- **Python 3.10+**：脚本依赖 `cryptography>=42.0.0` 和 `zstandard>=0.22.0`，见 [requirements.txt](skills/chat-relationship-workflow/requirements.txt)。CI 使用 Windows 与 Python 3.12。
- **外部 ChatLab CLI 0.37.7**：本包导入与查询契约按此版本核实。CLI 需另行安装，见 [ChatLab 项目](https://github.com/ChatLab/ChatLab) 与[官方外部 Agent 文档](https://github.com/ChatLab/ChatLab/blob/main/docs/cn/ai/external-agent.md)。它不随本仓库分发。

在 PowerShell 中克隆仓库并进入目录，也可下载 ZIP 后在解压目录执行安装命令：

```powershell
git clone https://github.com/qinwenk/emotion-analysis.git
cd emotion-analysis
python --version
python -m pip install -r skills/chat-relationship-workflow/requirements.txt
.\Install-Workflow.ps1
```

安装器默认复制到 `$env:CODEX_HOME/skills/chat-relationship-workflow`；未设置 `CODEX_HOME` 时使用用户目录下的 `.codex/skills/chat-relationship-workflow`。已有同名 skill 会先备份到本仓库的 `backups/`，随后复制文件并校验哈希。它只安装工作流，不安装 Python 或 ChatLab，不读取聊天、不提取密钥、不执行导入。

也可以指定 skill 根目录：

```powershell
.\Install-Workflow.ps1 -SkillsRoot "D:/my-codex-skills"
```

外部 CLI 就绪后，先检查当前接口。Windows PowerShell 使用 `clb.cmd`：

```powershell
clb.cmd manifest
clb.cmd formats
clb.cmd import --help
```

不同版本的参数可能变化。`manifest` 和 `formats` 在已核实版本中不接受 `--json`；导入与查询命令按各自帮助使用输出参数。

## 使用

在 Codex 中明确调用 skill，并说明对象、问题和材料位置。以下路径和人物均为占位示例，请替换为自己的授权范围：

```text
使用 $chat-relationship-workflow，我已经将需要的聊天同步到电脑微信。
请获取我和“示例联系人”最近三个月的记录，导入 ChatLab，
分析双方意愿、适配和关系卡点。结果放到 D:/private-runs/run-001。
```

已有 ChatLab 会话时可以直接继续：

```text
使用 $chat-relationship-workflow，接着 ChatLab 会话 my-private-session 分析。
先确认会话中的“我”和“对方”，找出关系推进的关键节点和当前卡点。
从关键场景中识别我的明显情绪变化，补齐前因后果后深入分析，
针对我现在想明确关系方向的困境，给出下一步和需要观察的反馈。
```

只有片段或口述也能讨论，但结论会限定在实际提供的材料内，不会声称读过完整聊天。后续补充会更新判断，不要求每次重新备份或导入。

也可以在仓库根目录启动本地程序，导入完成后再由 skill 继续分析：

```powershell
python -X utf8 skills/chat-relationship-workflow/scripts/desktop_workflow.py discover
python -X utf8 skills/chat-relationship-workflow/scripts/desktop_workflow.py run --contact "示例联系人" --since 2026-01-01 --until 2026-10-31 --run-dir "D:/private-runs/run-001"
python -X utf8 skills/chat-relationship-workflow/scripts/desktop_workflow.py status --run-dir "D:/private-runs/run-001"
```

恢复使用 `resume --run-dir ...`；已导出文件被修改会停止。自定义目录用 `--db-root`，必须指向账号 `db_storage`，不能指向 `Backup`。

### 先试合成示例

[examples/synthetic-chat.json](examples/synthetic-chat.json) 只含虚构身份与五条测试记录，包含同秒同文但不同 ID 的消息、图片占位和系统撤回事件。它用于检查结构和计数，不用于展示真实关系分析。

在仓库根目录可执行校验与导入预览：

```powershell
clb.cmd validate examples/synthetic-chat.json --json
clb.cmd import examples/synthetic-chat.json --format chatlab --session-id emotion-analysis-synthetic-demo --dry-run --json
```

预览不写入聊天会话，但 CLI 可能初始化自身数据目录。正式导入由工作流在用户授权的范围内执行，并核对数量、时间、身份和代表性消息。详细约定见 [ChatLab 导入与验收](skills/chat-relationship-workflow/references/chatlab-import.md)。

### 本地工具入口

以下命令均从仓库根目录运行，只显示帮助：

```powershell
python skills/chat-relationship-workflow/scripts/probe_source.py --help
python skills/chat-relationship-workflow/scripts/wechat_rmfh.py --help
python skills/chat-relationship-workflow/scripts/desktop_export.py --help
python skills/chat-relationship-workflow/scripts/desktop_workflow.py --help
```

实际处理请先读[电脑端自动流程](skills/chat-relationship-workflow/references/desktop-automation.md)；其他来源再看[备份与来源分流](skills/chat-relationship-workflow/references/backup-and-routing.md)和[解密与解析](skills/chat-relationship-workflow/references/decrypt-and-parse.md)。原始来源保持只读，结果写入独立新目录。RMFH 备份密钥与桌面逐库派生密钥用途不同；缺少哪一种，就明确停在哪一步，不盲试密码或冒称已恢复。

## 数据与隐私

- 只处理用户有权处理且明确指定的账号、对象和范围；不因工具可以批量导出就扫描所有联系人。
- **不要把真实聊天、备份、数据库、密钥、账号信息或分析报告提交到公开仓库、Issue、Pull Request 或 CI。** 本仓库示例与测试只使用合成数据。运行结果和安装器备份也应保存在本地，不作为发布内容。
- 自动取钥仅在内存传递；手工提供密钥时使用本地文件或支持的隐藏输入，不粘贴到对话中，不向外部解密网站提交密钥或完整聊天。
- 本地脚本处理不等于模型一定离线。分析时按实际使用的 Codex、模型与工具环境处理必要片段，不能承诺聊天内容永不经过外部服务；本工作流不把整个聊天文件作为额外附件上传。
- 导入授权不等于长期记忆授权。默认不创建人物档案、真人替身或跨任务关系记忆。

## 验证

在安装 Python 依赖后，从仓库根目录运行：

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python skills/chat-relationship-workflow/scripts/desktop_db_reader.py --self-test
```

当前共 **130 项合成测试**：112 项单元/集成测试与 18 项数据库读取自测。覆盖只读取钥的合成内存、联系人消歧、真实加密的合成数据库与日期筛选、恢复/哈希保护、ChatLab 身份和数量验收，以及原有 RMFH/protobuf 工具。另在独立数据目录运行真实 ChatLab CLI，验证首次、重复及增量导入，不访问用户会话。[GitHub Actions](.github/workflows/test.yml) 使用 Windows、Python 3.12 跑合成测试，不扫描真实微信，也不证明真实设备端到端兼容性。

独立串联演练使用真正加密的合成数据库，仅替换运行时取钥接口：6 条目标记录中按日期导入 5 条，排除另一会话；恢复新增 0 条，随后实际取证、生成报告与台账并完成状态登记。此结果验证程序与 skill 的衔接，不代表已重新验证真实微信的内存取钥。

关键场景的独立行为评测另见 [evals/README.md](evals/README.md)。它包含合成聊天、用户补充和人工评判标准；与上述工具单元测试分开，不通过匹配固定话术给分析判分，不将冒烟结果解释为真实聊天的准确率。

## 文件与来源

本项目原创工作流与脚本采用 [MIT 许可](LICENSE)。第三方改编与随包文件保留各自的版权声明。

- [主 skill](skills/chat-relationship-workflow/SKILL.md)：电脑自动获取到关系分析。
- [电脑端自动流程](skills/chat-relationship-workflow/references/desktop-automation.md)：运行条件、入口、范围与恢复。
- [关系判断与互动表达](skills/chat-relationship-workflow/references/analysis-and-dialogue.md)：证据、补问、直白反馈与有依据的希望。
- [关键场景检索与证据](skills/chat-relationship-workflow/references/key-scenes-and-evidence.md)：时间覆盖、事件上下文、节点与卡点、用户情绪及证据记录。
- [心理学机制与来源](skills/chat-relationship-workflow/references/psychology-mechanisms.md)：按需解释互动，不用于诊断或读心。
- [蒸馏与来源说明](skills/chat-relationship-workflow/references/sources-and-distillation.md)：改编、借鉴和未纳入的能力。

随包的 protobuf 解析器来自 [olansoft/wc4-backup-decrypt](https://github.com/olansoft/wc4-backup-decrypt)，保留[原 MIT 许可](licenses/wc4-backup-decrypt-MIT.txt)；关系分析参考中的改编内容保留 [goutoujunshi 的 MIT 许可](licenses/goutoujunshi-MIT.txt)。ChatLab CLI 是遵循其自身 **AGPL-3.0** 许可的外部依赖，本仓库不打包其代码或依赖树。再分发时请保留相应第三方版权与许可声明。

桌面取钥适配器的 Config.Cipher 最小算法改编自 [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool/tree/79f1b5b92e12c66aa281b4a60a3c478b5f547dfa)，保留[原 MIT 许可](skills/chat-relationship-workflow/licenses/wcdb-key-tool-MIT.txt)。
