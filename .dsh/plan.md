# npm-riscv64：项目理解与推进计划

> 版本：v7（2026-11-18，Q-f 定案 + 补入测试与验证策略）｜ 状态：**设计待最终确认，尚未改任何代码**
> 口径来源：README + 现有文件通读 + 五轮需求澄清
> 变更记录：见 `.dsh/change-logs/`（本版对应「包名即身份与测试策略-2026-11-18.md」）

---

## 0. 决策台账（已定，实现以此为准）

### 0.1 第一批（D1–D10）

| ID | 问题 | 定案 |
|---|---|---|
| **D1** | 版本全集来源 | **版本目录即全集**：每个版本目录必须有 `.stat`；`patches/`、`commands/` 可有可无。`common` 不参与可构建枚举 |
| **D2** | 合并语义 | **同名文件整体替换**（文件级）；未同名者继承；撤销用 `patches/.disable` |
| **D3** | 命令片段命名 | **固定五名：`fetch`/`patch`/`build`/`test`/`publish`**；未知名即报错 |
| **D4** | `.stat` 格式 | **YAML**；`upstream > source > npm` 逐级降级 |
| **D5** | 发布时机 | build/test 在 PR；publish 在合入 main 后 |
| **D6** | 安全边界 | fork PR 不给 secrets；不用 `pull_request_target` 跑 PR 代码 |
| **D7** | 构建载体 | **容器，固定在 template 内**；默认 qemu/riscv64（native）。2026-11-23 修订：新增 `cross` 型（amd64 交叉工具链构建 + riscv64 容器只跑测试），理由见 D26 |
| **D8** | rolldown | 仅占位（**已决定删除该目录**，见 D20） |
| **D9** | `scripts/**` 变更 | 仅渲染自检（2026-11-19 修订：同时把渲染器视为**全局构建输入**，配方先入候选、再按 base/head 的构建 key 差异收窄，见 §13.11） |
| **D10** | 矩阵限流 | `max-parallel: 3~4` |

### 0.2 第二批（D11–D17）

| ID | 原编号 | 定案 |
|---|---|---|
| **D11** | Q1 | **不允许** `common/.stat`（`common` 只有 `commands/`、`patches/`） |
| **D12** | Q2 | `test` 缺失 = **不合格**；`build` 与 `test` 均必需 |
| **D13** | Q3 | `publish` 同 fetch/patch 策略：由元数据生成，配方可覆盖（具体形态见 **D18**） |
| **D14** | Q4 | 首个真包 = 纯 C 的小原生模块 |
| **D15** | Q5 | `source` 按**失败**处理：源码必须能定位到具体 ref |
| **D16** | Q6 | main 侧**复用 PR artifact**，不重复构建（落地为内容寻址；key 口径 2026-11-19 改为「渲染后的构建命令」，见 §13.11） |
| **D17** | Q7 | **一条**工作流文件承载两种触发 |

### 0.3 第三批（D18–D21）

| ID | 问题 | 定案 |
|---|---|---|
| **D18** | **发布模型** | npm 多架构包的标准形态是**两类包**：**A = 平台二进制包**（含 `.node`），**B = 纯 JS 包**（通过 `optionalDependencies` 引用 A）。对 A、B 的改动**都体现在 patch 中**（含新增 A 包的文件）。publish 阶段可能需要**发布两个包**：新增/更新的 A，以及更新后的 B |
| **D19** | Q-new-2 | commit SHA 形式的 `ref` **不需要额外约束**，只要满足「不可变」即可（不要求等于某 tag 指向的提交） |
| **D20** | Q-new-3 | **删除** `packages/rolldown/` 占位目录（D12/D13 下它必然渲染失败，占位语义交给样例配方） |
| **D21** | Q-new-1 / Q-new-4 | 首包按草案建议：`bufferutil`（主选）／`utf-8-validate`（备选）；artifact retention = 90 天 |
| **D22** | Q-a | **清单的生成发生在 publish 阶段**。A 的清单**不预设由 patch 提供**：由 patch 给出、或由渲染器在 publish 阶段生成，两条路都合法，`generate_manifest: true` 即「生成兜底」开关。这与 fetch/patch/publish「配方未提供则生成」是同一原则 |
| **D23** | Q-b | B 包同理：`generate_manifest: true` 且配方未提供 `publish` 时，渲染器**基于已有 `package.json` 更新生成**（不是从零创建），其余字段原样保留 |
| **D24** | Q-c / Q-d / Q-e | 采纳草案建议：发布演练先 verdaccio 再真发；只有 A 无 B 合法；首包 `bufferutil`（需严格纯 C 时换 `node-expat`） |
| **D25** | Q-f | **包名即身份**：update 模式下清单 `name` 与 `.stat` 不一致一律**失败**，不覆盖、不补齐。理由：改名发出去的不是「更新」而是「另一个包」，原包使用者拿不到 riscv64 支持 —— 与「让开发者无感获得 riscv64 支持」直接相悖。`version` 相反，由 `.stat` 决定并写入 |
| **D26** | Q-rolldown | **工具链两型**：`toolchain: native`（默认，qemu 容器里编译，D7 原义）与 `toolchain: cross`（amd64 镜像里交叉编译 riscv64，**测试仍在 riscv64 容器**）。选择依据是实测：rolldown 的 Rust 工作区交叉编译 3m21s 出 19 MB 绑定，而同一份源码在 qemu 下 10 分钟连一个 crate 都没编译完（§13.12）。交叉不牺牲验证强度——产物必须在目标 CPU 上真实加载并打包一次 JS |

### 0.4 规范（N1）

| ID | 规范 |
|---|---|
| **N1** | **国际化**：`README.md` 与全部代码（脚本注释、workflow 注释）一律**英文**；`.dsh/**` 不受约束 |

---

## 1. 项目定位

> Including patches, compile commands and so on for constructing riscv64 binaries for npm packages.

仓库是**配方库 + 构建编排**：配方描述「某个上游 npm 包在某个版本上，怎么取源码、打什么补丁、怎么编、怎么测、怎么发」，CI 按变更挑配方执行。

---

## 2. 现状快照

**初始快照（2026-11-18 阅读仓库时）**

| 文件 | 字节 | 角色 | Git 状态 | 语言合规 |
|---|---|---|---|---|
| `README.md` | 112 | 定位说明 | 已提交（`73e82d0`） | ✅ 英文 |
| `.github/workflows/template.yml` | 167 | 渲染模板 | 未跟踪 | ✅ 无注释 |
| `.github/workflows/pr-run.yml` | 1066 | PR 主工作流 | 未跟踪 | ✅ 无注释 |
| `scripts/render.py` | 1916 | 占位符渲染器 | 未跟踪 | ❌ L10、L28 中文 |
| `scripts/detect_targets.py` | 1388 | 变更包发现 | 未跟踪 | ❌ L12、L22 中文 |
| `packages/rolldown/` | — | 空占位目录 | 不跟踪 | — （**已删除**，D20） |

**当前快照（2026-11-18 第一轮实现后，详见 §13）**：模板迁到 `.github/templates/action.template.yml`；
两个旧工作流合并为 `.github/workflows/build.yml`；`scripts/` 三个脚本全量重写（英文注释）；
`packages/@ruyisdk/bufferutil/4.2.0/` 为首个真实配方；新增 `docker/`、`docs/`、`tests/`。
工作树内容仍未提交（`git ls-files` 只有 `README.md`），提交时机由你决定。

---

## 3. 配方契约

### 3.1 目录树

```
packages/
  <pkg>/                             # 无 scope，如 rolldown
    common/                          #   通用基线（不可构建；无 .stat）
      commands/                      #     可选：fetch/patch/build/test/publish
      patches/                       #     可选：*.patch + 可选 .disable
    <version>/                       #   可构建版本
      .stat                          #     必需
      commands/                      #     可选：同名覆盖 common
      patches/                       #     可选：同名覆盖 + 追加
  @scope/                            # scope 容器（本身不是包）
    <name>/
      common/
      <version>/
        .stat
        commands/
          publish                    #     覆盖生成的 publish
        patches/
          .disable
```

**包名 → 路径**：`<pkg>` 按 `/` 拆开落盘（`@scope/name` → `packages/@scope/name/`）。
**枚举**：`packages/*/` 不以 `@` 开头 = 无 scope 包；以 `@` 开头 = scope 容器，其子目录才是包。每个包下除 `common` 外的每个子目录 = 一个可构建版本。

### 3.2 合并语义（D2）

| 维度 | 规则 |
|---|---|
| 命令片段 | 版本目录 `commands/<name>` 存在 → **整体替换** common 的；否则继承 |
| 补丁 | 同名 → **整体替换**；不同名 → **追加**（common 组在前，组内按名排序） |
| 撤销 | `patches/.disable` 每行一个文件名，命中者不应用（common 与版本并集） |
| 点文件 | `patches/` 下点开头文件不算补丁 |
| 元数据 | `.stat` 只在版本目录出现（D11），不参与合并 |

### 3.3 `.stat` 元数据

```yaml
upstream:                          # 优先级 1
  repo: https://github.com/owner/repo
  ref: v1.0.0                    # tag 或 commit SHA（不可变，D15/D19）
  subdir: packages/binding       # 可选

source:                            # 优先级 2
  url: https://example.com/pkg-1.0.0.tar.gz   # 版本固定 URL
  sha256: <64-hex>                 # 必填（D15）

npm:                               # 优先级 3
  name: "@scope/binding"
  version: 1.0.0
  registry: https://registry.npmjs.org   # 可选

artifacts:
  path: "*.node"                   # build 产物（test 依赖）

# 发布清单（D18）：有序列表，可含 1~N 个包；binary 必须先于引用它的 main
npm_publish:
  - role: binary                   # A：平台二进制包（新增或更新）
    name: "@scope/binding-linux-riscv64-gnu"
    version: 1.0.0
    dir: packages/binding-linux-riscv64-gnu    # 相对源码根；可由 patch 创建
    registry: https://registry.npmjs.org
    token_env: NPM_TOKEN
    os: linux                      # 与 role=binary 配套；生成清单时写入
    cpu: riscv64
    generate_manifest: true        # true = 清单由 publish 阶段生成（缺则创建）；false = 必须由 patch 提供

  - role: main                     # B：纯 JS 包
    name: "@scope/binding"
    version: 1.0.0
    dir: .
    registry: https://registry.npmjs.org
    token_env: NPM_TOKEN
    requires: ["@scope/binding-linux-riscv64-gnu"]   # 决定发布顺序与一致性处理对象
    generate_manifest: true        # true = 基于已有 package.json 更新生成（补齐 requires 指向）
```

**清单生成时机与三档优先级（D22 + D23）**

```
① commands/publish 由配方提供  → 完全交给配方；generate_manifest 被忽略（渲染器不碰清单）
② 无配方片段 + generate_manifest: true  → 渲染器生成发布步骤，并在 publish 阶段生成/更新清单
③ 无配方片段 + generate_manifest: false → 渲染器生成发布步骤，但只校验清单，缺文件或不一致即失败
```

生成时的两种模式由**文件是否存在**自动判定（不需要额外字段）：

| 模式 | 触发 | 行为 |
|---|---|---|
| **create** | `dir` 下无 `package.json` | 用 `.stat` 生成最小清单：`name` / `version` / `main`（产物文件名）/ `os` / `cpu`（后两者仅 binary） |
| **update** | `dir` 下已有 `package.json` | **读入 → 只改需要的字段 → 写回**：`version` 对齐 `.stat`；`optionalDependencies[requires[0]] = A.version`。**其余字段原样保留** |

> update 必须是真正的 JSON 合并（`jq` / `node -e`），**不能 heredoc 整文件重写** —— B 包原有的 `dependencies`、`exports`、`files` 等字段一旦被覆盖，发出去的就是个残废包。

> **包名即身份（D25）**：update 模式下清单里的 `name` **必须**与 `.stat` 声明一致，不一致一律**失败** —— 不覆盖、不补齐、不跳过。改名发出去的不是「更新」而是一个**新包**：原包的使用者依然拿不到 riscv64 支持，还会多出一个无人引用的孤儿包，与「让开发者无感获得 riscv64 支持」的目标直接相悖。`version` 则相反，由 `.stat` 决定并**写入**（那是本次构建的目标版本，不是身份）。

**一致性的责任随档位转移**：

- 档位 ②（生成）：一致性由**写入**保证 —— 清单字段直接来自 `.stat`，事后无需比对。
- 档位 ③（不生成）：一致性靠**发前断言** —— 不一致即失败。

**ref / 完整性约束（D15 + D19）**

- `source.sha256` **必填**，缺失即硬失败。
- `upstream.ref` 与 `source.url` 中的 ref 必须**不可变**：拒绝 `main`/`master`/`develop`/`HEAD`/`latest` 等常见可变引用名；commit SHA 无需额外约束（不要求等于某 tag 指向的提交）。
- `npm` 途径天然版本固定（`name@version` → `dist.tarball`）。

**npm 途径的能力边界**：需 ① 通过 registry **元数据 API 解析 `dist.tarball`**（`GET https://registry.npmjs.org/<URL 编码后的 name>` → `versions[<ver>].dist.tarball` / `.integrity`），**不自行拼 URL**；② scoped 名 URL 编码（`@scope%2Fname`）。**局限**：registry 上是发布产物，原生模块包常只含预编译 `.node` + JS 包装层，**可能不含可编译源码** —— 故 npm 只作最后兜底，失败信息须提示这一原因。解包需 `--strip-components=1`。

### 3.4 结构 vs 数据

- **结构（模板持有）**：五步顺序、容器与 qemu、校验、发布门禁、YAML 形状。
- **数据（配方持有）**：`.stat` 元数据、补丁（**含 A/B 两包的改动**）、五个片段。
- **交界**：`fetch`/`patch`/`publish` 未提供同名片段时由渲染器生成（`publish` 的生成深度由 `generate_manifest` 决定，见 §6.4）；`build`/`test` 必须由配方提供（D12）。

---

## 4. target 模型与变更映射

### 4.1 target 标识

```json
[{"pkg":"rolldown","ver":"1.0.0","dir":"packages/rolldown/1.0.0"}]
[{"pkg":"@scope/binding","ver":"1.0.0","dir":"packages/@scope/binding/1.0.0"}]
```

**slug 规则**（job 名 / artifact 名 / 渲染目录共用）：`slug(pkg)` = 去前导 `@`、`/`→`-`；`slug(ver)` 原样。

| 用途 | 形态 |
|---|---|
| job 名 | `build (scope-binding @ 1.0.0)` |
| artifact 名 | `scope-binding-1.0.0-riscv64` |
| 渲染目录 | `.rendered/scope-binding/1.0.0/action.yml` |

**⚠️ YAML 引号规则**：`@` 是 YAML 保留指示符，标量以它开头即非法 → 模板中凡以变量开头的标量一律加引号（`name: "{{npm.name}}"`）。

### 4.2 变更 → 目标 映射

| 变更路径 | 展开为 |
|---|---|
| `packages/<pkg>/<ver>/**` | `<pkg>/<ver>` |
| `packages/@scope/<name>/<ver>/**` | `@scope/<name>/<ver>` |
| `packages/<pkg>/common/**`（含 scoped） | 该包**全部**版本 |
| 新增版本目录 | 该版本首次构建；缺 `.stat` → detect 阶段失败 |
| 仅删除版本目录 | 无目标，日志提示「已下线」 |
| `template.yml` | 全量**候选**，再按 base/head 构建 key 差异收窄（§13.11） |
| `scripts/**` | 全量**候选** + 渲染自检；同样按 key 差异收窄（D9 修订） |
| `docker/**` | **全量**（镜像 digest 要到构建侧才知道） |
| 仅文档 / 仅工作流 | 无目标 |

**解析规则**：`packages/` 后首段以 `@` 开头 → 包名 = 段1/段2、版本 = 段3；否则包名 = 段1、版本 = 段2。版本段为 `common` 时按上表展开。

### 4.3 边界

- 同 PR 同时改 common 与版本目录 → 去重。
- 版本目录缺 `.stat` → 硬失败。
- `@scope` 容器目录下出现文件（而非子目录）→ 结构错误。
- **一个 (pkg, ver) 只产出一个构建目标**，但该目标可能需要发布 1~2 个包（A/B，D18）—— 构建维度不因 A/B 增加。

---

## 5. 触发与 CI 数据流

单文件、双触发（D17）：

```
① pull_request
   detect : git diff --name-only origin/$BASE_REF...$HEAD_SHA
   build  : fetch → patch → build → test          （publish 条件跳过）
   upload : artifact `<slug>-<ver>-riscv64`（名内含 head SHA，retention 90d）
   secrets: 不需要

② push: branches: [main]
   detect : git diff --name-only $BEFORE..$AFTER（before 全零 → 全量）
   reuse  : 定位 PR 侧产物并下载，失败/过期则回退重建
   publish: 按 `npm_publish` 清单发布（A → B，幂等）
   secrets: 需要
```

### 5.1 artifact 复用的定位链（D16）

> **实现变更（2026-11-19）**：实际落地的不是下面这条「合并提交 → PR head → run」定位链，
> 而是**内容寻址**：把决定产物的输入哈希成 key 并写进 artifact 名，命中即等价、无需 diff 校验，
> 也顺带覆盖直推 main 与重跑同一 commit 的情况。详见 §13.9。下面保留原始设计以备对照。
>
> **口径修订（2026-11-19 晚）**：key 不再哈希「配方 + 模板 + scripts + docker」这些**文件**，
> 而是哈希**渲染之后真正会执行的命令**（fetch/patch/build 三个步骤的 `run` 块 + `steps/*.sh`
> + 补丁内容 + 镜像 ID）。原因：模板加一个 `reuse` 输入或 `if:` 守卫会改文件但不会改任何一条被
> 执行的命令，产物必然相同；旧口径却因此整册失效。同一口径也让 detect 能回答「这条配方真的受影响
> 吗」，见 §13.11。

```
merge_sha = github.sha
  → gh api repos/{owner}/{repo}/commits/{merge_sha}/pulls  → 已合并 PR 的 head SHA
  → gh run list --commit <head_sha> --workflow <name>      → run id
  → gh run download <run_id> -n <slug>-<ver>-riscv64
```

**双重回退（必须实现）**：

1. **定位失败** → 重建（直推 main、强推、PR 未跑过构建、超 90 天保留期）。
2. **有效性校验** → 产物来自 PR head 树，而发布用合并后树；若
   ```
   git diff <pr_head_sha>..<merge_sha> -- packages/<pkg>/ .github/workflows/template.yml
   ```
   非空 → 判为过期，重建。（两个 PR 先后合入时，后者产物可能基于前者合入**前**的 common 补丁。）

复用的产物同样要过 §6 的校验（含架构断言），不能默认可信。

---

## 6. 五步 composite action

### 6.1 五步 ↔ 五片段

| # | 步骤 | 片段 | 必需性 | 生成 |
|---|---|---|---|---|
| 1 | fetch upstream | `fetch` | 三途径至少一条可解析 | ✅ |
| 2 | apply patches | `patch` | 无补丁时整步省略 | ✅ |
| 3 | build | `build` | **必需**（D12） | ❌ |
| 4 | test | `test` | **必需**（D12） | ❌ |
| 5 | publish | `publish` | `npm_publish` 可生成 或 配方提供 | ✅（D18 多包） |

优先级：`commands/<name>`（版本）> `commands/<name>`（common）> 渲染器生成（fetch/patch/publish）。
`commands/` 下出现五名以外文件 → 渲染失败。

### 6.2 fetch（三途径降级）

```bash
if ! fetch_from_upstream; then
  if ! fetch_from_source; then
    fetch_from_npm || { echo "all source routes failed: upstream/source/npm" >&2; exit 1; }
  fi
fi
```

- `upstream`：tag/commit 两种情况分别生成 clone 参数；可选 `subdir` 作后续工作目录。
- `source`：`curl -fsSL` + `sha256sum -c` + `tar xz --strip-components=1`。
- `npm`：元数据 API → `dist.tarball` + `.integrity` → 下载校验 + 解包。
- 三途径全失败时**分别列出各自原因**。

### 6.3 patch

按 §3.2 组装名单 → 逐个 `git apply -p1 --3way`，失败即停并打印拒绝详情。

> 注意：A 包（平台二进制包）通常**由 patch 新增**（目录 + `package.json`），B 包的 patch 则改 `optionalDependencies` 加一行指向 A。因此 patch 必须能**创建新文件**（`git apply` 天然支持）—— 这一点要在 `docs/recipe-spec.md` 里写明。

### 6.4 publish（D13 + D18 + D22/D23：可能发两个包，清单可能由渲染器生成）

**顺序有实质意义**：**先 A 后 B**。若先发 B，则存在「B 已可安装但 A 尚不存在」的窗口，`npm install B` 在该窗口内会因 `optionalDependencies` 解析失败而退化；先发 A 可保证 B 一旦可见就一定可安装。

生成的 publish 步骤：

```bash
publish_one() {   # $1=dir $2=name $3=version $4=registry $5=token_env
  # ① 档位②：清单 create（缺文件）| update（JSON 合并，保留其余字段）
  # ② 档位③：断言 name/version/os/cpu 与 .stat 一致、requires 边一致
  # ③ 幂等：npm view <name>@<version> 命中 → 跳过
  # ④ 发布：npm publish <dir> --registry <registry> --access public
}

# 依赖序：role=binary 在前；再按 requires 建边拓扑排序
publish_one <A.dir> ... 
publish_one <B.dir> ...
```

**交叉校验的责任按档位分配（详见 §3.3 三档表）**：

| 校验 | 档位 ②（生成） | 档位 ③（不生成） |
|---|---|---|
| A 清单 **`name`** 与 `.stat` 一致（身份，D25） | **两档都做**：update 模式读入既有 `name` 比对，不一致即失败 | 必须断言 |
| A 清单 `version` 与 `.stat` 一致 | 写入即成立，无需事后再比 | 必须断言 |
| A 的 `os`/`cpu` 实际生效 | 写入 `os`/`cpu` 字段 | 必须断言 |
| 产物架构确实是 RISC-V（`file` 断言） | **仍必须断言**（生成清单不保证产物对） | **仍必须断言** |
| B 的 `optionalDependencies[A.name] == A.version` | 由 update 写入，**天然成立** | 必须断言，否则版本漂移静默通过 |

> 注意「写入即成立」不等于「不用校验」：清单由渲染器写，产物却是编译出来的 —— 架构断言两档都要做。

**时序陷阱：test 早于 publish**

清单更新发生在 publish 阶段，而 test 在更早的步骤运行。届时 B 的 `optionalDependencies` 尚未指向本次构建的 A —— 若 test 需要验证 A/B 联动，配方应在 `commands/test` 内自行把 A 目录链入（如 `npm install ./<A.dir>`），不能指望 publish 阶段的清单更新。

**部分失败可重入**：A 已发、B 失败 → 重跑时 A 命中间等跳过、只补 B。因此幂等检查不能省。

### 6.5 容器（D7）

模板内自持容器（写在模板里才能实现「改模板 → 全量重建」）：

```yaml
    - name: setup qemu
      uses: docker/setup-qemu-action@v3
    - name: <step>
      shell: bash
      run: |
        docker run --rm --platform linux/riscv64 \
          -v "$GITHUB_WORKSPACE:/w" -w /w -e NPM_TOKEN \
          ${RISCV_IMAGE} bash -lc '<片段内容>'
```

工作区 bind mount 共享 → 五步各自 `docker run --rm` 也能保留状态。

### 6.6 失败策略

| 步骤 | 失败处理 |
|---|---|
| fetch | 逐级降级；三条全失败 → 失败并列原因 |
| patch | 任一补丁失败即停 |
| build / test | 失败即停（不发布半成品） |
| publish | 校验失败即停；已发布者跳过；部分失败可重入 |
| 复用产物 | 定位失败 / 校验不过 → 回退重建（不算失败） |

---

## 7. 渲染器能力清单

| # | 能力 | 现状 | 优先级 |
|---|---|---|---|
| R1 | **缩进感知替换**（多行值按占位符列补齐前缀） | ❌ 纯 `re.sub`，多行必产出非法 YAML（已实测） | P0 |
| R2 | 两层叠加（同名整文件替换 + 补丁追加 + `.disable`） | ❌ | P0 |
| R3 | `.stat` YAML 解析（含 `npm_publish` 列表） | ❌ | P0 |
| R4 | 五片段固定名 + 未知名报错 + `build`/`test` 必需校验 | ❌ | P0 |
| R10 | **scoped 支持**：`@scope/name` 解析、slug、`@` 开头标量加引号 | ❌ | P0 |
| R5 | `fetch` 生成（三途径降级） | ❌ | P1 |
| R6 | `patch` 生成（排序、替换、`.disable`、`--3way`） | ❌ | P1 |
| **R7** | **多包 publish 生成**：三档优先级、清单 create / update(JSON 合并)、一致性（写入保证或发前断言）、依赖序拓扑、幂等、`--access public`、可重入 | ❌ | P1 |
| R8 | 必填校验：缺 `.stat` / 缺 `build` / 缺 `test` / `source` 无 `sha256` / 可变 ref / `npm_publish` 为空 / **清单 `name` 与 `.stat` 不一致（D25）** | ❌ | P1 |
| R9 | 可选步骤省略（无补丁时整步不渲染） | ❌ | P1 |
| R11 | `rstrip("\n")` 与块标量语义对齐 | ⚠️ | P1 |
| R12 | `--check-all` 渲染自检（D9） | ❌ | P1 |
| R13 | 事件模式适配（PR 三点 / push 区间、`before` 全零） | ❌ | P1 |
| R14 | 输出「目标 → 输入路径集合」供复用校验 diff | ❌ | P1 |
| R15 | `--warn-unused` | ❌ | P2 |

---

## 8. 与现状的差距

### P0

- **P0-1 渲染输入对不上**：`pr-run.yml:40` 传 `--input-dir inputs/<target>`，配方在 `packages/...` → `render.py:51-52` 直接 `SystemExit`；还需改为 `--pkg/--ver`。
- **P0-2 多行渲染出非法 YAML**（已实测）。
- **P0-3 target 未升级**：只取路径第 2 段；不产出 `{pkg, ver}`；不展开 common；不支持 push 区间；不支持 scoped。
- **P0-4 scoped 支持缺失**。

### P1

- **P1-1 五步骨架缺失**（模板只有 `echo`）。
- **P1-2 `.stat` 未实现**（含 D15 的 sha256/ref 约束）。
- **P1-3 无发布能力**：尤其**无 A/B 双包模型**（有序清单、清单 create/update、发布顺序、一致性处理、幂等、可重入）。
- **P1-4 无 riscv64 载体**。
- **P1-5 `template.yml` 位置**：会被当工作流解析（无 `on:`）→ 移至 `.github/templates/action.template.yml`，同步 `detect_targets.py:6`、`pr-run.yml:39`、渲染器默认值。
- **P1-6 artifact 复用缺失**（定位链 + 双重回退）。
- **P1-7 语言整改（N1）**：`render.py:10/28`、`detect_targets.py:12/22` 四处中文；新增代码一律英文。
- **P1-8 删除 `packages/rolldown/` 占位目录**（D20）。

### P2

矩阵命名与限流、产物架构断言、`.gitignore`（`.rendered/`、`__pycache__/`）、配方文档与示例（含「patch 如何新增 A 包」）、qemu 层缓存（`/root/.cargo`、`target/` 放宿主机路径）。

---

## 9. 阶段计划

| 阶段 | 内容 | 验收 | 状态 |
|---|---|---|---|
| **S0** | 样例配方（unscoped + scoped 各一）；`docs/recipe-spec.md`（英文）；删除 rolldown 占位 | 走查通过：`.stat` 三途径、`source` 带 sha256、common 一补丁 + 版本一补丁 + `.disable`、`build`/`test` 齐备、`npm_publish` 覆盖 **create（A）与 update（B）两种清单模式**；文档写明「patch 可新增 A 包」 | **完成（裁剪）**：真实配方只做 `@ruyisdk/bufferutil`（单包、upstream 一条路线、`generate_manifest: true`）；三途径降级、`source` sha256、common 补丁 + `.disable`、A/B 双包与 `verify` 档位改由 L0 夹具与 L2 假配方覆盖（理由见 §13.3） |
| **S1** | 渲染器 R1–R4、R8–R11 | 渲染样例 → `yaml.safe_load` 通过；多行缩进正确；scoped 合法（`@` 开头标量加引号）；缺 test/sha256 硬失败；`--check-all` 全绿 | **完成**：`--check-all` 全绿；渲染结果自动过 YAML 校验 + 结构断言；scoped 未加引号会被自己解析出来报错 |
| **S2** | detect 升级 R13 | 单测覆盖 §4.2 全表（含 scoped）；push（`before` 全零）场景 | **完成**：§4.2 全表 + scoped + 版本目录删除 + 全零 `before` 均有单测 |
| **S3** | fetch/patch 生成 + 五步端到端 | PR 跑通 `bufferutil`；upstream 失效 → 降级 source；三途径全失效 → 列出三条原因 | **完成（本机）**：L1 实跑 bufferutil 全五步（含发布到本地 registry）；降级链与「三途径全失败」由生成脚本保证，真 CI 复验见 L3 |
| **S4** | 容器 + 复用 + **A/B 双包发布** | qemu 固定进模板；artifact 复用（含过期判定）；发布顺序 A→B 可验证；update 不丢字段；漂移拦截；不重复发布；部分失败可重入 | **部分完成**：容器与 qemu 固定进模板 ✅；A→B 顺序、幂等、update 不丢字段、`verify` 档位拦截改名、部分失败可重入 ✅（L2）；**artifact 复用尚未接线**（只上传，不复用，留给 L3 一并调） |
| **S5** | 规模化：第二个包、限流、缓存、文档 | 多包多版本同 PR 变更矩阵正确；缓存命中可见 | **未开始** |

---

## 10. 开放项（已全部收敛）

| ID | 问题 | 结论 |
|---|---|---|
| **Q-a** | A 的清单由谁提供 | **D22**：不预设由 patch 提供。生成发生在 **publish 阶段**，由 `generate_manifest: true` 兜底；patch 直接提供亦可，两者不冲突 |
| **Q-b** | B 的 `optionalDependencies` 由谁改 | **D23**：同一原则。`generate_manifest: true` 且无配方 `publish` 时，渲染器**基于已有 `package.json` 更新生成**（只改需要的字段） |
| **Q-c** | 发布演练目标 | **D24**：先 verdaccio 演练，再对测试 scope 真发 |
| **Q-d** | 只有 A 没有 B | **D24**：合法，`npm_publish` 只列一项，不做 B 侧处理 |
| **Q-e** | 首包是否严格纯 C | **D24**：`bufferutil` 主选；需严格 C 则换 `node-expat`（C + libexpat） |
| **Q-f** | 清单已存在但 `name` 与 `.stat` 不一致（update 模式） | **已定（D25）：直接失败**。包名是身份，改名 = 发新包，使用者拿不到 riscv64 支持，违背「无感」原则 |

---

## 11. 待 CI 实测的技术风险

| ID | 风险 | 备选 |
|---|---|---|
| R-a | `docker/setup-qemu-action` 在托管 runner 上的 riscv64 支持 | 镜像内置 qemu-user-static + `--platform` |
| R-b | qemu 模拟性能（5–20×），Rust 包可能逼近 6h | 首包选纯 C（D14）；**2026-11-23 修订**：Rust 包不再等自建 runner——改走 cross（D26/§13.12） |
| R-c | `dist.tarball` 指向 CDN/私有镜像 | `.stat` 用显式 `source.url` 覆盖 |
| R-d | artifact 被清理 / PR 未跑过构建 | §5.1 回退重建 |
| R-e | 复用产物基于 PR head 树而非合并树 | §5.1 有效性校验 diff |
| R-f | scoped 名的 `@` 在 YAML/路径/artifact 名中的特殊行为 | slug + 引号规则（R10） |
| **R-g** | **A/B 版本漂移**：B 的 `optionalDependencies` 指向 A 的旧版本 | 档位③发前断言拦截；档位②由 update 写入消除 |
| **R-h** | **发布顺序**：先发 B 会让「B 可装但 A 缺失」的窗口期存在 | §6.4 强制 A→B，并做幂等以支持重入 |
| **R-i** | **update 模式丢字段**：用 heredoc/整文件重写 B 的 `package.json` 会清掉 `dependencies`、`exports`、`files` 等 | §6.4 要求 JSON 合并（`jq`/`node -e`）；S4 验收比对非目标字段前后一致 |
| **R-j** | **test 早于 publish**：test 运行时 B 的 `optionalDependencies` 尚未指向本次 A，联动测试可能误判 | §6.4 时序说明；配方在 `commands/test` 内自链 A 目录 |
| **R-k** | 档位②「写入即成立」被误解为「无需校验」 | §6.4 明确：架构断言两档都做 |

---

## 12. 测试与验证策略

四层门禁，顺序 **L0 → L1 → L2 → L3**。L0–L2 全部可在本机闭环（能力已实测，见 §12.5），只有 L3 必须真 CI。

### 12.1 层次定义

| 层 | 覆盖对象 | 环境要求 | 能否替代 L3 |
|---|---|---|---|
| **L0 静态与单元** | 渲染器 R1–R4 / R7–R11、detect R13、生成片段语法 | 本机 Python + git，**零外部依赖** | 否 |
| **L1 片段真实执行** | fetch 三途径与降级、patch 应用、build 产物、架构断言 | 本机 docker + qemu binfmt + 网络 | 否 |
| **L2 发布语义** | 顺序 A→B、幂等、update 不丢字段、漂移拦截、可重入 | 本机 verdaccio（或 docker 版） | 否 |
| **L3 真 CI** | 见 §12.4 的 7 件事 | **GitHub Actions（需你提供仓库）** | — |

### 12.2 L0：静态与单元（可立即开始）

- **工具**：stdlib `unittest`（本机无 pytest；不为两个脚本引入依赖链，等需要夹具/参数化再考虑 pytest）。
- **夹具**：`tests/fixtures/<case>/`（配方输入）+ `tests/expected/<case>/action.yml`（golden）。
- **断言**：
  1. 渲染结果 `yaml.safe_load` 通过 —— **这是 P0-2「多行渲染出非法 YAML」的回归闸门**；
  2. steps 的数量/名称/顺序/`if:` 门禁（publish 在 PR 下被跳过）；
  3. 硬失败路径逐条：缺 `test`、缺 `.stat`、`source` 无 `sha256`、可变 ref、未知片段名、`npm_publish` 为空、清单 `name` 不一致（D25）；
  4. 叠加语义：同名整文件替换、补丁追加顺序、`.disable`、点文件排除；
  5. scoped：`@scope/name` 解析、slug、`@` 开头标量加引号、`.rendered/<slug>/<ver>/`；
  6. detect：临时 git 仓构造 PR 三点 diff 与 push 区间 diff（含 `before` 全零 → 全量）—— 此法已在现状脚本上验证可行；
  7. 生成的 shell 片段 `bash -n` 语法检查（shellcheck 本机缺失，可选后装）。
- **出口**：`--check-all`（R12）在全部夹具上全绿。

### 12.3 L1 / L2 明细

**L1（片段真实执行）**：从渲染出的 action.yml 中抽出每个 step 的 `run` 块，逐块执行

```bash
docker run --rm --platform linux/riscv64 -v "$PWD:/w" -w /w <image> bash -lc '<step run>'
```

用例：① upstream 正常取源（含 `subdir`）；② upstream 失效 → 降级 `source` 且 sha256 命中；③ 三途径全失效 → 失败信息含三条原因；④ 补丁正常应用 / 冲突时失败即停；⑤ build 产出 `.node` + `file` 断言为 RISC-V。

**L2（发布语义）**：本机起 verdaccio，构造最小上游树 + 两个包（A 用 create、B 用 update）。用例：① A 先于 B 发布；② 连跑两次，第二次全部命中等同跳过；③ update 前后 `package.json` 的**非目标字段逐字节不变**（R-i）；④ 手改 B 的 `optionalDependencies` 成旧版本 → 档位③失败（R-g）；⑤ A 成功、B 失败后重跑只补 B（可重入）。

### 12.4 L3：只能在真 CI 验证的 7 件事

1. `docker/setup-qemu-action@v3` 在**托管 runner** 上对 riscv64 是否就绪（R-a）。
2. job 名 / artifact 名含 scoped（`@`）与 semver（`.`, `-`, `+`）时 GitHub 是否接受。
3. `uses: ./.rendered/<slug>/<ver>` 这种**嵌套相对路径**本地 action 是否被接受（当前是假设）。
4. artifact 跨 run 复用：`commits/{sha}/pulls` → `gh run list --commit` → `gh run download`。
5. PR / main 双触发与 secrets 门禁（fork PR 无 secret）。
6. qemu 下的**真实耗时** vs 6h 上限（R-b；本机是 x86 CPU + 自有 binfmt，不代表 runner）。
7. `max-parallel` 实际并发与矩阵可读性。

### 12.5 本机能力实测（2026-11-18）

| 项 | 结果 |
|---|---|
| docker | ✅ 29.1.3（linux/x86_64, overlayfs） |
| qemu binfmt | ✅ `qemu-riscv64` 已注册（`/proc/sys/fs/binfmt_misc/qemu-riscv64`，flags `POCF`） |
| riscv64 容器 | ✅ 实测 `docker run --platform linux/riscv64 debian:trixie-slim uname -m` → `riscv64` |
| 网络 | ✅ `registry.npmjs.org` 200；`git ls-remote` GitHub 通 |
| jq / node / npm | ✅ jq 1.8.1 / node 22.22.1 / npm 9.2.0（L2 的 JSON 合并可本地验） |
| 缺失 | ❌ pytest ❌ gh ❌ shellcheck ❌ act（均非必须：unittest 替代、gh 仅 CI 需要、shellcheck/act 可选） |

结论：**L0–L2 可在本机闭环，L3 才需要 GitHub**。

### 12.6 需要你提供什么（最小清单）

| 项 | 需要吗 | 说明 |
|---|---|---|
| **私有 npm registry** | **不需要** | 演练用本地 verdaccio；端到端建议用 **GitHub Packages**（仓库自带 `npm.pkg.github.com`，只靠 `permissions: packages: write` + 内置 `GITHUB_TOKEN`，零额外维护），或公共 npm + 测试 scope + granular token。**只有**当你希望发布目标就是内部 registry 时，才需要它的地址 + token + 允许发布的 scope |
| GitHub 仓库（可跑 Actions） | **需要** | 建议**临时验证仓或 fork**，不要直接在 `RuyiRepo/npm-riscv64` 上推实验 workflow；稳定后再走 PR 合入主仓 |
| `NPM_TOKEN` | 视目标 | 选公共 npm 时需要（granular token，**限定测试 scope**）；选 GitHub Packages 时不需要 |
| riscv64 镜像来源 | 有更好 | Docker Hub 已可用；内网有加速/代理的话给地址 |
| 首包上游可达性 | 需要确认 | `bufferutil`（GitHub 可达）；若要求走内网镜像，给地址 |

### 12.7 排期建议

```
L0 ──▶ L1 ──▶ L2 ──▶ 交付一版可跑 workflow ──▶ L3（临时仓验证 7 项）──▶ 合入主仓
```

L0/L1/L2 不阻塞于环境申请，可以立即开始；L3 需要仓库就绪后才有意义。

---

## 13. 第一轮实现进展（2026-11-18）

### 13.1 交付物

| 路径 | 作用 |
|---|---|
| `scripts/recipe_lib.py` | 配方库：target 枚举、scoped 解析、`.stat` 校验、common/version 分层合并、patch 顺序与 `.disable`、仓库结构检查 |
| `scripts/render.py` | 渲染器：缩进感知替换、`{{#if}}/{{#unless}}` 块、`.stat` 展平与 `--override`、fetch/patch/publish 生成、YAML + 结构自校验、`--check-all` |
| `scripts/detect_targets.py` | 变更 → target：PR 三点 diff / push 两点 diff、common 展开、模板变更全量、`scripts/**` 自检、输出 `targets/has_targets/publish/self_check` |
| `.github/templates/action.template.yml` | 五步 action 模板；qemu + riscv64 容器固定在模板里（D7） |
| `.github/workflows/build.yml` | 单工作流双触发；detect → build（矩阵）→ 上传 artifact；发布由 `needs.detect.outputs.publish` 控制 |
| `packages/@ruyisdk/bufferutil/4.2.0/` | 首个真实配方：`.stat` + `commands/build` + `commands/test` |
| `docker/riscv64-toolchain.Dockerfile` | `debian:trixie-slim` + build-essential / nodejs / npm / python3 / node-gyp，`riscv64` 平台 |
| `docs/recipe-spec.md` | 配方规范（英文） |
| `tests/` | `test_render.py`、`test_detect.py`（L0）、`golden/`（参考配方的渲染快照，`regen_golden.py` 刷新）、`run_action_locally.py`（L1）、`l2_publish.py`（L2） |
| `README.md` / `.gitignore` | 英文总览；忽略 `.dsh/tmp`、`.rendered`、`__pycache__`、`node_modules` |

### 13.2 三层验证结果（本机实测）

| 层 | 命令 | 结果 |
|---|---|---|
| L0 | `python3 -m unittest discover -s tests` | **47 个用例全通过**（渲染器 + 检测器 + 生成脚本 `bash -n`/自洽 + **golden 快照** + 真实仓库 `--check-all` 集成），覆盖缩进/条件块/未解析占位符/分层覆盖/patch 顺序/`.disable`/scoped 引号陷阱/两档清单/仓库结构/§4.2 全表/全零 `before` |
| L1 | `python3 tests/run_action_locally.py --pkg @ruyisdk/bufferutil --ver 4.2.0` | **通过**：容器内 `uname -m` = `riscv64`；upstream 拉取 → `node-gyp rebuild` → 产物 `ELF 64-bit LSB shared object, UCB RISC-V, RVC, double-float ABI`；隐藏 `build/` 后 `node-gyp-build` 解析到 `prebuilds/linux-riscv64`，mask/unmask 往返一致 |
| L1+发布 | 同上 + `--publish`（`NPM_REGISTRY` 指向本地 verdaccio） | **通过**：`+ @ruyisdk/bufferutil@4.2.0`；tarball 内 `package.json` 已被改写成 `@ruyisdk/bufferutil@4.2.0` 且上游字段（`repository`/`scripts`/`dependencies`/`devDependencies` 等）逐字保留；包内 `prebuilds/linux-riscv64/bufferutil.node` 仍是 RISC-V ELF |
| L2 | `python3 tests/l2_publish.py` | **26 项断言全通过**：A→B 顺序、二次执行幂等、update 不丢字段、`verify` 档位拦截改名、部分失败可重入、新版本不影响旧版本 |

本机能力（2026-11-18 实测）：docker 29.1.3 + qemu riscv64 binfmt；`verdaccio/verdaccio:5` 镜像可拉起；node 22.22.1 / npm 9.2.0 / python 3.14.4（PyYAML 可用）；无 pytest/gh/shellcheck/act。

### 13.3 与计划文本的偏差（都是实现时被迫做的选择，需你确认）

| # | 偏差 | 原因 |
|---|---|---|
| 1 | 渲染产物不是「一个 action.yml 装所有 shell」，而是 `action.yml` + `steps/{fetch,patch,build,test,publish}.sh` | 把多行 shell 塞进 YAML 块标量后，(a) 无法在块内使用 heredoc（终止符必须顶格，而块标量要求缩进），(b) 单引号/反引号转义一旦出错就是运行期才炸。脚本落盘后 `bash -n` 可静态检查，模板只负责「怎么调」 |
| 2 | publish 步骤在**宿主**执行，不进 riscv64 容器 | 发布只是拷文件 + 打 registry，不需要模拟；容器里跑只会慢，且 token/`.npmrc` 的路径处理更绕。fetch/patch/build/test 仍在容器内 |
| 3 | 矩阵 job 里 `uses:` 用固定路径 `.rendered/action`（而不是按 slug 分目录） | GitHub 的 `uses:` **不接受表达式**；每个矩阵 job 有独立 workspace，固定路径天然隔离。本地 L1 仍用 `.dsh/tmp/l1/<slug>` 分目录 |
| 4 | 清单档位收敛为 **custom / generate / verify** 三档 | `create` 与 `rebrand` 在实现上是同一段逻辑（缺则建、有则合并更新），拆两档只会增加分支和文档噪声 |
| 5 | S0 的「unscoped + scoped 各一」裁剪为单一真实配方 `@ruyisdk/bufferutil` | 结构场景（三途径降级、`source`+sha256、common 补丁 + `.disable`、A/B 双包、`verify` 拦截）在 L0 夹具与 L2 假配方里能精确构造与断言；往真实配方里塞假补丁只会让首包变脏 |
| 6 | `.stat` 的 `npm_publish[].version` 必须等于版本目录名；仓库结构问题（空包目录 / 缺 `.stat` / 包目录散落文件）在 detect 阶段硬失败 | 版本目录即发布单位，「目录 4.2.0 里写 4.2.1」是必然的错误而不是风格选择 |

### 13.4 实测踩到的两个坑（已写进实现与文档）

1. **`npm publish src/.` 会被当成 GitHub 仓库简写**（`owner/repo` → `ssh://git@github.com/src/..git`）。生成脚本因此一律先 `cd` 到目标目录取绝对路径再发布；`npm view` 不受影响。
2. **npm 元数据会把 `optionalDependencies` 并入 `dependencies`**。L2 对「不丢字段」的断言改为「上游 `dependencies` 是发布后 `dependencies` 的子集」，并逐字比对 `description`/`scripts`/`files`。

### 13.5 L3 待验证清单（真 CI，需 fork 仓库）

| # | 要验证的点 | 若失败的对策 |
|---|---|---|
| 1 | `docker/setup-qemu-action@v3` 在托管 runner 上提供 riscv64（R-a） | 镜像内置 qemu-user-static，或用预构建的 `--platform` 镜像 |
| 2 | `uses: ./.rendered/action` 能在矩阵 job 里被解析（前一步刚生成） | 改为在 workflow 里直接 `bash .rendered/action/steps/*.sh`（牺牲一点「action 被消元」的纯度） |
| 3 | composite action 的步骤级 `if: inputs.publish == 'true'` 生效 | 改为在 job 级 `if` 控制，或拆一个 publish 专用 job |
| 4 | 发布到 GitHub Packages：npm scope 必须等于仓库 owner（`CannedFish/npm-riscv64` → `@cannedfish/*`） | 仓库变量 `PUBLISH_SCOPE=cannedfish`（渲染器 `--publish-scope` 会连同 `requires` 一起改写），配方保持 `@ruyisdk/*`；已连线 |
| 5 | `permissions: packages: write` + `GITHUB_TOKEN` 是否足够发布 | 退到 PAT（`secrets.NPM_TOKEN`），`token_env` 已支持任意变量名；首跑可用仓库变量 `SKIP_PUBLISH=true` 先只验构建 |
| 6 | qemu 模拟下 `bufferutil` 的真实耗时（R-b） | 纯 C 首包已是最省；Rust 包改走 cross（D26/§13.12），qemu 只承担测试 |
| 7 | artifact 复用（S4 剩余）：`gh run download` 定位 PR 产物 + 过期判定 | 先只保留「失败即重建」的保守路径 |

### 13.6 常用命令

```bash
python3 -m unittest discover -s tests                     # L0
python3 scripts/render.py --check-all                      # 全量渲染自检
EVENT_NAME=pull_request BASE_REF=main python3 scripts/detect_targets.py
docker build -f docker/riscv64-toolchain.Dockerfile -t npm-riscv64/toolchain:riscv64 .
python3 tests/run_action_locally.py --pkg @ruyisdk/bufferutil --ver 4.2.0   # L1
python3 tests/l2_publish.py                                               # L2
```

L1/L2 的沙箱都在 `.dsh/tmp/` 下（已 gitignore），可随时删除重来。

### 13.7 L3 首跑修正（2026-11-19）

首跑在 `prepare riscv64 emulation` 失败：`docker build` 返回 0 却没留下镜像——buildx 在显式
`--platform` 且无 `--load`/`--push` 时把结果留在构建缓存里，下一步 `docker run` 找不到镜像，
转去 Docker Hub 拉一个不存在的仓库名，报出 "pull access denied" 这种误导性错误。

改法（不再每次现建）：

| 改动 | 内容 |
|---|---|
| 新增 `toolchain-image` 工作流（`workflow_dispatch`） | `buildx build --platform linux/riscv64 --push` 推到 `ghcr.io/<小写 owner>/npm-riscv64-toolchain`，打 `:riscv64` 与 `:sha-<12>` 两个标签 |
| `build.yml` | 现建镜像步骤删除，改为「解析镜像名（`vars.RISCV_IMAGE` 优先）→ 登录 GHCR → `docker pull` → `docker image inspect` 回读」 |
| action 模板 `prepare` | 本地没有就拉，拉不到则给出「设 RISCV_IMAGE 或本地构建」的可执行提示再退出 |
| 文档 | README 新增 "Container image"，recipe-spec §9 说明镜像来源与 `RISCV_IMAGE` |

验证：本地起 `registry:2`，推入镜像后**删掉所有本地标签**，L1 的 `prepare` 成功从 registry 拉取，
并用这个拉来的镜像跑完 fetch/build/test 全套（`L1 result: OK`）；L0 51 个用例全过（golden 已刷新）。

**追加（同日）**：推上 GHCR 后 CI 仍报 `403 Forbidden`，但本机匿名 `docker pull` 成功——镜像本
就是 public，问题在 CI 那次多余登录：`GITHUB_TOKEN` 读不到「本地 PAT 推入用户命名空间」的包，而
Docker 持有凭据后不会回退匿名。改法：仅当 secret `GHCR_TOKEN` 存在时才登录 ghcr.io，否则显式
匿名拉取；`docker pull` 失败时按公开/私有两种情况给出提示。

### 13.8 L3 首跑第二处修正：发布步骤 EACCES（2026-11-19）

`publish` 阶段报 `EACCES: permission denied, open 'src/package.json'`。

原因：容器以 root 运行，`src/**` 全是 root 所有；而 GitHub runner 上的步骤以 `runner`
（非 root）执行，发布阶段那道「生成 manifest」的写操作就落到别人家的文件上。

改法：所有写工作区的容器步骤统一加 `--user "${RISCV_DOCKER_USER:-$(id -u):$(id -g)}"`
并把 `HOME`、`npm_config_cache` 指到工作区内（容器用户没有可写的家目录）。于工作区产物
属主 = 调用者，宿主机上的发布步骤天然可写。

本机复现与验证（沙箱里无法切换 uid，于是用 `RISCV_DOCKER_USER` 反过来模拟 runner）：

| 场景 | 结果 |
|---|---|
| 容器 root 建 `src/`（旧行为，属主 0）+ 以 uid 1001 发布 | `EACCES: permission denied, open 'src/package.json'` —— 与 runner 报错一致 |
| 容器以 uid 1001 建 `src/`（新行为）+ 以 uid 1001 跑完整 L1 | fetch/build/test 全绿，`src/package.json`、`bufferutil.node`、`node_modules` 属主均为 1001 |
| 同上再跑发布（verdaccio） | `[publish] wrote src/package.json (generate)` → `+ @ruyisdk/bufferutil@4.2.0` |

默认（uid 0）路径同时回归通过，L0 51 个用例与 golden 已刷新。

### 13.9 artifact 复用落地（S4 收尾，2026-11-19）

L3 全线跑通后按约定补上 D16 的缺口。原设计的定位链（合并提交 → PR head → run → download →
`git diff` 校验过期）换成**内容寻址**，理由：

| 原设计 | 落地做法 | 为什么更好 |
|---|---|---|
| 用 `commits/{sha}/pulls` 找 PR head，再按 commit 找 run | `scripts/build_key.py` 对「配方 + common + 模板 + scripts + docker + 镜像 ID」求哈希，key 写进 artifact 名 | 命中即证明输入相同，不需要再 `git diff` 判过期；直推 main、重跑同一 commit 同样能命中；少两次 API 往返与一类竞态（两个 PR 先后合入） |
| 缓存对象 = 构建产物（`.node`） | 缓存对象 = **整棵 `src/` 树**（排除 `.git`） | 发布需要源码树、测试需要 `node_modules`（否则 prebuild 解析路径与消费者行为不一致），只缓存 `.node` 反而跑不下去 |
| main 侧复用 | PR 与 main 都复用 | 同一个 key 命中即同一份输入；PR 反复推同一个配方不再重建 |

实现：`scripts/build_key.py`（含 18 个 L0 用例：稳定/敏感两个方向、common 层、模板、渲染器、
Dockerfile、镜像 ID、无关配方不失效、`.pyc` 忽略）；模板新增 `reuse` 输入，`fetch`/`patch`/
`build` 三步加 `if: inputs.reuse != 'true'`，**`test` 始终执行**、`publish` 语义不变；工作流新增
「算 key → 尝试恢复（`continue-on-error`，查不到就回退重建）→ 用后打包上传」三步，权限补
`actions: read`。

验证（本地，用 `.dsh/tmp/cache-a` 真构建后打包、再恢复到 `cache-b` 跑）：

| 检查 | 结果 |
|---|---|
| 打包体积 / 条目 | 4.1 MB / 2382 条（`--exclude=.git`） |
| 恢复后带 `reuse` 跑 | `fetch`、`build` 均 `skipped by if-condition`；`test` 执行并通过（`round-tripped 23 bytes`） |
| 恢复后 `--publish`（verdaccio） | `[publish] wrote src/package.json (generate)` → `+ @ruyisdk/bufferutil@4.2.0` |
| 解包属主 | 加 `--no-same-owner`，树归属 runner 而非打包者 |

### 13.10 第二个配方：`@ruyisdk/utf-8-validate` 6.0.6（2026-11-19）

按 D21 的备选落第二个真实配方，并用它复验上一轮的 artifact 复用。

| 项 | 内容 |
|---|---|
| 上游 | `websockets/utf-8-validate` @ `v6.0.6`（无 `.gitmodules`，`deps/is_utf8` 是 vendored 源码，普通 clone 即可） |
| ABI | N-API（`src/validation.cc` 以 `NAPI_MODULE` 结尾）⇒ 一份制品覆盖所有 Node 版本，不需要按 ABI 出多份 prebuild |
| 构建 | `npm install --ignore-scripts` → `node-gyp rebuild` → 把 `build/Release/validation.node` **改名**为 `prebuilds/linux-riscv64/utf-8-validate.node`（`binding.gyp` 的 target 叫 `validation`，而 node-gyp-build 按包名找文件） |
| 测试 | 隐藏 `build/` → `node-gyp-build` 解析出 prebuild 路径（断言以 `utf-8-validate.node` 结尾）→ `require` 该路径（dlopen 成功即证明架构正确）→ 5 条断言：空缓冲/ASCII/4 字节序列合法，lone surrogate 与截断序列非法 |
| 发布 | 与 bufferutil 同构：单包、`generate_manifest: true`、`@ruyisdk/utf-8-validate@6.0.6` |

验证：

| 检查 | 结果 |
|---|---|
| `--check-all` | 2 个配方，0 失败 |
| L0 | 70 个用例通过 |
| L1 | fetch → build → test 全绿；产物 `ELF 64-bit LSB shared object, UCB RISC-V, RVC, double-float ABI`；`utf-8-validate ok: ... answered 5 cases` |
| 变更检测 | 只改新配方 → 只选它；改模板 → 两个都选；改渲染器 → 只自检；只改文档 → 无目标 |
| **复用（本轮重点）** | 真构建后打包 6.6 MB（排除 `.git`）→ 恢复到新沙箱 → `--reuse` 跑：`fetch`/`build` 均 skipped，`test` 通过，`publish` 出版成功 `+ @ruyisdk/utf-8-validate@6.0.6` |
| key 敏感性 | 往 `commands/build` 末尾加一行注释 → key 从 `2de2921bfb27` 变为 `751331d8f064`；还原后回到原值 |

第二个配方顺带验证了「配方内容差异不会污染 key」：两个配方的 key 不同、互不影响，改模板则两者同时失效。

### 13.11 detect 按「构建输入是否真的变了」收窄（2026-11-19）

**触发**：PR #3 合入后，观测到 bufferutil 明明没被这个 PR 改动，却也跟着跑了一遍
（它在 main 上命中缓存、只跑了 test/publish，所以成本不大，但与「只构建有变更的包」的预期不符）。

**根因**（两层，都已实测确认）：

1. detect 的规则是「模板变更 → 全量」，而 PR #3 确实改了 `.github/templates/action.template.yml`
   （+11 行：新增 `reuse` 输入、fetch/patch/build 三处 `if: inputs.reuse != 'true'` 守卫、几行注释）。
   按规则它就该被选中——规则本身没坏。
2. 旧 key 哈希的是模板**文件**，所以 key 从 `a359ecd91acc` 变成 `1f37400b0deb`，缓存未能兜住。
   但把 base 与 head 都渲染出来对比，**实际执行的命令逐字节相同**：

   | 渲染产物 | base(efc2ed7) | head(f503486) |
   |---|---|---|
   | 全部 `run:` 块 | `f0561413c081` | `f0561413c081` |
   | 全部 `steps/*.sh` | `67bf97769e31` | `67bf97769e31` |

   即这次重建产出与缓存里那份等价，纯属浪费。

**修法（两条，配套）**：

- **口径（A）**：`scripts/build_key.py` 改为哈希**渲染后的构建命令**——fetch/patch/build
  三步的 `run` 块、`steps/{fetch,patch,build}.sh`、补丁文件内容、镜像 ID、`--extra`。
  `test`/`publish` 片段、`.stat` 里的发布字段、模板输入与 `if:` 守卫都不再影响 key。
  渲染由 key 自己完成（`STEPS_DIR=".rendered/action"` 固定），因此渲染器改动也会体现为命令文本变化，
  `scripts/` 不需要单独做输入。
- **选择（B）**：`scripts/detect_targets.py` 把全局输入（模板、`scripts/`）的变更先变成**全量候选**，
  再用 `narrow_by_build_inputs` 比较 base 与 head 的 key：相等即跳过。base 侧优先跑**base 自己的**
  `scripts/build_key.py`（于是渲染器改动会体现为 key 变化），base 上没有该工具时用当前工具渲染 base 树；
  任何一步失败（没有 base commit、渲染报错、工作树不可用）都退回「全部保留」。
  `docker/**` 例外：镜像 digest 在 detect 阶段未知，直接全量交给构建侧按 key 判定。
  明确变更的配方（含只改 `commands/test`）永远保留——新测试必须真跑一遍。

**验证（本地）**：

| 检查 | 结果 |
|---|---|
| 新 key 对模板「无实质改动」不敏感 | 加输入 / 加 `if:` / 改注释 / 改 test 步骤 / 改 `.stat` 发布 registry → key 均不变；改 fetch、build 步骤或 upstream ref → key 变化 |
| 重放 PR #3 | `path_rules` 给出候选 2 条，收窄后**只剩 `@ruyisdk/utf-8-validate`**（bufferutil 报 `build inputs unchanged (key bb563c29406f), skipping`） |
| L0 | 86 个用例通过（`test_build_key` 27、`test_detect` 18，新增「收窄」与「哪些编辑不该动 key」两组） |
| L1 + 复用 | utf-8-validate 真构建 2m05s → key `800d52552cdb`（两次计算一致）→ 打包 6.6 MB → 恢复到新沙箱 `--reuse`：fetch/build skipped、test 通过、publish `+ @ruyisdk/utf-8-validate@6.0.6` |

### 13.12 第三个配方：`@ruyisdk/rolldown` 1.3.3 与 `cross` 工具链（2026-11-23）

**任务**：前两个包（`bufferutil`、`utf-8-validate`）都是「源码不用动、qemu 里直接编译」就成功；
rolldown 预期是要打补丁才能出 riscv64 二进制的案例。实测结论比预期乐观：**源码一行不用改**，
真正卡住的是**构建载体**而不是源码。

**上游现状（逐条实测）**：

| 事实 | 证据 |
|---|---|
| 上游不产 riscv64 绑定 | `napi.targets` 无 riscv64；npm 上 `@rolldown/binding-linux-riscv64-gnu@0.0.0` 是空占位（16 个平台包齐全，独缺 riscv64） |
| 加载器**已经**支持 riscv64 | 发布包 `dist/shared/binding-*.mjs` 里就有 riscv64 分支：先 `require('./rolldown-binding.linux-riscv64-gnu.node')`，再退到 `@rolldown/binding-linux-riscv64-gnu` |
| 打包过滤不会漏掉它 | `files: ["dist", ..., "!dist/*.node"]`：`!dist/*.node` 只匹配 `dist/` 第一层，`dist/shared/*.node` **会**被打进 tarball |
| Rust 源码在 riscv64 上零错误 | `cargo build --release -p rolldown_binding --target riscv64gc-unknown-linux-gnu`（rustc 1.100.0 + 交叉 gcc 12.2.0）：3m21s（16 核）出 19 MB `stripped` RISC-V ELF，无报错无 riscv 相关警告 |
| 功能可用 | 上游 `dist/` + 我们的 `.so` 改名为 `dist/shared/rolldown-binding.linux-riscv64-gnu.node` → 在 riscv64（qemu，node 20.19.2）里真打包一个 JS 模块，输出正确 |

**真正的约束：JS glue 无法在 riscv64 上重建。** `dist/` 由 `@oxc-node/core` 驱动、并且要先用
rolldown 自己来打包自己（自举）；而 `@oxc-node/core` 的 16 个平台包里**没有 riscv64**。所以复用上游
已发布的 npm tarball 是唯一可行且更忠实的做法：JS 部分与上游逐字节相同，只有原生绑定是本仓库构建的。
代价是这条配方的 `fetch` 变成**双来源**（git 取 Rust 源码 + 按 sha512 钉住的 npm tarball 取 JS 外壳），
配方自带 `commands/fetch` 覆盖生成版（D13 的「配方覆盖」机制天然支持）。

**为什么需要 cross（D26）**：同一份源码在 qemu/riscv64 容器里，10 分钟连 `target/release/deps`
都没出现一个文件（每次 `rustc` 调用要几分钟）；rolldown 有数百个 crate，照此推算要数小时，
逼近 GitHub 单 job 6h 上限。交叉编译 3-4 分钟出活。**测试仍在 riscv64 容器里跑**——交叉只搬走
编译，不搬走验证：

| 阶段 | native | cross |
|---|---|---|
| fetch / patch / build | riscv64 容器（qemu） | amd64 容器（交叉工具链），镜像 `RISCV_IMAGE` |
| test | 同上 | riscv64 容器，镜像 `RISCV_TEST_IMAGE` |

`.stat` 加一个 `toolchain: native|cross` 字段（缺省 native，老配方**渲染结果不变**），
渲染器据此给出 `build.platform` / `build.image` / `build.dockerfile` / `test.*` / `riscv.triple`
变量，模板只引用变量，`{{#if job.cross}}` 只多出一段「交叉工具链自检」。新增镜像
`docker/riscv64-cross.Dockerfile`（amd64 + rustup 钉 1.100.0 + `riscv64-linux-gnu-gcc` + cmake），
`toolchain-image` 工作流改成矩阵一次推两型（`:riscv64` 与 `:cross`）。

**验证（本机实测）**：

| 检查 | 结果 |
|---|---|
| 交叉镜像自检 | `rustc 1.100.0`、`riscv64gc-unknown-linux-gnu` 已装、`riscv64-linux-gnu-gcc 12.2.0` |
| L1 全链（cross） | fetch（git + npm 双源，tarball 过 sha512）→ build **3m13s** → binding 19 MB RISC-V → test：`npm pack` 断言「tarball 会带 `dist/shared/rolldown-binding.linux-riscv64-gnu.node`」（59 文件 / 20.5 MB 未压缩）→ 在 riscv64 上 `rolldown()` 打包成功（`[test] bundled on riscv64 with node v20.19.2`） |
| 老配方未受影响 | 模板改动后 bufferutil 的 **key 仍是 `bb563c29406f`**（渲染出的 fetch/build 命令逐字节不变）→ CI 不会重建，只跑 test/publish。这正是 §13.11「按渲染命令计 key」的直接收益 |
| L0 | 92 个用例通过（新增 toolchain 渲染/校验、detect payload、`.stat` 校验 6 例） |

**踩到的两个坑（都已写进配方注释）**：

1. 交叉镜像缺 `cmake`：rolldown 的分配器 `libmimalloc-sys2` 用 CMake 编 mimalloc C 源码，
   报错只有一句 `is cmake not installed?`。补进镜像后在镜像构建期就固定下来。
2. **上游清单的 `publishConfig.registry` 会压过 `--registry`**：rolldown 的清单钉了
   `https://registry.npmjs.org/`，于是 `npm publish` 无视配方给的 registry 直奔 npmjs——第一次
   L2 演练就是这样失败（`ENEEDAUTH ... registry.npmjs.org`）。这是**发布机制的通用缺陷**，不是本配方
   独有：任何带 `publishConfig` 的上游清单都会踩。改法是发布生成器统一处理——`generate` 模式把
   `publishConfig.registry` 改写为配方 registry（保留 `access`，并打日志），`verify` 模式发现不一致
   直接**硬失败**（渲染器无权改的清单不能被悄悄发到别处）。修完 L2 演练通过：
   `+ @ruyisdk/rolldown@1.3.3`，verdaccio 里的 tarball 含 19.5 MB 绑定。
3. `npm pack` / `npm install` 的**位置参数**会被当成包名解析：`npm pack out/rolldown` 会去
   `git ls-remote ssh://git@github.com/out/rolldown.git`（`out/rolldown` 被读成 GitHub 简写
   owner/repo）。配方里改成先 `cd` 进目录再执行。

**消费者视角复验**（用 verdaccio 里**已发布的 tarball**，不是构建树）：在 riscv64 容器里
`npm install rolldown-1.3.3.tgz --omit=optional` → `added 3 packages` → `import { rolldown } from
"@ruyisdk/rolldown"` → 打包成功（`consumer arch=riscv64`）。即「发出去的包」本身可用。

**已知限制**：cross 镜像里没有 riscv64 的 C 库/头文件，所以「Rust 之外还依赖系统 C 库」的包不能走
cross（要么回到 native，要么给镜像加 `dpkg --add-architecture riscv64` 与对应 `-dev` 包）；本轮
rolldown 只需要交叉 gcc 做链接，不涉及该限制。

---

## 14. 2026-11-23 第二轮：配方命名、发布仓库与幂等判定（D27–D29）

### D27 配方路径与发布名

* 路径写 `packages/<npm 名>/<version>`，`<npm 名>` = **上游发布时用的名字**：三个包都
  unscoped（`bufferutil` / `utf-8-validate` / `rolldown`），因此从 `packages/@ruyisdk/...` 移回
  `packages/bufferutil/...`。scoped 包才占两段路径（`packages/@scope/name/1.2.3`）。
* 路径是**配方的身份**，不该承载「发到哪个仓库、挂在谁的 scope 下」——那是 `npm_publish[].name`
  的事。
* 发布名**沿用上游名**（不加 scope）：`npm i rolldown` / `npm i bufferutil` 才有机会被透明替换成
  riscv64 版本。若目标仓库强制要求 scope，用 **`@ruyirepo`**（`--publish-scope ruyirepo` 或仓库变量
  `PUBLISH_SCOPE`），不再用 `@ruyisdk`——那是上游组织名下的 scope，我们无权发布。
* 历史记录（§13.1 / §13.10 / §13.12）保留当时的 `@ruyisdk/*` 字样，它们是**当时的事实**。

### D28 发布仓库与凭据

* 目标仓库：`https://forgestash-old.verify.oepkgs.net/repository/npm-rv-test-local`。
* 该仓库对未认证请求回 `401` 且带 `WWW-Authenticate: Basic realm="ForgeStash platform"`——要的是
  **HTTP Basic**，不是 npm bearer token。因此 `.stat` 新增 `npm_publish[].auth`：`token`（默认，写
  `_authToken`）| `basic`（写 `_auth`，值为 base64 `user:password`）。三个配方都用 `basic`。
* 授权行按**完整 registry URL**（含路径）作 key 写入一次性 `.npmrc`（`//host/<path>/:_auth=...`）：
  npm 是按「它正在访问的那个 URL」查凭据的，只写 host 对 `/repository/x/` 这类仓库并不可靠。
* `token_env` 指**环境变量名**（沿用 npm 惯例的 `NODE_AUTH_TOKEN`），CI 里由 secret
  `NPM_PUBLISH_TOKEN` 注入；本机演练用 `NODE_AUTH_TOKEN=$(cat .dsh/tmp/verdaccio-token)`。

### D29 幂等判定：发完之后看注册中心回答

* 原实现先 `npm view <name>@<version>` 再决定发不发。**同名策略下这条预检是错的**：只要仓库带
  npmjs 上行/组仓库，`npm view bufferutil@4.2.0` 会命中上游同名版本 → 判定「已发布」→ 静默跳过。
  本轮 verdaccio 演练就这样把 `bufferutil` 跳过了（日志里只有 `already published`，仓库里没有）。
* 改为：直接 `npm publish`；若注册中心回 409 / `EPUBLISHCONFLICT` / "already exists" /
  "previously published" / "cannot publish over" → 记一次 skip（重跑仍绿）；其它错误照常失败并打印。
  判定权交给**目标仓库本身**：它才知道「这个版本在不在我这里」。

### 本轮验证

| 项 | 结果 |
|---|---|
| L0 | **99 用例通过**（新增 `PublishAuthTests`：auth 两档渲染、未知档拒绝、幂等不再依赖 `npm view`）；`--check-all` 3 配方 0 失败 |
| 构建 key | `bufferutil` `bb563c29406f`、`utf-8-validate` `6416447a76d1`、`rolldown` `a8b66e70b5c1`——与改名**前**一致（目录名不参与 key，缓存树不用重建） |
| L2 演练（verdaccio） | `[publish] published rolldown@1.3.3`、`[publish] published bufferutil@4.2.0`；重跑 → `already published: rolldown@1.3.3 (the registry refused a second publish; skipped)` 且 job 仍 OK |
| 消费者（riscv64，指向本地仓库） | `npm i rolldown` → `added 3 packages`，`node_modules/rolldown/dist/shared/rolldown-binding.linux-riscv64-gnu.node`，`bundle ok, 5 lines, arch=riscv64` |
| 消费者（riscv64，上游 npmjs 对照） | `npm i rolldown` → `added 3 packages`（16 个 optionalDependencies 全被 os/cpu 跳过），运行 → `Cannot find native binding`（退化到 wasm32-wasi 分支） |
| 消费者（bufferutil） | `npm i bufferutil` → 解析到 `prebuilds/linux-riscv64/bufferutil.node`，`unmask` 往返一致（23 字节） |
| 已知坑 | 原生 `mask` 是 5 参数（`source, mask, dest, offset, length`）、`unmask` 是 2 参数（就地还原）；用 2 参数调 `mask` 会触发上游 `assert` 崩溃——**是调用者的错，不是产物的错**（第一版自测就踩了） |

### D30 「publish 卡住」的排查结论与可见性修复

* 目标仓库侧没有问题：本地实测 10 MB 裸上传 `http=200 / 85ms`；真实小包发布 `PUT 201 / 199ms`；
  9.4 MB 的 `rolldown@1.3.3` 发布 `PUT 201 / 865ms`（整条命令 4.2s）。
* CI 里"卡在 publish 不动"的直接原因是**我们自己的脚本把 npm 的输出吞了**：`out=$(npm publish ...)`
  在命令结束前不产生任何日志，慢上传因此表现为完全静止。已改成 `| tee` 流式输出 + `--loglevel http`，
  CI 日志会直接出现 `npm http fetch PUT ... <耗时>`，慢路线一眼可见。
* 另一个先决条件：`.stat` 的 registry **必须以 `/` 结尾**（D28 的 `normalize_registry`）。否则 npm 用
  `new URL(".", url)` 推凭据 key 时会砍掉最后一段，`npm publish` 预检拿不到凭据 → `ENEEDAUTH`
  且**一个请求都不发**（本地 mini registry 复现，见 change log §8）。
* 消费者侧实测（真仓库 + scope 分流）：`.npmrc` 用 `registry=<hosted>`、`@rolldown:registry` /
  `@oxc-project:registry` 指向 npmjs，并加 **`replace-registry-host=never`**（否则 npm 会把 npmjs 的
  tarball URL 改写回默认仓库 → tarball 404）。riscv64 容器内 `npm i rolldown` → `added 3 packages` →
  `bundle ok, arch=riscv64`。

## 15. 2026-11-24 第三轮：上游形态发布（D30–D36）

本轮的问题不是「哪个包没做」，而是**做出来的包不是上游那个包**：bufferutil / utf-8-validate
发出去只剩 riscv64 一个平台目录（上游 6 个），rolldown 则把 19.5 MB 的 riscv64 二进制塞进
主包，每台机器都白下。用户给的三条原则是：①不改上游打包习惯，只加 riscv64 增量；②测试走上游
自己的套件，只补 riscv64 用例；③生成的 patch 必须是可直接提交上游的增量。以下决策照此落地。

### D30 rolldown：发布形态跟随上游的双包

* 上游每个 release 发**两个包**：纯 JS 的 `rolldown`，以及每目标一个的
  `@rolldown/binding-<arch>-<abi>`（包内只有一个 `.node`）。因此本配方发两个包，绑定在前：
  `@rolldown/binding-linux-riscv64-gnu@1.3.3`（3 文件）→ `rolldown@1.3.3`（上游 58 文件 + 一条
  `optionalDependencies`）。
* 绑定**不再** stage 进 wrapper 的 `dist/shared/`。旧做法依赖上游 `files` 里的 `!dist/*.node` 只匹配
  `dist/` 直接子文件这个巧合，等于让每个架构都下载 riscv64 二进制；现在由 npm 按 `os`/`cpu` 决定是否安装。
* 平台包清单用 `manifest_shape: template`（`templates/package.json`）而不是 `generated`：上游的
  平台清单是 napi 在 release 时生成的，模板逐字段照抄（`files`/`engines`/`repository.directory`/`bugs`/
  `publishConfig.access`/`os`/`cpu`/`libc`），riscv64 包与 x64 包只在架构相关字段上不同。
* **测试 A 档**：上游 `packages/rolldown/tests/behaviors/emit-debug-data/*.test.js`（3 文件 6 例，
  实测 **16.07s**，riscv64 容器内）。选它的理由是它只依赖公开 API 且是纯 JS；其余 `.test.ts` 需要
  `@oxc-node/core`（riscv64 无二进制）与整仓库 devDeps，在 riscv64 上跑不起来。落地要点：包必须按
  **真实依赖**加载（`server.deps.external`）而非走 runner 的 SSR 变换，否则 `import.meta.resolve`
  被替换成 shim，上游那条并行插件用例会误报——而消费者装的正是真实依赖形态。

### D31 prebuild 型包：上游 tarball 基座 + overlay

* `base.kind: upstream-tarball`：publish 步骤下载上游 tarball、校验 `sha512`、解包，再把
  `prebuilds/linux-riscv64` 拷进去。overlay **只增不改**（`allow_overwrite` 默认 false），所以上游
  6 个平台目录逐字节保留。
* 下载与校验放在 **publish 步骤内**（runner 上），不是 `fetch`：publish 侧不参与构建 key
  （§13.11 的口径只看渲染后的 fetch/patch/build），配方换发布形态、改 integrity 都不会让缓存失效。
* `manifest_shape: upstream`：清单用上游的，只写 `optionalDependencies`（本配方不发平台包时为空）
  与 `publishConfig.registry`（npm 的 `publishConfig` 优先于 `--registry`，不改就会发到 npmjs）。
* 上游仓库**不在 git 里存 `prebuilds/`**（是 CI 产物），所以「其余平台一个不少」只能在发布后的
  tarball 上断言，不能在容器里断言——这条由 `tests/verify_published.py` 承担（D35）。

### D32 清单生成：三形态 + 幂等合并

* `manifest_shape`：`upstream`（原样保留，最像上游）/ `generated`（按 `.stat` 写平台字段）/
  `template`（渲染 `templates/package.json`）。`manifest_shape` 与旧的 `generate_manifest` 互斥。
* `optional_dependencies` 合并是**加性且幂等**：上游列出来的平台一个都不许消失，只许加本配方发布的
  包，重跑必须产出同一份清单。原来的「数量必须变大」判定在 `source-tree` 发布下是错的——清单就写在
  构建树里，第二次跑到这里会看到 17→17 而失败（本地 L2 重跑真实触发）。
* `publishConfig.registry` 由渲染器改写；这是重发的包里**唯一**的清单差异，且在日志里显式记录。

### D33 patch 分轨：`patches/` 与 `upstream/`

* `patches/` = 构建期补丁（我们为了让构建能跑而加的），由生成的 `patch` 步骤应用。
* `upstream/` = 同一个增量写成给上游的 PR，**本仓库从不应用**；`.stat` 的 `upstream_increments`
  记 `patch`/`touches`/`pr`，其中 `touches` 必须等于 diff 触及的文件清单（双向相等）。
* `scripts/check_patches.py` 是硬门禁，随 `--check-all` 跑：真 diff（无 `/dev/null` 两侧、`a/ b/` 前缀、
  每文件一个 `diff --git`）；路径与 `touches` 双向相等；**不得出现本仓库任何痕迹**（仓库名、镜像、
  容器文件、`render.py`、`dsh` 路径）；平台包名必须等于 napi 从 triplet 推出的名字
  （`riscv64gc-unknown-linux-gnu` → `-riscv64-gnu`，注意 npm 的 `libc: glibc` 要映射成 `gnu`），且该增量
  确实新增了这个 target；`--check-patches-network` 时对 pinned ref 跑 `git apply --check`。
* 生成的 `patch` 步骤**永不**引用 `upstream/`，有单测断言——否则两条轨道迟早串味。

### D34 测试数据跟着配方走

* 配方可以带 `tests/`：`riscv64-skip.txt`（跳过的上游用例，一行一条，`#` 后写原因）、
  `vitest.config.mjs`、`package.json`、模板文件的替代品等。
* 模板把 `RECIPE_DIR=/w/<recipe.dir>` 传给 **test** 步骤：`/w` 在 CI 里就是 checkout，所以配方目录
  可达；本地 `run_action_locally.py` 也把配方目录拷进 sandbox，保持同构（否则本地永远测不到这条路径）。
* 测试**仍在 riscv64 容器里跑**（N1 不变）。bufferutil / utf-8-validate 的上游套件与架构无关，三个配方
  目前都没有 skip 条目——机制在，空转。

### D35 交付物核对进仓库

* `tests/verify_published.py` 把「发布出来的字节」与「上游的字节」逐文件比对，三种形态：
  `prebuild`（上游 + 一个 riscv64 目录，其余逐字节相同）、`wrapper`（文件集合等于上游、`optionalDependencies`
  = 上游 + N）、`platform`（与上游另一个架构的平台包布局一致，清单逐字段相同、架构字段不同）。
* 它是 opt-in 脚本（要一个真有该版本的仓库 + 能连 npmjs），不进单测；落地到 L2/L3 验收。

### D36 上游增量必须公开

* README 单列一节，把三个 `upstream/*.patch` 的内容、对应的上游仓库与文件、以及检查方式写清楚：
  这些增量是准备提交给上游的，任何人不该需要读本仓库的实现细节才能把它交出去。

### 本轮验证

| 项 | 结果 |
| --- | --- |
| L0 | **136 用例通过**（`test_check_patches.py` 9 例、清单幂等 2 例、`GoldenTests` 改为两配方表驱动并加集合双向一致）；`--check-all` 3 配方 0 失败；`--check-patches` 0 问题（`--network` 亦 0） |
| L1 bufferutil / utf-8-validate | 上游套件 8 / 10 例在 riscv64 通过 + riscv64 断言；产物 RISC-V ELF |
| L1 rolldown | 上游 6 例通过（16.07s）；平台包 3 文件（9.1 MB packed / 19.5 MB unpacked）；主包 58 文件、无 `.node` |
| L2 rolldown（verdaccio） | 两包 `PUT 201`；主包 58 文件 / 231.8 kB packed / 993 kB unpacked |
| 产物核对 | bufferutil 13→14、utf-8-validate 16→17（新增仅 riscv64 prebuild，`e_machine=243`）；rolldown 主包 58=58（`optionalDependencies` 16→17）；平台包与 x64 平台包同构 |
| 消费者（riscv64） | `npm i bufferutil` / `utf-8-validate` 解析到 `prebuilds/linux-riscv64`；`npm i rolldown` 装到 `@rolldown/binding-linux-riscv64-gnu` 并成功打包 JS |
| 体积 | 主包 ~1 MB（与架构无关）、riscv64 平台包 9.1 MB packed / 19.5 MB unpacked；x64 消费者省下 19.5 MB |

### 遗留风险

* 目标仓库是否允许发布 `@rolldown` scope 未验证（本地误用 token 得到 403 是凭据问题，不足以判定）。
  若拒绝：主包可改 `PUBLISH_SCOPE`，但平台包名不能改（loader 按字面量 `require`），必须由仓库放行该 scope。
* rolldown 上游套件只跑 A 档三文件；扩大范围需要 `@oxc-node/core` 的 riscv64 构建，属上游议题。
* `upstream-tarball` 基座在 publish 时依赖 npmjs 可达；断网重发会明确失败（宁可失败也不发来源不明的包）。

### D37 「已发布」不等于「已发对」：发布步骤回读仓库

* L3 实测暴露：npm ≥11 在客户端预检就拒绝「版本已存在」，一个字节都不发；旧逻辑把 409 记作
  「已发布，跳过」并保持绿色，于是真仓库继续供上游原版 rolldown（无 riscv64 绑定）——绿的一步、错的包。
* 决策：`npm publish --force` 让仓库判断能否覆盖（本仓库发的就是上游版本 + riscv64 增量，替换即目的），
  随后**回读断言**：`npm pack --dry-run --json` 得到的摘要 == 仓库该版本 `dist.integrity`，且服务端清单仍引用
  本配方的平台包；不同即硬失败，并给出处置建议（开 redeploy / 删除该版本 / 换版本）。
* 「绿」的定义随之收紧为：**仓库确实在供我们的字节**。`--assert-served` 是这条承诺的唯一实现点，
  单测用 PATH 上的桩 npm 覆盖六个分支（含 npm ≥11 的 object 形 pack 输出）。
* 已知代价：每次发布多一次 `npm pack`（对 19.5 MB 绑定约 1 秒）和一次 `npm view`；换来的是「静默没发出去」
  这类事故变成红的构建。

### D38 发布侧改动必须能触发交付：publish key 与 `publish` 模式

* 事实：build key 有意不含发布侧（D16/§13.11），而发布步骤本身就是交付物。于是「只改发布脚本」时
  `targets=[]`、`has_targets=false`，整个作业被跳过，修好的发布逻辑永远送不出去（L3 实测复现）。
* 决策：引入第二个 key（`publish_key`：渲染后 publish 步骤 + `steps/publish.sh` + `manifest.mjs`），
  检测器按两个 key 给出三种 mode（`build` / `publish` / 跳过）；`publish` 目标从缓存树交付，
  不重建、不重测。`merge()` 中 `build` 优先。
* `publish` 目标只在允许发布时产生（push→main、或 `workflow_dispatch` + `republish`），PR 里丢弃。
* 手动杠杆：`workflow_dispatch` 的 `republish` 输入把全部配方标成 `publish`——交付修复不需要碰配方。
* 守卫：action 的 `publish_only` 模式要求 `reuse==true`，否则在 publish 之前硬失败（拿没恢复的树去发
  = 发错包）。
* 顺带：`runs-on` 支持 `RISCV_RUNNER` 变量（自维护 runner 只需一个标签，不改 workflow），README 增补
  「Running it in a fork」与「Self-hosted runners」两节，并把仓库地址改为占位符（当前 `.stat` 指向的是
  测试仓库，直接展示会误导）。

### D39 发布后的"仓库是否在服务我们的字节"必须读三种形态，且"读不到"≠"不一样"

* 事实：L3 两次都在 201 之后立刻报 "the registry does not serve this artifact … (no integrity)"。
  根因在客户端：npm ≥12 的 `npm view --json` 输出是一元素**数组**（≤11 是对象），
  我们把数组当 manifest，`dist` 恒为 undefined；随后又把"字段缺失"当作"摘要不同"，
  对一个已经 201 接受的上传给出错误指控。
* 事实（用户补充验证）：目标私仓的版本文档只有 `dist.tarball`，既无 integrity 也无 shasum。
  所以"读不到"不是边缘情况，而是这家仓库的正常形态。
* 决策：校验按三层证据降级——`dist.integrity`(sha512) → `dist.shasum`(sha1) →
  下载 `dist.tarball` 自算 sha512。下载用 npm 的凭据规则（nerf-dart 最长前缀匹配 npmrc）。
* 决策：结论三态——一致 / 确凿不一致（失败，打印两份摘要）/ 无法验证。
  无法验证在 **201 之后是 WARNING（不判红）**，在 **被拒之后是失败**（此时没有任何证据
  表明消费者拿到的是我们的字节）。shell 通过 `--accepted` 传入上传结果。
* 决策：`PUBLISH_VERIFY=digest` 允许只认摘要不下载（慢链路的显式选择，日志如实声明未验证）；
  `PUBLISH_VERIFY_MAX_BYTES`（默认 64 MB）限制体积。
* 教训（写进注释）：凡是"读不回来就把缺失当冲突"的断言，都会在换一个仓库实现时制造假红线；
  断言必须区分"证据不足"与"证据相反"。npm 版本差异（9/11 对象、12 数组）同理必须双向兼容。

### D40 上传预算按包体计算，不继承 npm 的 5 分钟默认

* 事实：`minipass-fetch` 的 `timeout` 是一个从 socket 分配起计时的墙钟定时器，覆盖上传与响应；
  npm 9/11/12 这段实现相同，默认都是 5 分钟。于是"默认预算"实际是**对路由速度的上限**：
  binding 的请求体约 12.2 MB，低于 ~40 kB/s 就不可能完成。
* 事实：超时发生后 npm 会重试（默认 2 次），每次重传整个 12 MB；且上传未完成时前置代理不会把
  请求交给私仓，所以私仓"看不到请求"。
* 决策：`manifest.mjs --plan` 在发布前给出包体与预算：`min(1h, 300s + 包体×1.4/16kB每秒)`，
  并在日志中明示（`upload … bytes, fetch timeout …ms`）；`PUBLISH_FETCH_TIMEOUT` 可覆盖。
* 决策：包体 ≥ 4 MiB 时 `--fetch-retries 1`（重试成本 = 整包重传）。
* 教训：凡是"默认值恰好够用"的地方，都要问一句默认值度量的是什么；npm 的 5 分钟度量的是
  整条请求的墙钟，而不是连接的空闲。

### D41 物理硬件验证层：模拟流水线之外，追加"真机装 + 真机跑"（计划见单独文件）

* 目标（用户 2026-11-25）：产物不仅要在 riscv64 容器（模拟）里装得起、跑得通，还要在**真实
  RISC-V 硬件**上装得起、跑得通，作为对用户的可信背书。计划全文：
  `.dsh/物理验证计划-2026-11-25.md`（本条目只记摘要，细节以该文件为准）。
* 形态：**保留现有模拟流水线不动**，新增**独立工作流** `verify-physical.yml`
  （`workflow_run`/`workflow_dispatch`，`runs-on: [self-hosted, linux, riscv64]`），
  跑在硬件上的**只有 install + verify 两步**；不构建、不发布、不参与缓存、不阻塞发布。
* 无 docker：新模板 `.github/templates/physical.template.yml` 内**禁止出现 docker**
  （DinD 支持存疑），步骤直接跑在 runner 上。
* 读仓库 ≠ 发布仓库（用户确认）：新增 `.stat` 顶层 `physical` 段——
  `registry: https://forgestash.artifact.yuantu.ac.cn/repository/npm-riscv-group`（组仓库，
  `auth`/`token_env` 语义同发布），`source: registry|tarball`；build 上传的 tgz 作兜底，
  但兜底也必检"摘要仍等于主流程发出去的那份"。
* 范围：每次只验**本次变化/发布的包**（由 build 新写的 `publish-report-<slug>.json` 驱动，静态矩阵 + `if:` 过滤）。
* 常驻容器护肤：`mktemp` 隔离目录 + `trap` 清理 + `HOME`/`npm_config_cache` 隔离、绝不 `-g`；
  `concurrency: physical-riscv`（不取消）+ 容器内 `flock`；`uname -m` 与磁盘下限前置自检；
  作业 `timeout-minutes: 30`。
* 首次安装一次即可（用户确认 node/file/flock 可能缺失）：幂等 `bootstrap` job，缺什么装什么
  （riscv64 无官方 node 二进制，走发行版仓库）；无 sudo 时明确报 `environment` 而非静默失败。
* 报告：job summary + `physical-report.<slug>.json`（三层摘要对齐：上传摘要 → 读回结论 → 物理机取回摘要），
  失败分类区分 `environment`/`install`/`digest`/`execute`；**不回写仓库**（用户确认）。
* 重档（上游套件 / 995 MB 缓存树恢复）暂不采用，后续只改 `verify` 片段即可扩充（用户确认）。
* README `How it works` 增加**单配方视角流程图**（模拟层 + 物理层的完整链路）。
* 教训（沿用 D39）：物理机与组仓库不通，要报"环境/网络"类失败，绝不能读成"产物坏了"。
* 二次讨论（同日，计划已升为 v2）：node **≥ 20 为硬门槛**（装不到就报 `environment` + 一次性手工命令，
  不用 18 顶）；无 sudo 时接受"一次性手工装 + 之后只检查"；组仓库当前匿名可读，但 `physical.auth`
  保留且**与发布认证互相独立**（含 `none`）；兜底 tgz **只对 `served: unverified` 的目标上传**；
  矩阵形态按执行结果再调；`file` 缺失可退化为读 ELF 头 `e_machine == 243`，但须标记降级并提示更新环境。
* **已实现（2026-11-26）**，模拟流水线未改，改动面：新模板 + 新工作流 + 新脚本
  （`scripts/{auth_function,install_template,physical_template}.sh`、`scripts/physical_plan.mjs`）、
  `scripts/render.py`/`scripts/recipe_lib.py` 支持第二模板、`scripts/publish_manifest.mjs --report`
  与 publish 步骤写报告、三个配方的 `physical:` 段与 `commands/verify`、`tests/test_physical.py`
  （37 条）+ golden 重生成。
* 与计划的偏差（都记在下面，均出于执行中的具体证据）：
  * 配方只提供 `verify`；`install` 由渲染器生成（可用 `commands/install` 覆盖）。计划里写成"两段都来自配方"，
    实际安装动作必须由渲染器统一（摘要比对、npm 形状兼容、兜底判定都是渲染器的事）。
  * 不引入 `uses:` 动态引用：物理工作流按"目录里有哪些 `steps/physical.sh`"循环调用（`bash` 直跑），
    因此新增配方无需改 YAML；矩阵不再是硬编码，而是由 build 上传的 `physical-action-*` 决定。
  * 物理作业**会 checkout 触发构建的那个 commit 的 `scripts/`**（`sparse-checkout`）：只为跑
    `scripts/physical_plan.mjs`（合并报告 / 摘要 / 终判）。计划原写"不 checkout"，改为"只取 scripts"，
    记为一次依赖增加（git 在 bootstrap 自检里报）。
  * `--check-all` 对没有 `physical:` 的配方打印 `[SKIP]` 而不是失败；`verify` 片段存在但 `.stat` 无
    `physical:` 时报错（半套契约不许留）。
* **真机已跑通（2026-11-27）**：三个配方 `verdict=ok`、`digest matches`，整轮约 3 分钟。实测
  `uname -m=riscv64`、node `v20.19.2`、npm `9.2.0`、`flock` 在位、`file` **缺失**（ELF 退化为头检查）、
  组仓库可达且两个配方能确认摘要、rolldown 的发布报告是 `served: unverified` 走了兜底 tgz 且摘要一致。
  随之修了三处（详见变更日志 §4.6）：片段 shell 里 `log` 未导出（`export -f log`）、板卡型号读不到
  （增 `/proc/device-tree/model` 与更多 cpuinfo key）、摘要 note 把"缺 file"和"无摘要"混成一句；
  并把 runner 工具清单做成可执行的 `scripts/physical_preflight.sh`（工作流第一步调用，永远 exit 0，
  缺工具 → `environment` verdict 或报告里自报降级；README 与 spec §12.3a/§12.4 记了工具表与安装命令）。
  剩余：补 `file` 后重跑一轮确认 `elf_check: file`；查清组仓库为何没给 rolldown 确认摘要。
