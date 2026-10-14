# 抖音 SparkFlow — 飞牛 fnOS 原生 FPK 打包工程

把 [halfwaystudent/douyin-sparkflow](https://github.com/halfwaystudent/douyin-sparkflow) 打包为飞牛 fnOS
**原生应用**（不使用 Docker），随应用中心安装、开箱即用。

## 交付物

```text
dist/fnos-douyin_sparkflow_v<version>.fpk      # v1.1.6 实测 337,274,011 B（≈322 MiB）
dist/fnos-douyin_sparkflow_v<version>.fpk.sha256
```

`build-fpk.sh` 每次构建后把实际 SHA-256 写入同名 `.sha256` 文件，以该文件为准。
注意 **fnpack 的产物不保证逐字节可复现**（同一源码两次构建的 SHA 可能不同）；
CI 里用 tar+gzip 兜底构建时带 `--mtime='@0'`，才是可复现的。需要校验一致性时，
比对包内 `manifest.checksum` 与 `app.tgz` 的 MD5，而不是外层 FPK 的 SHA。

## 方案要点

| 项 | 做法 |
| --- | --- |
| 运行时 | `install_dep_apps=python312:nodejs_v24`（应用中心自动安装） |
| Python 依赖 | uv 交叉安装 manylinux2014_x86_64 轮子（含 websockify），预置在 `app/dist/deps` |
| 虚拟环境 | 启动时按需物化轻量 venv（符号链接 + `pyvenv.cfg`），依赖指纹变化才重建 |
| 浏览器 | Playwright Chromium 1194 预置在 `app/dist/chrome`（跟随上游 requirements 锁定版本）；**只保留完整 Chromium**，无头任务经 `channel="chromium"` 复用它（`patch-src.py` 注入） |
| 登录桌面 | Xvfb + fluxbox + x11vnc + websockify/noVNC，全部随包（`app/dist/display`、`app/dist/novnc`） |
| 字体 | 文泉驿微米黑/正黑 + Noto Color Emoji，随包 |
| 调度器 | 内置 `cron_runner.py` 轮询数据目录 spool（**不写系统 crontab**） |
| 源码补丁 | 5 个文件 11 处（环境变量开关 + Chromium channel），默认值不变，锚点严格校验 |
| 显示栈裁剪 | 按 ldd 精确闭包只保留实际加载的 110 个 `.so`；未加载的 Mesa 驱动/LLVM/ICU 一律不带（`prune-display.py`，构建后以 `ldd` 零缺失为硬门槛） |

安装/升级**不联网、不构建镜像**：依赖与浏览器全部在包内，`bootstrap.py` 首次启动时物化环境。

## 包体积

| 版本 | 体积 | 说明 |
| --- | --- | --- |
| v1.1.5 | 744,912,218 B（710.4 MiB） | 精简前 |
| v1.1.6 | 337,274,011 B（321.6 MiB） | **-54.7%**，功能不变 |

两处缩减，均已实测（无头任务、noVNC 扫码登录、中文/Emoji 渲染逐项验证通过）：

- **-323 MiB**：不再打包 `chromium_headless_shell`。Playwright 1.49+ 的
  `headless=True` 默认启动该二进制，删掉后由 `patch-src.py` 给启动参数注入
  `channel="chromium"`，无头任务改用完整 Chromium（自带 SwiftShader 软件光栅化）。
- **-805 MiB**：`dist/display` 按 ldd 精确闭包裁剪（913 MB → 108 MB）。
  X/VNC 与 Chromium 实际只加载 110 个共享库；原包被传递依赖拖入的
  Mesa GPU 驱动（`dri/` 320 MB）、`libLLVM-15`（224 MB）、`libicu72`、
  `libz3`/`libgs`/`libx265` 等从未被加载，一并移除。
  裁剪后构建脚本以 **`ldd` 零缺失为硬门槛**，缺一个库即中止出包。

> `KEEP_HEADLESS_SHELL=1 scripts/prepare-deps.sh` 可临时保留 headless_shell（排查浏览器问题用）。

## 目录结构

```text
douyin-sparkflow-fpk/
├── manifest                     # appname/version/依赖/端口/入口
├── ICON.PNG / ICON_256.PNG
├── LICENSE                      # 上游 PolyForm Noncommercial 1.0.0
├── cmd/                         # 生命周期脚本（start/stop/status/install/upgrade/uninstall）
├── config/privilege|resource    # run-as: package（专用包用户，无 root）
├── wizard/install|uninstall     # 安装说明 / 卸载是否保留数据
├── app/
│   ├── src/                     # 上游源码（构建时同步 + 打补丁）
│   ├── scripts/                 # 运行时脚本：bootstrap.py、login_stack.sh、web.sh、scheduler.sh、common.sh
│   ├── dist/deps/               # Python 3.12 依赖（233 MB）
│   ├── dist/chrome/             # Playwright Chromium 完整版（602 MB，不含 headless_shell）
│   ├── dist/display/            # X/VNC 组件 + 字体 + xkbcomp 垫片（142 MB，已按闭包裁剪）
│   ├── dist/novnc/              # noVNC 1.5.0 静态资源
│   └── ui/                      # 桌面入口（8787 端口）
└── scripts/
    ├── prepare-deps.sh          # 下载并准备全部离线依赖（构建机执行一次）
    ├── prune-display.py         # 按 ldd 精确闭包裁剪显示栈共享库（构建硬门槛：零缺失）
    ├── build-fpk.sh             # 同步源码 → 打补丁 → 权限/软链归一化 → fnpack build → 校验
    ├── patch-src.py             # fnOS 原生部署补丁（锚点不匹配即失败）
    ├── update-upstream.py       # CI 用：固定上游 SHA + 递增 manifest patch 版本
    ├── gen-changelog.py         # CI 用：区间提交 → 中文用户向更新说明（写回 manifest）
    └── xkbcomp-shim.c           # LD_PRELOAD 垫片（Xvfb 找 /usr/bin/xkbcomp）
```

## 构建

前置：`fnpack`（飞牛官方）、`gcc`、`python3`、`uv`（脚本自动准备）、
上游源码默认在 `../douyin-sparkflow/DouYinSparkFlow`（可用 `SRC_DIR=` 覆盖）。

```bash
bash scripts/prepare-deps.sh   # 一次性：Python 依赖 + Chromium + X 组件 + 字体 + noVNC
bash scripts/build-fpk.sh      # 产出 dist/*.fpk 并自检
```

`build-fpk.sh` 内置校验：必备成员、`manifest.checksum` 与 `app.tgz` MD5 一致、
关键文件存在、JSON 合法、`cmd/main` 执行位、补丁已生效。

## 更新日志（changelog）

`scripts/gen-changelog.py` 在每次自动更新时生成**中文用户向**更新说明，同时写入
`manifest` 的 `changelog` 字段、`changelog.txt`，并成为 GitHub Release 的正文。

为什么不直接用上游 `CHANGELOG.md`（实测数据）：

| | 实测 |
|---|---|
| 上游 CHANGELOG 日期小节 | 5 个（05-30 / 07-11 / 08-25 / 08-27 / 10-13） |
| 有提交但无对应小节的日期 | **34 个** |
| 未被记录的提交 | **133/151 = 88%** |

而且上游那 5 个小节是 Markdown 英文技术日志（含 webhook、锁文件一类的实现细节），
`manifest` 的 `changelog` 是**单行 INI 值**，官方要求「简洁、面向用户表达」。

因此生成策略是：

1. 取 `config/bootstrap/upstream.env` 里上一个 pin 与本次 pin 之间的 commit
   （首次运行时退化为上游最近 30 个提交）；
2. 按 conventional commits 分类：`fix → 修复`、`feat → 新增`、`perf → 优化`，
   `docs` / `test` / `ci` / `chore` / `refactor` 直接丢弃（用户无感）；
3. 用项目领域词表把英文 subject 中文化；**命中不了的整条丢弃**——
   宁可少写一条，也不在应用中心输出半中半英的说明；
4. 上游 CHANGELOG 只用于校验「本次区间确有手写摘要」，不作为文案来源；
5. 同类型最多 4 条、总计 8 条、整段上限 420 字符。

词表与整句映射在 `scripts/gen-changelog.py` 的 `VOCAB` / `_PHRASE` 里，
新增上游高频词时按需补充。

## 自动跟踪上游与 GitHub Actions

工程内提供两条 workflow：

- `.github/workflows/auto-update.yml`：每天 3 次检查上游 `main` 的 HEAD commit SHA。只有 SHA 变化才更新 `config/bootstrap/upstream.env`、递增 manifest patch 版本并请求构建；无变化不提交、不触发构建。
- `.github/workflows/build.yml`：手动或 push 可构建；自动更新时由 `repository_dispatch` 接力。默认 `GITHUB_TOKEN` 的 push 不会触发普通 `push` workflow，因此不要改成只依赖自动提交触发构建。
- 构建成功后上传滚动版 Release `latest` 和固定版本 Release `vX.Y.Z`，附件包括 `.fpk` 与 `.sha256`。

### 推送到你自己的 GitHub 仓库

在 GitHub 新建一个空仓库（建议 Private；若公开发布，请确认上游 PolyForm Noncommercial 许可和品牌说明），然后在有该仓库写权限的终端执行：

```bash
cd /vol2/1000/AI/douyin-sparkflow-fpk
git init -b main
git add .
git commit -m "Initial native fnOS FPK packaging and CI"
git remote add origin https://github.com/<你的账号>/douyin-sparkflow-fpk.git
git push -u origin main
```

若目录已经有 `.git`，改为 `git remote add` 或 `git remote set-url origin ...`，不要重复 `git init`。之后在仓库 **Settings → Actions → General → Workflow permissions** 允许 `Read and write permissions`，否则自动版本提交、`repository_dispatch` 和 Release 上传会失败。首次推送后在 **Actions** 页面手动运行 `Auto-update upstream`；若当前 SHA 尚未 pin，它会写入首个 SHA 并接力构建。也可手动运行 **Build fnOS FPK**。

`repository_dispatch` 与 Release 都由仓库自己的默认 `GITHUB_TOKEN` 完成，不需要把 PAT/token 放到 workflow 文件里。请只在首次运行后验证两个 workflow 均成功，再将下游自动安装流程指向 Release 的滚动版资产。GitHub 定时触发属于尽力服务，建议偶尔检查 Actions 的 schedule run 历史。

### CI 依赖行为

- Actions 只在 runner 的 `$GITHUB_WORKSPACE/.cache` 放临时 apt 状态与下载文件；不会改写 runner 的 `/etc/apt`。
- Python 依赖、Chromium、X 组件、字体和 noVNC 分别根据版本/内容 fingerprint 复用缓存；`workflow_dispatch` 可勾选 `force_rebuild_deps` 强制重建。
- 上游 commit SHA 固定后，依赖与源码构建使用同一个 commit，避免两次 fetch 取到不一致 HEAD。

## 实机验证结论（本机 = 目标机型 Debian 12 / x86_64）

- X 显示栈：Xvfb（经 xkbcomp 垫片）+ fluxbox + x11vnc + noVNC 全部正常，`vnc.html` HTTP 200
- Chromium：头显/无头均可；中文与 emoji 正常渲染；键盘输入正常
- 跨路径：Chromium 移出构建目录后仍可启动（无路径依赖）
- 抖音登录页：加载完整、二维码可提取（`/qr` 返回 178×178 PNG）
- 调度器：spool 触发按时执行
- `cmd/main`：start ≈3 s、stop ≈1.3 s、status 正确返回 0/3，零残留进程、端口全释放

## 已知取舍

- **平台限定 x86_64**（`platform=x86`）。包内是 x86-64 ELF（Chromium / Python 扩展 / X 组件），
  不会在 arm64 设备上安装；需 arm64 时用 `uv --python-platform aarch64` + 对应架构 Chromium 重新构建。
- **Xvfb 的 xkbcomp**：xorg 写死 `/usr/bin/xkbcomp`，宿主非 root 无法满足，
  用 `LD_PRELOAD` 垫片重定向到随包二进制（见 `scripts/xkbcomp-shim.c`）。
- **无 host crontab 写入**：调度 spool 完全在应用数据目录，避免污染系统 crontab。
- 软链接在打包时统一替换为实体文件（fnOS 解压后统一设权限，软链易出异常）。

## 升级自 Docker 版（v1.0.0）

数据都在 `${TRIM_PKGVAR}`（账号 / 登录态 / 配置 / 日志），`install_callback` 只替换代码目录，
用户数据保留。旧版数据目录属主为 `docker-douyin-sparkflow`，`install_init` 会在 root 语境下
自动 `chown` 到新的包用户；若安装仍报权限问题，在 NAS 终端执行：

```bash
chown -R douyin_sparkflow:douyin_sparkflow /vol1/@appdata/douyin_sparkflow
```

## 免责声明

上游项目为第三方非官方开源项目（PolyForm Noncommercial 1.0.0，仅限非商业用途），
与抖音无任何关联。自动化操作可能触发平台风控，使用者自行承担风险。
