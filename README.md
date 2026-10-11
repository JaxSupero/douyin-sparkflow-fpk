# 抖音 SparkFlow — 飞牛 fnOS 原生 FPK 打包工程

把 [halfwaystudent/douyin-sparkflow](https://github.com/halfwaystudent/douyin-sparkflow) 打包为飞牛 fnOS
**原生应用**（不使用 Docker），随应用中心安装、开箱即用。

## 交付物

```text
dist/fnos-douyin_sparkflow_v1.1.0.fpk          # ~387 MB
dist/fnos-douyin_sparkflow_v1.1.0.fpk.sha256
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
| 浏览器 | Playwright Chromium 1194 预置在 `app/dist/chrome`（跟随上游 requirements 锁定版本） |
| 登录桌面 | Xvfb + fluxbox + x11vnc + websockify/noVNC，全部随包（`app/dist/display`、`app/dist/novnc`） |
| 字体 | 文泉驿微米黑/正黑 + Noto Color Emoji，随包 |
| 调度器 | 内置 `cron_runner.py` 轮询数据目录 spool（**不写系统 crontab**） |
| 源码补丁 | 3 个文件 9 处环境变量开关，默认值不变，锚点严格校验 |

安装/升级**不联网、不构建镜像**：依赖与浏览器全部在包内，`bootstrap.py` 首次启动时物化环境。

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
│   ├── dist/chrome/             # Playwright Chromium（924 MB）
│   ├── dist/display/            # X/VNC 组件 + 字体 + xkbcomp 垫片（54 MB）
│   ├── dist/novnc/              # noVNC 1.5.0 静态资源
│   └── ui/                      # 桌面入口（8787 端口）
└── scripts/
    ├── prepare-deps.sh          # 下载并准备全部离线依赖（构建机执行一次）
    ├── build-fpk.sh             # 同步源码 → 打补丁 → 权限/软链归一化 → fnpack build → 校验
    ├── patch-src.py             # fnOS 原生部署补丁（锚点不匹配即失败）
    ├── update-upstream.py       # CI 用：固定上游 SHA + 递增 manifest patch 版本
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