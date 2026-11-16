# `/write-paper` v2 —— 通用跨 Agent 理论物理论文写作与确定性审计工作流

*[English](README.md) | [中文说明](README_zh.md)*

一套模块化、支持任意阶段按需重入的理论物理与数学物理 LaTeX 论文工作流与确定性验证工具链，原生兼容 **Claude Code**、**OpenAI Codex CLI**、**Google Antigravity（IDE 与 `agy` CLI）**、**OpenCode**、**Qwen**、**`npx skills add`** 及 **独立 Python CLI**。

与只靠单轮大模型生成或强行拆短句“降 4-gram AI 率”的思路相反，`/write-paper` v2 强调 **真实严谨性与正收益确定性门禁**：**不可变真值与逻辑量词锁表（`00_locks.json` + `--check-quantifiers`）**、**基于本组旧论文的 Few-Shot 语感仿写**、***PRL* / *Nature* 期刊级图件视觉语言 + PyMuPDF/AST 硬门禁**、**双向 DOI + arXiv + `.bbl` 引用审计**、**上下文完全隔离的三方模拟盲审** 与 **带全量快照的局部修补（`repair`）**。

---

## 一键安装

```bash
git clone https://github.com/Misaka16384/liber.git
cd liber
```

### 1. 自动安装到本机所有已装 Agent（推荐）
自动探测并将 `skills/write-paper` 部署到 `~/.claude/skills/`、`~/.codex/skills/`、`~/.agents/skills/`、`~/.gemini/config/skills/` 与 `~/.config/opencode/`：

**Windows (PowerShell)：**
```powershell
.\install.ps1
# 如需顺便安装 Python 依赖（pymupdf, matplotlib, sympy, numpy, Pillow）：
.\install.ps1 -WithDeps
```

**macOS / Linux：**
```bash
sh install.sh
# 或附带安装 Python 依赖：
INSTALL_DEPS=1 sh install.sh
```

**指定宿主 / 软链接模式 / 仅项目级安装：**
```bash
python install.py --hosts all --mode link --check-deps
python install.py --project /path/to/my-paper-repo
```

### 2. Claude Code `/plugin` 或 `skills` CLI
```text
# 在 Claude Code 内通过插件安装：
/plugin marketplace add Misaka16384/liber
/plugin install write-paper@write-paper

# 或使用 npx skills（指向你克隆下来的目录）：
npx skills add /path/to/liber -y
```

---

## 三种运行方式

### 方式一：在 Claude / Codex / Antigravity / OpenCode 对话中自然触发
在论文项目目录打开任意 Agent，直接描述需求或用 `/write-paper` 调用单个阶段：
- *「用 `/write-paper` 的 `inventory` 和 `blueprint` 阶段读取 `./notes` 与 `./SCOPE.md`，按 PRL Letter 规划叙事蓝图。」*
- *「只对 `./paper` 跑 `/write-paper` 的 `figures` 阶段，按 `nature` 风格重构脚本并用 PyMuPDF 验图。」*
- *「对现有成稿 `./paper` 跑一轮上下文隔离的 `blind_referee` 模拟盲审。」*

### 方式二：跨 Agent 命令行总控（支持跨厂商交叉盲审）
直接通过 `scripts/run_paper.py` 调起 `claude`、`agy` 或 `codex` 子进程并发执行，甚至可以在 Claude Code 写稿后指定 `--referee-backend agy` 走异构模型盲审：

```bash
# 调用 Claude Code 子 Agent 完成起草与全套审计
python skills/write-paper/scripts/run_paper.py \
  --mode draft_and_audit \
  --paper-dir ./paper \
  --source-paths ./notes \
  --authority-paths ./SCOPE_AND_TRUTH.md \
  --exemplar-paths ./group_exemplar.tex \
  --genre letter \
  --figure-style prl \
  --backend claude

# 上下文隔离的三方盲审（支持指定另一家 CLI 做独立审稿）
python skills/write-paper/scripts/run_paper.py \
  --mode blind_referee \
  --paper-dir ./paper \
  --backend claude \
  --referee-backend agy

# 带时间戳全量快照的精准局部修复
python skills/write-paper/scripts/run_paper.py \
  --mode repair \
  --paper-dir ./paper \
  --repair-instructions ./paper/audit_reports/05_blind_referee_report.md \
  --sync-bib ./updated_refs.bib
```

### 方式三：Antigravity 原生 `run_workflow`
以 `ScriptPath="<SKILL_DIR>/scripts/paper_workflow.py"` 调用 `run_workflow`，`Args` 见 `scripts/workflow.json`。

**请在规划回合（`Plan mode is on`）里直接调用 `run_workflow`**，即使这个阶段要改文件，也不要先调用 `exit_plan_mode`。原因是：在执行回合里，`inherit` 子 agent 没有计划记录，会卡在 `[plan review due]`。引擎启动时也会读取会话轨迹自行判断回合类型（可用 `Args.plan_mode = "on" | "off"` 手动指定）。若判断为执行回合，本该用 `inherit` 的调用会改走独立的 `agy` CLI 会话；本机没装 `agy` 时退回 `model="pro"`。

### 模型分流（`routing_policy`）
默认 `auto_tiered`：每个 agent 调用按任务档位选模型。
- `heavy`：扫描、审计、盲审、修复规划。
- `builder`：合成、蓝图、起草、作图、修复改稿。
- `fast`：润色、编译修复。
- `probe`：连通性测试。

各运行环境的选择：
- Antigravity 用 `inherit` 或 `flash`。
- `agy` CLI 用 Gemini 模型 ID，每次运行先用 `agy models` 校验一次，模型下线时自动改用 CLI 默认模型。
- `claude` 分别用 `opus`、默认模型、`sonnet`、`haiku`。
- Codex 保持默认配置。

显式指定的 `models[阶段]`（或 `--model`）优先级最高；`--routing-policy uniform_inherit` 则全部使用宿主或 CLI 的默认模型。档位只决定模型，能否写文件由每个调用点单独控制。

---

## 10 个独立可重入阶段（按需接管已有草稿）

| 阶段 (`--mode`) | 核心职责 |
| :--- | :--- |
| `inventory` | 建立 `00_ground_truth_ledger.json`、不可变锁表 `00_locks.json`、`00_style_profile.json` 及 **Lexicon 安全审查 Agent** 放行。 |
| `blueprint` | 检索并核验种子文献、生成 `macros.tex` 与 `01_blueprint.json`，执行 **Stage 1C 蓝图反向核查原始权威文档**。 |
| `draft` | 分批并发（`<=4`）起草，直接按分类 Few-Shot 示例段落仿写自然节奏，并尽早运行 `audit_tex_defs.py`。 |
| `figures` | 按 `prl` / `nature` 视觉规范重构绘图脚本，过 `verify_figures.py` PyMuPDF 印刷字号 + 矢量碰撞 + AST 门禁及图文双向同步。 |
| `verify` | 内部台账/未定义符号审计 + **3 路交叉验证**（直对照原始真值与锁表 + SymPy 反例脚本 + 全局记号对齐）。 |
| `citations` | DOI + arXiv + `.bbl` 双向核查、占位引用补全、以及带 `--allow-cite-changes --check-quantifiers` 差异引导重试的 Claim 保真审计。 |
| `hygiene` | 仅做确定性正收益清理：剥离零宽字符/水印、修复 `rank rank`/`at most <=`/句首 `Sec.`、仅对检出问题段落做定点精细润色。 |
| `build` | `main.tex + supplement.tex` 双编译、检测 `\letterhead` 缺 `\par` 标题串行与 APS 字数/页数，并剥离 `\status{...}` 输出干净的 `main_arxiv.tex`。 |
| `blind_referee` | 三方审稿团（小同行、大同行、期刊编辑）在只含稿件、`.bib`/`.bbl`、图片 PNG 和审计 JSON 的临时副本里工作（CLI 后端下读不到笔记和 `.workflow/`），输出 `05_blind_referee_report.md`。 |
| `repair` | **兜底重入入口**：自动全量快照备份 -> 按 `repair_instructions` / `repair_files` / `sync_bib` 定点修复 -> 跑全套确定性验证与重编。 |

---

## 独立 Python 确定性工具链（零 LLM 依赖即可运行）

所有工具均位于 `skills/write-paper/scripts/tools/`：
- `verify_figures.py` & `journal_fig_theme.py`：PyMuPDF 按实际印刷栏宽核算真实 pt 字号、封禁 `DejaVu`/Type-3、擦字留线检测箭头/曲线穿字碰撞并裁剪 600 DPI 放大图、AST 拦截 10 类 AI 信息图特征。
- `audit_tex_defs.py`：跨正文/SM 检测 `Lemma L*`/`Grade N`/`complex128` 泄露、`\ref`/`\extref` 悬空、PRL 幽灵章节、仅在 SM 定义而正文未定义的符号、以及双记号混用。
- `check_math_integrity.py`：保护所有数学公式、标签与引用不被破坏，并通过 `--check-quantifiers` 防止摘要和定理中的英文逻辑量词被静默篡改。
- `verify_citations.py`：Crossref / INSPIRE-HEP / HTTPS arXiv API 双向比对 DOI 与 `eprint`，校验多作者顺序，扫描渲染后 `.bbl` 的重复 `arXiv:` 与数学符号小写塌缩。
- `clean_watermarks.py` & `style_metrics.py`：提取 Few-Shot 段落、清理隐形字符与 AI 套话、阻止连续 `<=8` 词电报短句。
- `build_and_flatten_tex.py`：跨平台自动定位 TeX Live / MiKTeX 完成双编译、检测行中标题碰撞，并输出无内部路径残留的提交单文件。
