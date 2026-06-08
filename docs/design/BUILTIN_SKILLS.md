# Lyre — builtin skills(出厂技能库,直接读 package)

> **文档定位**:在此之前 Lyre **没有 builtin skill**——整个 skill 系统是纯 user-space(`~/.lyre/skills/approved|proposed/`,agent 自行挖掘/固化)。要让 Lyre **随版本分发**精选 playbook(第一个:`adversarial-review`),需要一个 builtin-skill 落地机制。本文定的是它:**直接读 package**——package 内 `src/lyre/data/skills/` 既是出厂源也是读取处,菜单与 `read_memory` 都**在原地**读它(无拷贝、无镜像);`approved/` 同名遮蔽 builtin(owner override)。
>
> **English one-liner**: Builtin skills are code-like (they track the installed version), unlike copy-once shipped personas/facts (frozen on first onboard). So Lyre ships them under `src/lyre/data/skills/` and reads them **in place** — the skill menu scans that packaged dir directly, and `read_memory` permits that one trusted read-only root — with no copy/mirror into `~/.lyre`. An owner skill of the same name in `approved/` shadows the builtin.
>
> **相关**:`runtime/skills.py`(扫描/scope/固化)、`PERSONAS.md`(shipped persona 是"配置型、拷一次冻住"的反面对照)、capability-discovery(proposed→approved 固化生命周期,不变)。
>
> **状态**:已实现并通过测试(846)。本设计经一次 review 从"启动镜像"改成"直接读 package"(见 §3 注)。

## 1. 为什么不照搬 persona/facts 的"拷一次冻住"

shipped persona / facts(`ensure_shipped_facts`)= **配置型**:onboard 拷进 `~/.lyre` 一次、**永不覆盖**(owner 编辑优先)。代价:**升级不传播**——出了改进版,老安装收不到。

skill = **能力/配方(代码型)**:出了更好的 `adversarial-review`,你通常**希望它像函数升级一样到所有安装**,除非 owner 显式改过。**代码就在它装的地方读**,不该复制进用户目录。

## 2. 约束在哪里(以及正确的拆法)

- 菜单:`load_skills_for_context(lyre_home)` 原本只扫 `~/.lyre/skills/`。
- 读正文:`read_memory` 沙箱在 `memory_root`,有 `skills/...` 特例解析到 `lyre_home/skills/`。

这俩**只是现有 reader 的假设,不是物理约束**。正确做法是**让 reader 也读 package**,而不是把文件搬进 `~/.lyre` 去迁就 reader(那是配置的搬运模式,误用到代码上)。

## 3. 设计:直接读 package + override

- **出厂源 = 读取处**:`src/lyre/data/skills/<name>/SKILL.md`(随 wheel 分发,同 `data/checklists`)。`shipped_skills_dir()` 返回它。
- **菜单**:`load_skills_for_context` 扫 `approved/` + **`shipped_skills_dir()`(package)**;`<location>` 给的是该 SKILL.md 的**真实绝对路径**(在 package 里)。
- **读正文**:`read_memory`(`_resolve_memory_path`)放行**解析落在 `shipped_skills_dir()` 下的绝对路径**——一个**可信、只读、Lyre 出厂**的根;其余仍是 rel-only + 沙箱。所以无论 agent 用 `read_memory`(沙箱)还是 `shell/python`(直接 cat),拿菜单里那条真实路径都能读到,**同一条路径对所有读工具有效**。
- **override**:`approved/` 先扫,碰名 first-wins → **同名 owner skill 遮蔽 builtin**。owner 定制 = 拷一份进 `approved/` 改。
- **测试隔离**:`load_skills_for_context(..., include_builtin=False)` 关掉 package 扫描,便于单测控制技能集;生产默认 `True`。
- **固化生命周期不变**:builtin = "出厂即 approved";新 skill 仍走 `proposed/→approved/`。

> **§3 注(review 后的修正)**:初版用"启动镜像"(`sync_builtin_skills` 每次启动覆盖刷新 `~/.lyre/skills/builtin/`)。review 指出那是把"config 的拷贝模式"误用到"代码型 skill"上——多一个派生镜像、多一次拷贝。改为**直接读 package**:单一权威源、零拷贝、天然最新。唯一代价是 `read_memory` 多放行一个可信只读根(读 Lyre 自己的出厂代码,非沙箱逃逸;且 `ensure_shipped_facts` 本就按 Path 读 package,一致)。

## 4. 五铁律

- **一**:纯 FS + Python。**三/四**:builtin 是 package 里的只读权威源,无派生态;override 在 `approved/`(global 文件层)。**五**:不涉通信。read_memory 的放行**严格 bounded** 到 `shipped_skills_dir()` 一个只读根。

## 5. 非目标

- **不镜像/不拷贝**进 `~/.lyre`(review 后的核心改动)。
- 不动 proposed→approved 固化流程。
- 不做 builtin skill 的版本/签名校验(MVP:信任 package)。
