# Lyre — builtin skills(出厂技能库,启动镜像)

> **文档定位**:在此之前 Lyre **没有 builtin skill**——整个 skill 系统是纯 user-space(`~/.lyre/skills/approved|proposed/`,agent 自行挖掘/固化)。要让 Lyre **随版本分发**精选 playbook(第一个:`adversarial-review`),需要一个 builtin-skill 落地机制。本文定的是它:**启动镜像**——package 内 `src/lyre/data/skills/` 是出厂源,每次启动覆盖刷新到 `~/.lyre/skills/builtin/`,与 `approved/` 一同进菜单,`approved/` 同名遮蔽 builtin(owner override)。
>
> **English one-liner**: Builtin skills are code-like (they track the installed version), unlike copy-once shipped personas/facts (frozen on first onboard). So Lyre ships them under `src/lyre/data/skills/` and *mirrors* them into `~/.lyre/skills/builtin/` on every startup (overwrite), surfaced alongside `approved/`, with an owner skill of the same name in `approved/` shadowing the builtin — the override escape hatch.
>
> **相关**:`runtime/skills.py`(扫描/scope/固化)、`PERSONAS.md`(shipped persona 是"配置型、拷一次冻住"的反面对照)、capability-discovery(proposed→approved 固化生命周期,不变)。
>
> **状态**:已实现并通过测试(846)。

## 1. 为什么不照搬 persona/facts 的"拷一次冻住"

shipped persona / facts(`ensure_shipped_facts`)= **配置型**:onboard 拷进 `~/.lyre` 一次、**永不覆盖**(owner 编辑优先)。代价:**升级不传播**——出了改进版,老安装收不到。

skill = **能力/配方(代码型)**:出了更好的 `adversarial-review`,你通常**希望它像函数升级一样到所有安装**,除非 owner 显式改过。所以 builtin skill 该**跟版本走**,不该冻住。

## 2. 硬约束:builtin 必须落在 `~/.lyre/skills/` 下

- 菜单:`load_skills_for_context(lyre_home)` 只扫 `~/.lyre/skills/`。
- 读正文:`read_memory` 沙箱在 `memory_root`,但**特例放行 `skills/...`** 解析到 `lyre_home/skills/`(`introspect.py`)——所以 agent 读 skill 正文也只到 `~/.lyre/skills/`。

→ builtin 不能"直接从 package load",否则要同时改扫描器 + read_memory 沙箱两处。**落到 `~/.lyre/skills/builtin/` 下,两处零改动即可读/可见。**

## 3. 设计:启动镜像 + override

- **出厂源**:`src/lyre/data/skills/<name>/SKILL.md`(随 wheel 分发,同 `data/checklists`)。
- **镜像**:`sync_builtin_skills(lyre_home)` **wipe + recopy** `~/.lyre/skills/builtin/`(覆盖刷新,所以删/改名的 builtin 不残留)。在 `bootstrap_runtime` 内调用——**`onboard` 与 `serve` 都走它**,故每次启动自动刷新。
- **扫描**:`load_skills_for_context` 现扫 `approved/` + `builtin/`,**approved 先扫**;碰名 first-wins → **approved 遮蔽 builtin**(owner override)。
- **三层语义**:`builtin/`=Lyre 出厂、每启动刷新、**owner 不碰**;`approved/`=owner/agent 固化的 + override 的;`proposed/`=待审。
- **固化生命周期不变**:builtin = "出厂即 approved";新 skill 仍走 `proposed/→approved/`。owner 定制 builtin = 拷一份进 `approved/` 改(遮蔽)。

## 4. 五铁律

- **一**:纯 FS + Python。**三/四**:`builtin/` 是每启动可重建的镜像(非权威态,权威在 package);override 在 `approved/`(global 文件层)。**五**:不涉通信。

## 5. 非目标

- 不改 `read_memory` 沙箱(已天然支持 `skills/…`)。
- 不动 proposed→approved 固化流程。
- 不做 builtin skill 的版本/签名校验(MVP:信任 package)。
