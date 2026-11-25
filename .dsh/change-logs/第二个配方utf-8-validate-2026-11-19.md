# 第二个配方：@ruyisdk/utf-8-validate 6.0.6

日期：2026-11-19
目的：按 D21 的备选落第二个真实配方，并在真实配方上复验上一轮的 artifact 复用。

## 1. 上游选型

- 仓库 `websockets/utf-8-validate`，tag `v6.0.6`（与 bufferutil 同族，prebuildify + node-gyp-build 布局）。
- 确认**没有 `.gitmodules`**：`deps/is_utf8` 是 vendored 源码，所以生成的 `fetch`（普通 `git clone --depth 1 --branch <tag>`）足够，不需要 submodule 支持。
- 6.0.6 是 **N-API**（`src/validation.cc` 以 `NAPI_MODULE(NODE_GYP_MODULE_NAME, Init)` 结尾，`Init` 直接返回函数），一份制品覆盖所有 Node 版本，不需要按 ABI 出多份 prebuild。

## 2. 一个真实差异：target 名 ≠ 包名

`binding.gyp` 的 target 是 `validation`，产物为 `build/Release/validation.node`；而 node-gyp-build（以及上游 prebuildify 的布局）按**包名**找文件：`prebuilds/<platform>-<arch>/utf-8-validate.node`。

所以 `commands/build` 里必须**改名拷贝**：

```bash
cp build/Release/validation.node prebuilds/linux-riscv64/utf-8-validate.node
```

这一条写进了配方注释——如果照抄 bufferutil 的写法（`cp build/Release/<pkg>.node ...`），会得到「构建成功但测试解析不到 prebuild」的假阴性。

## 3. 测试策略

沿用 bufferutil 的结构（隐藏 `build/` → 让 node-gyp-build 按消费者方式解析）：

1. `file` + `grep -Eqi 'risc-?v'` 断言 ELF 是 RISC-V；
2. `node-gyp-build.path()` 必须指向 `prebuilds/linux-riscv64/utf-8-validate.node`；
3. `require` 该路径——dlopen 成功即证明这份二进制能在 riscv64 上加载（不是 x86 误标）；
4. 5 条行为断言：空缓冲、ASCII、4 字节序列合法；lone surrogate（`ed a0 80`）与截断序列（`c3`）非法。

## 4. 验证结果

| 检查 | 结果 |
|---|---|
| `--check-all` | 2 个配方，0 失败 |
| L0 | 70 个用例通过 |
| L1 | 全绿，产物 `ELF 64-bit LSB shared object, UCB RISC-V, RVC, double-float ABI`，`utf-8-validate ok: ... answered 5 cases` |
| 变更检测 | 只改新配方 → 只选它；改模板 → 两个都选；改渲染器 → 只自检 |
| **artifact 复用** | 真构建后打包 6.6 MB → 恢复到新沙箱 → `--reuse`：`fetch`/`build` skipped、`test` 通过、`publish` 成功 `+ @ruyisdk/utf-8-validate@6.0.6` |
| key 敏感性 | `commands/build` 加一行注释 → `2de2921bfb27` → `751331d8f064`；还原后回原值 |

## 5. 推上去之后要看的

1. 首次 main 跑：两条配方各构建一次，各自上传 `...-tree-<key>`。
2. 再推一次（或 `--allow-empty` 提交 / 重跑同一 workflow）：应看到 `hit: restored ...-tree-<key> from run ...`，`fetch` 与 `build` 两步 skipped，`test`/`publish` 照常。
3. 若只改其中一个配方：只有它重建（另一条的 key 不变但也不会被选中）。
