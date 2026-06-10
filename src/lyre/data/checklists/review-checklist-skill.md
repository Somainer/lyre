---
name: review-checklist-skill
description: Skill 草案评审清单——reviewer 审 ~/.lyre/skills/proposed/<name>/SKILL.md 时按此走
type: review_checklist
artifact: skill_proposal
---

# Skill Proposal Review Checklist

按重要性从上到下。**通用性**是最关键的——大多数拒掉的草案都是栽在这一项。

## 1. 通用性（最关键）
判定：这个 skill 对**未来类似任务**真的通用吗？还是 task-specific 的细节伪装成 skill？

- **正例**："如何在 lisa-lang 加一个 builtin function"、"如何诊断 sbt 依赖冲突"
- **反例**："修了 issue #142 里 X 的 bug 怎么做的"、"为 owner 的 Y 项目重命名 module"

**通用性不够 → 直接拒**。不要心软。

## 2. 完整性
- body 步骤清晰、可独立执行（不假设外部上下文 / 不假设上一个 task 留下的状态）？
- 命令是否完整（不是"再 build 一次"而是给出确切命令）？
- 假设的输入 / 输出明确？

## 3. 不重复
`shell_exec ls ~/.lyre/skills/approved/` 看是否已经有同义品。
- 已存在同名 / 同主题 skill → **拒**（让 worker 改进既有 skill 而不是新增）
- 部分重叠 → **回打**，让 worker 把差异点说清楚或合并到既有 skill

## 4. 安全
body 不应包含：
- `rm -rf` / 大批量删除
- 直接 merge to main / `git push --force`
- 改 CI / 改依赖文件 / 改 secrets
- 任何在 worker.md Tier-2 矩阵里需要请示 owner 的操作

命中任一 → **拒**或**回打**让 worker 删掉危险步骤。

## 5. 准确
- frontmatter `description` 跟 body 实际能解决的问题一致？
- `scope` 字段标识合理（global / per-repo / per-language）？

## 决议落地

### approve
```bash
shell_exec mv ~/.lyre/skills/proposed/<name> ~/.lyre/skills/approved/<name>
mailbox_send to=<proposing-worker> body="approved skill <name>"
```

### reject
```bash
shell_exec rm -rf ~/.lyre/skills/proposed/<name>
mailbox_send to=<proposing-worker> body="rejected skill <name>: <理由 1-2 句>"
```

### revise
不动文件。
```
mailbox_send to=<proposing-worker> body="please revise <name>:
- <具体改进点 1>
- <具体改进点 2>"
```

## 写邮件的格式
```
verdict: approve | reject | revise
checklist 走完情况：
- 通用性：...
- 完整性：...
- 不重复：...
- 安全：...
- 准确：...

理由 / 改动建议：...
```

## 边界
- 你不直接编辑 skill 文件内容——要改让 worker 重提交
- 你是仅有的能 `mv` 到 `approved/` 的 persona
