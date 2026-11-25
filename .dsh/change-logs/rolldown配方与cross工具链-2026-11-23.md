# 第三个配方 @ruyisdk/rolldown 1.3.3 与 cross 工具链

日期：2026-11-23
目的：用 rolldown（上游只发 JS 外壳 + 分平台绑定、且不含 riscv64）验证「需要专门伺候」的包能不能落成配方；
结论是**源码一行没改**，要新增的是「交叉构建」这条工具链路线（D26）。

## 1. 上游现状（实测，不是推测）

| 事实 | 证据 |
|---|---|
| 上游不产 riscv64 绑定 | `napi.targets` 里没有 riscv64；npm 上 `@rolldown/binding-linux-riscv64-gnu` 只有 `0.0.0` 空占位 |
| 外壳**已经**认 riscv64 | 发布包 `dist/shared/binding-*.mjs` 里就有 riscv64 分支：先 `require('./rolldown-binding.linux-riscv64-gnu.node')`（相对 `dist/shared/`），再退到 `@rolldown/binding-linux-riscv64-gnu` |
| `files` 不会漏掉我们的绑定 | `"files": ["dist", …, "!dist/*.node"]`：`!dist/*.node` 只匹配 `dist/` 的**直接子文件**，`dist/shared/*.node` 会进 tarball（测试里有 `npm pack --dry-run` 断言） |
| Rust 源码零改动可编译 | `cargo build --release -p rolldown_binding --target riscv64gc-unknown-linux-gnu`（rustc 1.100.0 + 交叉 gcc 12.2.0）：3m21s、19 MB `stripped` RISC-V ELF、无 riscv 相关警告 |

所以「必须打补丁」这个预期**没有出现**；卡点是构建载体与 JS 外壳的来源。

## 2. 为什么 JS 外壳必须从 npm tarball 取

`dist/` 是 `@oxc-node/core` 驱动、并且要用 rolldown 自己打包自己（自举），而
`@oxc-node/core` 的 16 个平台包**没有 riscv64**。因此 riscv64 上没有可行的「从源码重建外壳」路径。

配方选择：**外壳取上游已发布 tarball（sha512 钉死）**，只重建原生绑定。这样发出去的 JS 与上游逐字节相同，
差异只在绑定；代价是 `fetch` 变成双来源、需要配方自写（D13 的「配方可覆盖」机制天然支持）。

## 3. 为什么新增 cross（D26）

| 路线 | 实测 |
|---|---|
| qemu/riscv64 容器原生编译 | 10 分钟连 `target/…/deps` 都还没冒出文件（每次 `rustc` 数分钟）；数百 crate 推算数小时，逼近 6h 上限 |
| amd64 交叉编译 | 3m13s（L1 实测） |
| 测试 | **仍在 riscv64 容器**：交叉只搬走编译，不搬走验证 |

实现：`.stat` 增 `toolchain: native|cross`（缺省 native ⇒ 老配方渲染不变），渲染器给变量
`build.platform|image|dockerfile`、`test.platform|image`、`riscv.triple`、`job.cross`；模板只引用变量，
`{{#if job.cross}}` 多一段工具链自检；新增 `docker/riscv64-cross.Dockerfile`；
`toolchain-image` 工作流改成矩阵推 `:riscv64` 与 `:cross`；`build.yml` 解析并拉取两个镜像。

## 4. 验证

| 检查 | 结果 |
|---|---|
| 镜像自检 | `rustc 1.100.0`、已装 `riscv64gc-unknown-linux-gnu`、`riscv64-linux-gnu-gcc 12.2.0` |
| L1 全链（cross） | fetch 双源（tarball 过 sha512）→ build 3m13s → 19 MB RISC-V ELF → test：`npm pack` 断言绑定会进 tarball（59 文件 / 20.5 MB 未压缩）→ riscv64 上真实 `rolldown()` 打包成功 |
| 老配方不受影响 | 模板改动后 bufferutil 的构建 key 仍是 `bb563c29406f`（渲染出的 fetch/build 命令逐字节不变）⇒ CI 不重建，只跑 test/publish |
| L0 | 92 个用例通过（新增 toolchain 渲染/校验、detect payload 字段） |
| `--check-all` | 3 个配方全部 OK |
| L2（verdaccio，`--reuse --publish`） | `+ @ruyisdk/rolldown@1.3.3`；发布日志显示 `publishConfig.registry https://registry.npmjs.org/ -> http://127.0.0.1:4873` |
| 消费者复验 | 用**已发布的 tarball** 在 riscv64 上 `npm install`（added 3 packages）→ `import "@ruyisdk/rolldown"` → 打包成功 |

verdaccio 演练需要把 `max_body_size` 抬到 100mb（默认 10mb 会 413，因为包里带着 19.5 MB 的绑定）。

## 5. 踩到的坑

1. **交叉镜像缺 `cmake`**：`libmimalloc-sys2`（rolldown 的分配器）用 CMake 编 mimalloc C 源码，失败信息只有
   `is cmake not installed?`。已加入镜像，失败暴露在镜像构建期而不是每次 CI。
2. **上游清单的 `publishConfig.registry` 压过 `--registry`（通用 bug，改在发布机制里）**：
   rolldown 的清单钉了 `https://registry.npmjs.org/`，`npm publish` 因此无视配方 registry 直奔
   npmjs（首次 L2 演练报 `ENEEDAUTH ... registry.npmjs.org`）。任何带 `publishConfig` 的上游清单都会
   踩。修法：`generate` 模式把 `publishConfig.registry` 改写为配方 registry（保留 `access` 并打日志）；
   `verify` 模式（渲染器不改清单）发现不一致直接硬失败。L0 增加 4 个用例覆盖两种模式。
3. **npm 的位置参数会被当包名**：`npm pack out/rolldown` 会去 `git ls-remote ssh://git@github.com/out/rolldown.git`
   （`out/rolldown` 被解析成 GitHub 简写 owner/repo），`npm install --prefix` 同理。配方改成先 `cd` 再执行。
3. **交叉镜像的 rustup 目录要可写**：容器按调用者 uid 运行，而源码树可能自带 `rust-toolchain.toml`
   要求另一版工具链；镜像因此把 `RUSTUP_HOME`/`CARGO_HOME` 设为任意 uid 可写，`CARGO_HOME` 另指到
   工作区以保住跨步骤的 cargo 缓存。

## 6. 已知限制

cross 镜像没有 riscv64 的系统 C 库与头文件：若某个包除 Rust 外还链接 riscv64 系统库，要么回 native，
要么给镜像加 `dpkg --add-architecture riscv64` 与对应 `-dev` 包。本轮 rolldown 只需交叉 gcc 做链接。

## 8. 拉取镜像报 `no matching manifest for linux/amd64`

镜像推成功了，但 `build.yml` 拉 `:riscv64` 时报：

```
Error response from daemon: no matching manifest for linux/amd64 in the manifest list entries
```

原因：`buildx build --platform linux/riscv64` 推上去的是**只含 riscv64 的 manifest list**。
`docker pull` 不给 `--platform` 时按**宿主平台**（runner 是 amd64）去要，索引里没有就报错。交叉镜像
（amd64）在 amd64 runner 上刚好命中，所以只有 riscv64 那个失败。

改法：`build.yml` 的 Resolve 步骤额外导出 `RISCV_IMAGE_PLATFORM` / `RISCV_TEST_IMAGE_PLATFORM`
（native → `linux/riscv64`；cross → build `linux/amd64` / test `linux/riscv64`），
Pull 步骤与模板的 prepare 步骤都改成 `docker pull --platform <platform>`（`docker image inspect`
命中本地镜像时跳过拉取）。L0 增加断言，防止有人把 `--platform` 从 pull 上拿掉。

顺带确认：`docker build` 那条 `FromPlatformFlagConstDisallowed` 警告是**故意保留**的——两个
Dockerfile 各自只服务一个平台，写死 `FROM --platform` 能让「在错误平台上构建」直接失败，而不是悄悄
产出一个架构不对却挂着 riscv64 名字的镜像。
