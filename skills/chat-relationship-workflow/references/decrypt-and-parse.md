# 解密与解析适配

只读用户授权来源，输出到全新独立目录。脚本成功产生文件后，还要核对身份、时间、数量和类型，才可开始导入。不要将 tool stdout 的文本当作认证证明。

## RMFH 备份

配套工具 `scripts/wechat_rmfh.py`：离线检查与 AES-GCM 认证解密，Python 3.10+、cryptography。公开已验证容器为 128 字节头/尾，nonce 在 header[19:31]，tag 在 footer[10:26]，当前无 AAD；遇到新布局按不支持处理。

```powershell
python scripts/wechat_rmfh.py inspect --source "D:/backup/source"
python scripts/wechat_rmfh.py check-key --source "D:/backup/source" --key-file "D:/private/backup-key.txt"
python scripts/wechat_rmfh.py decrypt --source "D:/backup/source" --key-file "D:/private/backup-key.txt" --output "D:/analysis/run-001/decrypted-container"
```

密钥准确名称：此份 RMFH 备份的 AES-GCM 数据加密密钥。公开格式使用 16 字节，即 32 个十六进制字符；工具接受其他 AES 合法长度不意味着当前备份支持。它不是微信密码、手机验证码、Apple ID、手机锁屏密码、Finder/iTunes 加密备份密码，也不是桌面数据库密钥。

不让用户把密钥贴入对话；用本地小文本文件或交互隐藏输入。样本密钥认证失败时不创建输出。每个文件分别认证，通过才发布到 decrypted/；unchanged/ 只是未加密或未知元数据副本。部分失败保留逐项报告，不能声称全量成功。

已核查的 `olansoft/wc4-backup-decrypt` 手机端提取方案针对 Android 指定版本。本包不包含密钥提取器，也没有已验证的 iPhone RMFH 密钥获取方法。缺密钥就清楚说明该缺口，不能声称凭一个备份文件夹能绕过加密。用户自己的其他数据来源可按备份分流说明继续。

## RMFH 解密后的 protobuf

配套的 `scripts/vendor/export_chat_messages.py` 来自 MIT 项目 wc4-backup-decrypt，保留原始代码与许可证。只把本包解密器新生成、无外部链接的 decrypted/ 作为输入；先用少量文件测试当前结构，再导出。输出目录须全新、与输入分开，且父目录已存在。

```powershell
python scripts/vendor/export_chat_messages.py "D:/analysis/run-001/decrypted-container/decrypted" --output "D:/analysis/run-001/protobuf-export"
```

输出 messages.jsonl、messages.html、envelopes.jsonl。HTML 时间为 UTC，timestamp_ms 为毫秒字符串，64 位数字保留字符串；不是可直接导入的 ChatLab 标准文件。field_2/field_3 的身份语义未经本包真实 iPhone 样本确认，不能猜哪个是发送者。

有了明文后，结合账号元数据及双方各几条已知短锚点确认发送方向、目标会话、时间与原始 ID，再制作当前格式的转换映射。若只能读到内容、不能确认是谁说的，停止情感归因和自动导入，明确缺的字段。不要把中性字段硬写成 sender，也不把转换脚本尚未实现称为已完成。

本包能检查 RMFH 并使用已提供的有效密钥认证解密；解析器经过合成样本验证，真实 iPhone RMFH 到 ChatLab 的端到端兼容性仍未证实。

## Windows Weixin 4.1 风格桌面数据库

配套 `scripts/desktop_db_reader.py` 只支持已核实布局：4096 字节页、80 字节保留区、Windows 小端 WAL index。它接收每库已派生的 32 字节 AES 密钥，验证 HMAC、稳定 DB/WAL/SHM 快照、已提交 WAL 和 SQLite quick_check；不是任意 SQLCipher 密码破解器。单库或 WAL 上限 1 GiB；不支持的布局停止。

`scripts/desktop_export.py` 将账号、对象、目录改为参数，仅提取目标会话。先看 `--help`，按其密钥文件格式使用已有授权的逐库密钥。密钥未取得时，说明需要每库已派生的密钥及匹配标识；不要索要微信登录密码。该工具不扫描进程、不自动提取密钥。

```powershell
python scripts/desktop_export.py --db-root "D:/source/db_storage" --key-file "D:/private/database-keys.json" --self-id "account-owner" --target-id "account-peer" --output "D:/analysis/run-002/desktop-export"
```

`db_storage` 的准确账号与目标账号 ID 必须可靠确认；显示昵称重名时不能任选。当前适配器只支持已知消息表和 Name2Id 结构，遇到未知 sender、冲突消息 ID 或变化的 schema 不推断修复。输出 JSON 与报告的实际文件名以该工具帮助和回执为准。

密钥文件结构为 `databases` 对象，其键是 `message/message_0.db` 等实际相对路径，值含 `key_hex`（64 个十六进制字符，32 字节已派生密钥）及可选 `salt_hex`（32 个十六进制字符）。不要将原始口令放入 key_hex。脚本发现的每个消息分库都需对应密钥；本包不含任何真实密钥。输出为 chatlab.json、raw_messages.jsonl、audit.json，状态明确为 exported_not_imported。

无原始 server ID 时使用 `local-source:` 前缀的确定性来源定位 ID；这能保留同秒同文的不同记录，但重分片或重建数据库可能改变该 ID，跨来源去重必须另核对。不能把本地生成 ID 宣称为微信原始 ID。

数据库忙或 WAL 不一致时，等待用户空闲后有限重试；不自动退出微信、删除 WAL、降级软件或恢复数据库。电脑端可见历史不等于手机全部历史。多媒体的占位与 XML 信息不等于已经识别声音、图片含义；分析时保留这一缺口。

## 已有导出与其他工具

先检查 `clb formats` 与导入帮助。已支持格式直接验证、预览，不再绕过二次转换。陌生 JSON/CSV/HTML/SQLite 先核实结构，再做小样本适配；保留源 ID、发送者、时间与类型。create-ex 的风格摘要解析器不能代替完整消息转换器。

确需新的开源工具时，核实仓库、版本、许可证、真实支持平台、输出行为与测试，先在副本/合成数据试验。星数和 README 宣称不等于兼容。不要下载不明可执行程序，不将密钥或完整聊天提交给外部解密网站。
