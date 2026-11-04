# cs2slim-cli

CS2 精简服务端命令行工具（用户侧）。

本仓库是 **cs2-slim-replica**（纯自动构建仓库）的用户侧工具仓库，提供：

- `cs2slim/`：CLI 包（pip 可装；`cs2slim` 命令，子命令 `init` / `download` / `extract` / `build` / `run` / `package` / `all` / `prebuilt` / `status` / `upgrade` / `clean`）
- `scripts/get-cs2slim.sh` / `get-cs2slim.ps1`：一键安装（拉取 Release 包 + 预构建）
- `scripts/prebuilt.sh`：纯 bash 预构建模式（零 Python，供无 Python 环境 / CI）
- `scripts/make_zipapp.sh`：构建 `cs2slim.pyz`（单文件 zipapp 可执行包）
- `cs2slim/registry/`：组件目录本地副本（从 cs2-slim-replica 同步，供离线回退）

## 安装

```bash
# pip（PyPI，推荐）
pip install cs2slim

# Linux（curl 一行，免 pip）
curl -fsSL https://raw.githubusercontent.com/cyqmq/cs2slim-cli/main/scripts/get-cs2slim.sh | bash

# 或单文件 zipapp（Release 附带 / 本地构建）
python3 scripts/make_zipapp.sh && ./cs2slim.pyz --help
```

> PyPI 自动发布（OIDC 可信发布，无需 token）：仓库已配置 `.github/workflows/publish-pypi.yml`，push 形如 `vX.Y.Z` 的 tag 时自动构建、发布到 PyPI 并创建 GitHub Release。需在 PyPI 侧登记 Trusted Publisher：Project → Settings → Publishing → Add trusted publisher（PyPI project: `cs2slim`，Owner: `cyqmq`，Repository: `cs2slim-cli`，Workflow: `publish-pypi.yml`，Environment: 留空）。

## 使用

```bash
cs2slim init            # 生成配置模板
cs2slim prebuilt        # 拉取 Release 精简包 + 地图/功能，拼装到 cs2-build/slim
cs2slim run             # 启动服务端（委托 start_server.sh）
cs2slim status          # 状态 + 已装/最新版本对比
cs2slim upgrade         # 检查附属仓库预构建版本并升级
cs2slim clean           # 清理下载缓存
```

## 版本自检与升级

- `prebuilt` / `all` 安装完成后在精简树写入 `.cs2slim-version.json`，记录 core/maps/features 各组件版本。
- `status` 对比已装版本与附属仓库（cs2-slim-replica / cs2-slim-maps / cs2-slim-features）最新 Release。
- `upgrade` 检测到新版本后自动重新拉取对应预构建包并解压，核心升级时自动重跑 prebuilt。

## 供应链校验与下载加速

- **SHA256 校验**：所有下载产物（核心包 / 地图 / 功能 / DepotDownloader / CLI 自更新）均校验 SHA256（Release 挂 `.sha256` 边车或 GitHub API `digest`）；框架组件上游直装按官方资产 digest 校验。`--no-verify` 可绕过（默认关闭，不建议）。
- **框架组件上游直装**：metamod / css / swiftly / plugify / modsharp（含 win 版）在 prebuilt 模式下直接从各自官方 GitHub Release 下载（`source=upstream`），不再依赖 cs2-slim-features 发布包；metamod 自动取 CS2 版 prerelease（2.0.x）。
- **GitHub 镜像加速**：国内网络可配置镜像，优先级 `--gh-proxy` > `slim.yaml gh_proxy` > 环境变量 `CS2_GH_PROXY`：

  ```yaml
  # slim.yaml
  gh_proxy: https://gh-proxy.com
  ```

  ```bash
  # 或命令行（覆盖 config）
  cs2slim prebuilt --config slim.yaml --gh-proxy https://gh-proxy.com
  # 或环境变量（兜底）
  export CS2_GH_PROXY=https://gh-proxy.com
  ```

  镜像只改传输地址，内容不变，SHA256 校验仍生效。

## 与构建仓库的关系

- 组件目录（`registry/`）由 [cs2-slim-replica](https://github.com/cyqmq/cs2-slim-replica) 维护，是本仓库的权威来源。
- CLI 默认优先从 cs2-slim-replica 的 raw 拉取最新 registry（可用 `--registry-base` 覆盖），本地副本仅作离线回退。
- 本仓库不包含构建管线（`core/`、`scripts/updater`、workflows 等），构建相关请见 cs2-slim-replica。
