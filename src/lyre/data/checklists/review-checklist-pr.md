---
name: review-checklist-pr
description: PR 评审清单——reviewer persona 按此走完每一项再下结论
type: review_checklist
artifact: pull_request
---

# PR Review Checklist

按重要性从上到下。每项给出**判定**（通过 / 不通过 / 不适用）。

## 1. 正确性
- diff 实现的功能跟 PR description（或派活时的 acceptance）一致吗？
- 关键路径有测试覆盖吗？测试跑过了吗（`uv run pytest -q` 或对应仓库的等价命令）？
- 是否引入回归？看 PR 改的文件被哪些其它模块依赖。

## 2. Tier-2 风险（任何一条命中 → 不能 approve，必须 block 或 escalate）
- 改动了 CI 配置（`.github/workflows/`、`.circleci/`、`build.yml` 等）
- 改动了依赖（`pyproject.toml`、`package.json`、`Cargo.toml`、`requirements*.txt`）
- 包含 `git push --force` / `rm -rf` / 删除大量文件的脚本
- 改动了 secrets / 凭据 / `.env*` 模板
- 直接 merge 到 main 而非通过 PR

## 3. 安全
- 用户输入是否被验证 / sanitize？
- 有没有 SQL injection / command injection / path traversal 隐患？
- 新加的网络调用 / 文件读写是否在合理范围内？

## 4. 可维护性
- 命名清晰、函数体长度合理（>100 行函数需要正当理由）？
- 注释解释 **why** 而非 what？
- 有没有把"会变的"和"稳定的"混在一起的耦合？

## 5. 风格 / 形式（最低优先级）
- lint 过了？（`ruff check` 等价物）
- 类型检查过了？
- 跟仓库现有风格一致？

## 决议选项
- **approve**：1/2/3/4 全通过，5 有小瑕疵可在邮件里指出
- **revise**：2/3 通过但 1/4/5 有具体可改点 → 列出**精确**改动建议（文件+行号或函数名）回打给 worker
- **block**：任何 Tier-2 风险或明显回归 → urgency=high 邮件 worker，必要时 urgency=blocker 升级 owner

## 写邮件的格式
```
verdict: approve | revise | block
checklist 走完情况：
- 正确性：...
- Tier-2：...
- 安全：...
- 可维护性：...
- 风格：...

行动项（如有）：
- file.py:NN — 建议...
```
