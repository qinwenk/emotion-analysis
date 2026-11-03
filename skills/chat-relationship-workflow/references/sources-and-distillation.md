# 蒸馏与来源

本工作流将已有 skill 的有效机制重新组织为“备份 → 解密解析与导入 → 证据分析与补问 → 结果”四步。它不调用真人模拟器，也不把上游未经实现的能力继承成承诺。以下依据为 2026-11-03 本机安装快照的实际文件，不代表已重新核实各仓库最新提交或星数。

## 来源与取舍

| 来源与实际地址 | 核实许可 | 保留与改写 | 不纳入 |
| --- | --- | --- | --- |
| 狗头军师 goutoujunshi：[README 安装地址](https://github.com/shengjidaguai-china/goutoujunshi)；[README 徽章地址](https://github.com/powerycy/goutoujunshi)。本地两地址并存，不推断迁移关系。 | 本地 LICENSE：MIT，Copyright (c) 2026 powerycy。 | 具体行为与反证、分开恋爱意愿与适配、心理学按需加载；结合本项目用户修正改成自然直球、可校准的锐评和有依据的希望。 | 固定五步输出、必填问卷、人格配对分、羞辱或操控式互动。 |
| [ChatLab](https://github.com/ChatLab/ChatLab)；[官方外部 Agent 文档](https://github.com/ChatLab/ChatLab/blob/main/docs/cn/ai/external-agent.md)；[分析 skill 文档](https://github.com/ChatLab/ChatLab/blob/main/skills/chatlab-analyze-cn/SKILL.md)。 | 本机 chatlab-cli 0.37.7 package.json 声明 AGPL-3.0；已安装 chatlab-analyze 目录没有独立 LICENSE，不假定其为 MIT。 | 用官方 CLI 契约查证据、确认身份与范围、保留消息编号和隐私预处理；本包以原创流程说明描述接口。 | 不复制 chatlab-analyze 原文件，不打包 ChatLab CLI 或其依赖树，不将其改标为本包许可。 |
| [create-ex / ex-skill](https://github.com/therealXiaomanChu/ex-skill) | 本地 LICENSE：MIT，Copyright (c) 2026 therealXiaomanChu。 | 借鉴关系事件时间线、反复模式和新材料更新思路；改成事件证据与暂定解释。 | 不复制代码或人物模板；不默认前任、不生成替身，不从照片扩展提取位置，不直接运行其解析器。 |
| [distilly](https://github.com/titanwings/distilly) | 本地 LICENSE：MIT，Copyright (c) 2026 titanwings；package.json、CITATION.cff 一致。 | 借鉴按需加载、来源约束、场景化纠正与版本恢复思路；路径按实际 skill 根目录解析。 | 不复制代码或人物模板；不启动 owner onboarding、名人研究、采集器或强制 Persona 建档。 |

狗头军师的分析参考包含改编内容，本 skill 内保留 [原 MIT 许可证](../licenses/goutoujunshi-MIT.txt)，完整交付包的 `licenses/goutoujunshi-MIT.txt` 另保留一份。再分发本包或单独分发相应改编文件时，保留该版权和许可声明。create-ex、distilly 仅作机制参考，没有复制其代码或大段模板。ChatLab CLI 是外部依赖，安装和分发遵循其自身许可。

## 随包分发的上游解析器

本包另原样分发 [olansoft/wc4-backup-decrypt](https://github.com/olansoft/wc4-backup-decrypt) 已下载快照中的 `tools/export_chat_messages.py`，位于 [scripts/vendor/export_chat_messages.py](../scripts/vendor/export_chat_messages.py)。其 [原许可证](../scripts/vendor/LICENSE.txt) 为 MIT，Copyright (c) 2026 olansoft；完整包也保存 `licenses/wc4-backup-decrypt-MIT.txt`。没有打包该上游的密钥获取或解密实现。

这段解析器只处理已经解密的 ChatPackage，导出 HTML 与 JSONL。其 `field_2`、`field_3` 是未经本流程自动判定语义的原始字段，必须另核实谁是发送者、谁是接收者及本人的账号；不能根据两个字段的位置猜方向。输出不是 ChatLab 标准格式，需经过独立规范化与校验，不能直接宣布“已支持 ChatLab 导入”。

本交付的验证范围限于合成记录与结构检查，未使用真实 iPhone RMFH 备份完成端到端解密和解析验证。具体执行过的用例及结果以交付测试记录为准，不能把合成测试通过外推为所有版本、消息类型和备份均可使用。

## 为什么没有直接拼接上游工具

本机 create-ex 的 `wechat_parser.py` 虽然说明写有 CSV、HTML、SQLite，但解析函数字典实际只有 `wechatmsg_txt`、`liuhen` 和 `plaintext`；其它检测结果会退回纯文本。它没有解密实现，输出主要是风格摘要，不能作为无损 ChatLab 导出。这个检查结果要求本流程按真实文件结构选适配器，并核验输出；不能通过“脚本运行结束”宣称解析成功。

人物模拟型 skill 的“Persona”也不是对方内心的事实。本流程只提取有依据的行为与互动，明确推测和未知。用户补充可以纠正理解，未经选择不持久化人格档案。上游关于标签、依恋、情感还原度的表述不直接继承。

## 本项目加入的修正

- 用户目标决定建议方向，证据决定结论强度。保留真实机会，不根据用户想听什么编造爱意。
- 锐评落在判断跳步和行为上；用户脆弱或表示不适时收起调侃。反问允许用户否认，不指定其感受。
- 内部分析要深入，外部表达按当前问题组织。先有用地回答，再补最有判别力的信息；不固定问卷和报告模板。
- 分开亲近、恋爱意愿、相处适配、持续经营。理论帮助形成可验证的问题，不用于诊断、读心或计算成功率。
- 备份保存、认证解密、结构解析、导入核验和完成分析是不同状态。切换数据来源时说明变化，不能把桌面记录导出成功写成手机备份解密成功。
- 可分发流程与用户运行数据分开。默认没有真实聊天、账号密钥、人物档案或自动长期记忆。

心理学公开来源与实际核实范围见 [心理学机制与来源](psychology-mechanisms.md)。
