# 电脑端自动获取与分析

默认主流程使用 Windows 微信已同步的本地数据库。官方手机备份不是前置条件，也不会被桌面密钥解开。用户本人已登录微信、指定联系人及范围并请求获取时，沿用该授权执行；只让制作流程或解释工具时，不扫描真实进程。

## 当前边界

- 运行时取钥仅启用已验证布局的 Windows 微信 **4.1.15.56**，需要 Windows 64 位 Python 3.10+。新版适配器经过合成验证，依据此前成功的取钥算法改编；这不等于已验证所有设备或所有 4.1 版本。
- 同一安装的多个微信进程可在总预算内尝试；多个账号目录、不同安装或版本组不能猜选。默认只查用户目录和 Documents 中的 `xwechat_files`，自定义位置用 `--db-root`。
- 同步、登录或手机确认由用户在微信中完成，本程序不操控手机、不自动完成全历史同步。记录缺少时明确实际覆盖，不把文件名或同步提示当完整性证明。
- 密钥只读取得、逐库验证、只在内存传递。默认总时限 120 秒、总读取预算 4 GiB，可读单区小于 500 MiB；不写进程、不注入、不联网传钥、不自动提权。权限不足或版本不匹配返回具体错误。
- 媒体消息和已有元信息可保留，附件提取和语音转写尚未实现。

## 一句话请求如何执行

用户可以说：“获取我和某人的最近半年聊天，导入后分析关系卡点。”复用已知账号、目标与日期；相对时间按用户时区解析。缺目标或同名时才补问，未指定时间范围可使用电脑端全部可取得记录，并说明范围。不要每轮重新询问手机型号或要求制作 Backup 包。

首次完整安装运行仓库的 `Install.cmd` 或 `Install-Workflow.ps1`，自动配置固定版本的专用 Python、Node、ChatLab CLI 0.37.7 及两个配套 skill。安装和检查不会访问微信或用户 ChatLab 会话。安装后主 skill 根目录的 `runtime.json` 保存本机程序位置，不打印、不发布该文件；运行入口自动读取它。仅复制 skill 文件夹或使用 `-SkillsOnly` 不会完成环境配置。

将主 skill 目录解析成实际绝对路径；下列 `D:/my-codex-skills/...` 均为示例。参数按数组传入，包含空格、引号或中文的参数保持为同一个数组项：

```powershell
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Tool check
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Tool chatlab -Arguments @('manifest')
```

`check` 只做隔离环境检查。`chatlab` 用于正常已授权的导入或查询，沿用用户当前的数据目录；参考文档中的 `clb.cmd ...` 可对应为此入口的参数数组。安装器的临时测试目录不会变成真实数据目录。没有运行配置时，按已有安装授权补跑完整安装器，不改用未知的系统程序。

随后执行预检；这一步会查看微信进程元数据，因此只在用户请求获取或明确检查兼容性时使用：

```powershell
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Arguments @('discover')
```

`discover` 仅检查目录和进程元数据，不读取密钥。随后执行（人物和路径均为占位示例）：

```powershell
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Arguments @('run', '--contact', '示例联系人', '--since', '2026-01-01', '--until', '2026-10-31', '--run-dir', 'D:/private-runs/run-001', '--question', '分析双方意愿、适配与关系卡点')
```

- `--contact` 精确匹配备注、昵称或完整账号；同名不按任意排序选人。
- `--since`、`--until` 支持 ISO 日期或时间。无时区默认 `--utc-offset +08:00`；结束日期包含当天最后一秒。其他用户时区应显式传偏移或带时区的时间，跨夏令时优先传两个带各自时区的边界。
- 无日期筛选表示电脑目标会话全部可取得记录。
- `--db-root` 指定选定账号的 `db_storage`；`--self-id` 仅在自身份无法确认时使用用户已确认的完整账号。账号目录与显式身份冲突会停止。
- `--pid` 用于明确进程选择；`--scan-timeout` 为 5–300 秒。不要通过不断增加时限盲试未知布局。
- `--session-id` 可复用当前已知的 ChatLab 会话。未提供时按本人、对方的稳定账号核对已有单聊；多个匹配会话需选择，昵称相同不算同一会话。
- `--clb` 可指定实际 `clb.cmd` 路径；正常使用不必填写，运行入口自动提供已配置的位置与私有 Node。程序直接调用 Node 的 ChatLab 入口，不把姓名等参数拼进命令 shell。
- `--run-dir` 必须是独立新目录，不能位于微信数据库目录内或包含源目录。密钥不会写入任务目录。

实际范围在认证读取后保存在 `coverage`：源目标会话最早/最新时间、原始条数、请求范围与选中条数。时间窗没有记录时返回 `empty_time_range`，不制造空导入。覆盖端点不能证明中间无缺失，更不能证明与手机一致。

## 状态、恢复与分析

任务目录包含：`workflow.json`（阶段与身份/范围元数据）、`export-N/chatlab.json`、`raw_messages.jsonl`、`audit.json`，以及导入完成后的 `analysis-request.json`。导出内容属于私人数据，不能发布；状态不记录密钥或消息正文。

```powershell
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Arguments @('status', '--run-dir', 'D:/private-runs/run-001')
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Arguments @('resume', '--run-dir', 'D:/private-runs/run-001')
```

登录或环境恢复后可继续。尚未导出时，可用 `resume --contact <完整账号>`、`--db-root`、`--self-id` 或 `--pid` 解决明确歧义。已导出任务不允许更换账号/联系人，恢复时检查三个输出文件哈希，复用那次快照。新的同步记录要用新目录获取；同一账号对在 ChatLab 复用会话并去重。

导入执行严格校验、预览、身份检查、正式导入及入库聚合验收。首次导入核对各发送者数量；增量导入核对总新增量、各方数量变化边界与时间覆盖，CLI 不提供分发送者的重复计数，因此不能把这些检查说成逐条内容比对。中断后不删除会话，恢复先查当前状态与重复情况，避免再次写入相同消息。

程序到 `analysis_ready` 后，agent 读取交接中的会话、身份、问题与范围，按 [关系判断与互动表达](analysis-and-dialogue.md) 和 [关键场景检索与证据](key-scenes-and-evidence.md) 真正取证、分析并写出报告及证据台账。正文仅走 `clb --format agent`；不能直接从 raw_messages.jsonl 绕过隐私预处理做关系分析。文件保存后记录完成：

```powershell
& 'D:/my-codex-skills/chat-relationship-workflow/Run-Workflow.ps1' -Arguments @('finish-analysis', '--run-dir', 'D:/private-runs/run-001', '--report', 'D:/private-runs/run-001/relationship-report.txt', '--ledger', 'D:/private-runs/run-001/evidence-ledger.txt')
```

`finish-analysis` 检查导入状态、文件属于本任务且非空，并保存哈希；它不会代替人工/agent判断报告质量。只有真实完成分析后才调用。单独运行 Python 不会自动调用大模型或虚构一份报告。

## 需要用户处理的状态

账号重名或多个目录：展示最小必要候选，让用户确认一次。联系人重名：给出可区分备注/昵称/账号信息，随后用完整账号继续。自身份不明：只请求确认本人账号，不猜。

未登录、权限不足或未知版本：说明当前缺口，不自动退出微信或更改系统设置。记录不足或空窗：报告已发现范围，请用户补齐所需同步或缩小范围；不要改去 Backup 路线却声称来源相同。认证、WAL 或数据库结构不通过：停在对应阶段，原文件保持不变。

运行时扫描来源和许可见 [蒸馏与来源](sources-and-distillation.md)。这个入口没有通用取任意账号数据的能力，输出只针对所选账号中的指定双人会话。
