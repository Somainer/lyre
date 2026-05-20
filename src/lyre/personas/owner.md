---
name: owner
role_description: "项目所有者，背后有人的 actor（不是 LLM agent）"
allowed_lyre_tools: []
needs_worktree: false
model_preference: null
---

（owner 不是 LLM agent。这条 persona 记录存在仅为 persona_profiles 的 name='owner' 行
提供 FK 目标，让 Soul（owner 偏好档案）有合法 home。）

如果某段流程意外把 owner 当作可调用 persona，应抛错。
