# 构建 key 口径与 detect 收窄-2026-11-19

日期：2026-11-19
起因：PR #3 合入后，未被该 PR 改动的 bufferutil 也重新构建了。查清是「模板变更 → 全量」规则
命中（PR 确实改了模板）+ 旧 key 哈希模板文件（因而缓存未命中），但两版渲染出的执行命令逐字节
相同，重建纯属浪费。

## 1. 现象与证据

| 事实 | 数据 |
|---|---|
| PR #3 改动 | 15 个文件，含 `.github/templates/action.template.yml`（+11 行：`reuse` 输入、3 处 `if:` 守卫、注释） |
| 旧 key | base `a359ecd91acc` → head `1f37400b0deb`（不同 → 缓存未命中 → 真重建） |
| 渲染对比 | `run:` 块两侧均为 `f0561413c081`；`steps/*.sh` 两侧均为 `67bf97769e31`（**完全相同**） |

结论：模板这次只改了「步骤何时执行」，没改「步骤做什么」。

## 2. 改法

### A. key 改为哈希「渲染后的构建命令」

`scripts/build_key.py`：

- 哈希 fetch / patch / build 三步的 `run:` 块（含镜像、挂载、工作目录、环境变量、参数）；
- 哈希 `steps/{fetch,patch,build}.sh` 的内容（fetch/patch 由渲染器生成，build 是配方片段）；
- 哈希补丁文件内容（patch 步骤运行期按路径读取，文件名出现在脚本里、内容不在）；
- 哈希镜像 ID 与 `--extra`。

不再直接哈希模板文件、`scripts/`、`docker/`、`test`/`publish` 片段、`.stat` 的发布字段。
渲染由 key 自己完成，`STEPS_DIR` 固定为 `.rendered/action`，因此 key 与本地输出目录无关。

### B. detect 先全量候选、再按 key 差异收窄

`scripts/detect_targets.py`：

- `path_rules()` 把「显式变更的配方」与「全局输入变更 → 全量候选」分开返回；
- `narrow_by_build_inputs()` 为每个候选比较 base 与 head 的 key，相等即跳过；
- base 侧优先执行 **base 自己的** `scripts/build_key.py`（渲染器改动因此体现为 key 变化），
  base 上还没有该工具时用当前工具渲染 base 树（过渡期行为）；
- 无法比较（无 base commit、渲染失败、工作树不可用）→ 保留全部候选；
- `docker/**` 变更 → 直接全量（镜像 digest 在 detect 阶段不可知）；
- 只改 `commands/test` 这类显式变更的配方永远保留，因为新测试必须真跑一遍。

## 3. 验证

| 检查 | 结果 |
|---|---|
| key 不敏感项 | 模板加输入 / 加 `if:` / 改注释 / 改 test 步骤 / `.stat` 改发布 registry → key 不变 |
| key 敏感项 | 改 fetch 步骤、改 build 步骤、改 upstream ref、改补丁内容、换镜像 ID、`--extra` → key 变化 |
| 重放 PR #3 | 候选 2 条 → 收窄后只剩 `@ruyisdk/utf-8-validate@6.0.6`；bufferutil 打印 `build inputs unchanged (key bb563c29406f), skipping` |
| L0 | 86 个用例通过（新增 17 个） |
| L1 + 复用 | 真构建 2m05s → key `800d52552cdb`（两次一致）→ 打包 6.6 MB → 恢复后 `--reuse`：fetch/build skipped、test 通过、publish 成功 |

## 4. 推上去之后要看的

1. 只改模板输入/守卫/注释的 PR：detect 日志应出现 `build inputs unchanged (key ...), skipping`，
   `has_targets=false`，不构建也不发布。
2. 改构建命令（模板或配方）的 PR：对应配方被选中并真构建。
3. 改 `scripts/`：`self_check=true`（跑 `--check-all`），且只有渲染命令真的变了的配方被选中。
4. 首次跑会有一次「旧 key 与新 key 不通用」的冷启动：现有的 `<slug>-tree-<oldkey>` 产物不会被
   命中，全部重建一次并上传新 key 的产物。
