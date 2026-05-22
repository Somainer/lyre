---
name: owner
display_name: owner
kind: singleton
role_description: "项目所有者，背后有人的 actor（不是 LLM agent）"
allowed_lyre_tools: []
needs_worktree: false
model_preference: null
---

（owner 不是 LLM agent。这条 persona 记录存在仅为 `agents` 表里 `name='owner'` 那条
agent row 提供 FK 目标——mailbox / wakeups 等都通过 agent_id 引用 owner。Owner 的
identity & preferences 本身在 `~/.lyre/user.md`，是用户独写的文件，不进 DB。）

如果某段流程意外把 owner 当作可调用 persona，应抛错。
