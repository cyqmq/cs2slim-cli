#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cs2slim - CS2 精简服务端 配置驱动 CLI（用户侧工具）

多仓库架构:
  - 本仓库 (cs2slim-cli) : CLI + 一键安装脚本（用户侧）
  - cs2-slim-replica     : 构建仓库（核心配方 + 组件注册表权威来源）
  - cs2-slim-maps        : 地图组件仓库 (filelist 片段 + 预构建包)
  - cs2-slim-features    : 功能组件仓库 (bots/gotv 等)

子命令:
  init      生成 slim.yaml 配置模板
  download  按配置组合 filelist 并调用 DepotDownloader 下载
  extract   提取 loose files
  build     组装精简树 (核心 + 选配地图)
  package   打包
  run       启动服务端
  prebuilt  预构建模式（拉取 Release 包自动拼装）
  status    查看状态（含版本自检）
  upgrade   检查附属仓库预构建版本并升级
  clean     清理下载缓存

示例:
  python -m cs2slim.cli init
  python -m cs2slim.cli download --config slim.yaml
  cs2slim prebuilt --platform linux --maps de_dust2,de_mirage --features bots

依赖: 仅 Python 3 标准库 (无需 PyYAML)。
"""

__version__ = "1.0.7"

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile


def _zipapp_zip_path():
    """返回当前以 zipapp 运行的 pyz 绝对路径；非 zipapp 模式返回 None。"""
    mod_file = os.path.abspath(__file__)
    for marker in ("cs2slim.pyz", ".pyz", ".zip"):
        idx = mod_file.find(marker)
        if idx != -1:
            return mod_file[: idx + len(marker)]
    return None


def _extract_zipapp_resources():
    """zipapp 运行时引导：把内置数据目录解到缓存，返回可读目录；常规源码运行返回 None。

    当以单文件 cs2slim.pyz 运行时，__file__ 形如 '<path>/cs2slim.pyz/__main__.py'，
    数据文件在 zip 内无法直接 open()，首次运行解到 ~/.cache/cs2slim/<hash> 并复用。
    """
    zip_path = _zipapp_zip_path()
    if not zip_path:
        return None
    import hashlib
    st = os.stat(zip_path)
    # 缓存键含路径+mtime+size：重建的 pyz 不会复用旧布局缓存
    h = hashlib.md5(f"{zip_path}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8")).hexdigest()[:8]
    cache = os.path.join(os.path.expanduser("~"), ".cache", "cs2slim", h)
    data_dir = os.path.join(cache, "cs2slim")
    if os.path.exists(os.path.join(cache, ".extracted")) and os.path.exists(os.path.join(data_dir, "registry")):
        return cache
    os.makedirs(cache, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        # 防 zip-slip（_safe_join 定义在后面，模块加载期无法调用，此处内联）
        cache_real = os.path.realpath(cache)
        for info in z.infolist():
            target = os.path.realpath(os.path.join(cache_real, info.filename))
            if target != cache_real and not target.startswith(cache_real + os.sep):
                raise ValueError(f"非法解压路径: {info.filename}")
        z.extractall(cache)
    open(os.path.join(cache, ".extracted"), "w").write("")
    return cache


_ZIPAPP_RES = _extract_zipapp_resources()
# zipapp 解包后数据在 <cache>/cs2slim/；源码/pip 包模式为包目录自身
CLI_DIR = os.path.join(_ZIPAPP_RES, "cs2slim") if _ZIPAPP_RES else os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(CLI_DIR, "core")
SCRIPTS_DIR = os.path.join(CLI_DIR, "scripts")
REGISTRY_DIR = os.path.join(CLI_DIR, "registry")

PLATFORM_DEPOT = {
    "linux": ("2347773", "filelist_2347773.txt"),
    "win64": ("2347771", "filelist_2347771.txt"),
}
BIN_DIR = {
    "linux": "game/bin/linuxsteamrt64/cs2",
    "win64": "game/bin/win64/cs2.exe",
}

# 全局输出控制（由 --quiet / --no-progress 设置）
QUIET = False
PROGRESS = True


def _info(msg):
    """信息输出，--quiet 时抑制（错误仍走 stderr/sys.exit）。"""
    if not QUIET:
        print(msg)


def _warn(msg):
    """警告输出（stderr，不受 --quiet 抑制）。"""
    print(f"[警告] {msg}", file=sys.stderr)


_ARGS = None  # 当前 argparse 命名空间（供 download_file 读取 --no-verify）


def _progress_ok():
    """是否启用 \r 实时进度：需 PROGRESS 且 stdout 为 TTY（非 TTY 自动降级为逐行）。"""
    return PROGRESS and sys.stdout.isatty()


# ---------------------------------------------------------------------------
# 极简 YAML 子集解析（满足 slim.yaml 需求，零依赖）
# ---------------------------------------------------------------------------
def _strip_inline_comment(line):
    """去掉行尾 YAML 注释（` # ...`），引号内的 # 保留。"""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            # 注释需以空白或行首开头（避免误伤 hostname: "my#tag" 这类）
            if i == 0 or line[i - 1] in " \t":
                return line[:i].rstrip()
    return line.rstrip()


def parse_yaml_simple(text):
    """解析极简 YAML：顶层 `key: value`、`key:` + 缩进 `- item`、内联 `[a, b]`、
    以及段内嵌套 `key: value`（如 server 配置段）。支持行尾 ` # 注释`。"""
    data = {}
    section_key = None
    for raw in text.splitlines():
        line = _strip_inline_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line.strip()
        if content.startswith("#"):
            continue
        if indent == 0 and ":" in content and not content.startswith("- "):
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            section_key = None
            if val == "":
                data[key] = []
                section_key = key
            elif val.startswith("[") and val.endswith("]"):
                data[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val.startswith("{") and val.endswith("}"):
                d = {}
                for part in val[1:-1].split(","):
                    if ":" in part:
                        k, _, v = part.partition(":")
                        d[k.strip().strip("'\"")] = v.strip().strip("'\"")
                data[key] = d
            else:
                data[key] = val.strip("'\"")
        elif indent > 0 and content.startswith("- "):
            if section_key is not None and isinstance(data.get(section_key), list):
                data[section_key].append(content[2:].strip().strip("'\""))
        elif indent > 0 and ":" in content and not content.startswith("- "):
            # 段内嵌套键值对（如 server 下的 hostname/maxplayers/port）
            if section_key is not None:
                k, _, v = content.partition(":")
                k = k.strip()
                v = v.strip()
                if isinstance(data.get(section_key), list) and not data[section_key]:
                    data[section_key] = {}
                if isinstance(data.get(section_key), dict):
                    data[section_key][k] = v.strip("'\"")
    return data


def load_config(path):
    if not os.path.exists(path):
        sys.exit(f"配置不存在: {path}")
    # utf-8-sig 自动去除 BOM (PowerShell Set-Content -Encoding UTF8 会写 BOM)
    text = open(path, encoding="utf-8-sig").read()
    if path.lower().endswith(".json"):
        return json.loads(text)
    # 可选 PyYAML：更完整的 YAML 支持；未安装时回退内置极简解析器
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else parse_yaml_simple(text)
    except ImportError:
        return parse_yaml_simple(text)
    except Exception as e:
        print(f"  WARN: PyYAML 解析失败 ({e})，回退内置解析器")
        return parse_yaml_simple(text)


# ---------------------------------------------------------------------------
# registry（组件注册表）
# ---------------------------------------------------------------------------
def load_registry(name, registry_base=None):
    """读取组件注册表 JSON。registry_base 为远程 raw URL 时优先远程。"""
    if registry_base:
        _warn(f"使用远程注册表 --registry-base={registry_base}：该源完全可信，"
              "请确保其由你信任的仓库维护（SHA256 校验依赖同源 .sha256，注册表被篡改则失效）")
        url = gh_proxy_url(registry_base.rstrip("/") + "/" + name)
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"  WARN: 远程注册表加载失败 {url}: {e}")
    local = os.path.join(REGISTRY_DIR, name)
    if os.path.exists(local):
        return json.load(open(local, encoding="utf-8"))
    sys.exit(f"找不到注册表: {name}")


def parse_csv(value):
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return list(value or [])


def resolve_maps(cfg):
    maps = parse_csv(cfg.get("maps") or ["de_dust2"])
    if "de_dust2" not in maps:
        maps.insert(0, "de_dust2")  # 核心地图必含
    return maps


# ---------------------------------------------------------------------------
# filelist 组合器
# ---------------------------------------------------------------------------
def build_combined_filelist(core_filelist, cfg, registry_base=None):
    """核心 filelist + 地图组件片段 + 功能组件片段 => 合并行列表。"""
    # utf-8-sig：去除核心 filelist 可能带有的 UTF-8 BOM，避免组合文件首行出现 \ufeff
    with open(core_filelist, encoding="utf-8-sig") as f:
        lines = [ln.rstrip("\n") for ln in f]

    # 核心行也纳入去重集合，避免地图/功能片段重复追加核心已含文件（如 de_dust2.vpk）
    added = set(lines)
    reg_maps = load_registry("maps.json", registry_base)
    for m in resolve_maps(cfg):
        meta = reg_maps.get("maps", {}).get(m)
        if not meta:
            sys.exit(f"错误: 未知地图组件 {m}，请检查 registry/maps.json（可用: {', '.join(reg_maps.get('maps', {}))}）")
        for path in meta.get("filelist", []):
            if path not in added:
                lines.append(path)
                added.add(path)
        print(f"  + 地图 {m}")

    reg_feat = load_registry("features.json", registry_base)
    for feat in parse_csv(cfg.get("features") or []):
        meta = reg_feat.get("features", {}).get(feat)
        if not meta:
            sys.exit(f"错误: 未知功能组件 {feat}，请检查 registry/features.json（可用: {', '.join(reg_feat.get('features', {}))}）")
        for path in meta.get("filelist", []):
            if path not in added:
                lines.append(path)
                added.add(path)
        print(f"  + 功能 {feat}")
    return lines


# ---------------------------------------------------------------------------
# 下载/解压工具
# ---------------------------------------------------------------------------
def gh_proxy_url(url):
    """若设置了 CS2_GH_PROXY（如 https://ghproxy.com），为 GitHub URL 加代理前缀。"""
    proxy = os.environ.get("CS2_GH_PROXY", "").strip().rstrip("/")
    if proxy and (url.startswith("https://github.com/")
                  or url.startswith("https://raw.githubusercontent.com/")):
        return f"{proxy}/{url}"
    return url


def _sha256_hex(path, chunk=1024 * 1024):
    """计算文件 SHA256（hex）。"""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _parse_sha256_file(text):
    """解析 .sha256 文件，返回首个 64 位 hex 校验值；失败返回 None。"""
    m = re.search(r"[0-9a-fA-F]{64}", text or "")
    return m.group(0).lower() if m else None


def _verify_sha256(path, expected_hex, what=""):
    """校验文件 SHA256；不匹配则删除并报错。"""
    if not expected_hex:
        return True
    actual = _sha256_hex(path)
    if actual.lower() == expected_hex.lower():
        return True
    try:
        os.remove(path)
    except OSError:
        pass
    sys.exit(f"安全校验失败: {what or os.path.basename(path)} SHA256 不匹配\n"
             f"  预期 {expected_hex}\n  实际 {actual}\n"
             f"  已删除 {path}。请勿使用 --no-verify，除非信任该下载源。")


def download_file(url, dest, desc="", expected_sha256=None, sha256_url=None):
    """下载文件到 dest，带进度显示 + 断点续传 + SHA256 校验。

    未完成部分保存在 dest.part；中断后再次运行会从断点继续（HTTP Range）。
    失败时保留 .part，不清理。
    若给 sha256_url（通常为 url + '.sha256'），下载完成后拉取该边车并校验。
    expected_sha256 为已知 hex 时直接校验。
    """
    url = gh_proxy_url(url)
    _info(f"下载 {desc or os.path.basename(dest)} ...")
    tmp = dest + ".part"
    resume = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    if resume:
        _info(f"  检测到未完成下载 ({resume/1024/1024:.1f} MB)，断点续传 ...")

    def open_conn():
        headers = {"Range": f"bytes={resume}-"} if resume else {}
        req = urllib.request.Request(url, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 416 and resume:  # Range 不可满足: 重下
                os.remove(tmp)
                nonlocal_resume_zero()
                return urllib.request.urlopen(urllib.request.Request(url), timeout=60)
            raise

    def nonlocal_resume_zero():
        nonlocal resume
        resume = 0

    try:
        with open_conn() as r, open(tmp, "ab" if resume and r.status == 206 else "wb") as f:
            if r.status == 200:
                resume = 0
            total = int(r.headers.get("Content-Length") or 0) + resume
            done = resume
            use_progress = _progress_ok()
            last_line = done
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if not total:
                    continue
                pct = done * 100 // max(total, 1)
                if use_progress:
                    print(f"\r  {pct}% ({done/1024/1024:.0f}/{total/1024/1024:.0f} MB)", end="", flush=True)
                elif not QUIET and done - last_line >= 64 * 1024 * 1024:
                    # 非 TTY / --no-progress: 每 64MB 一行，避免 \r 刷屏
                    print(f"  {pct}% ({done/1024/1024:.0f}/{total/1024/1024:.0f} MB)", flush=True)
                    last_line = done
        if not use_progress and not QUIET:
            print(f"  完成 ({done/1024/1024:.0f}/{max(total, 1)/1024/1024:.0f} MB)")
        elif use_progress:
            print()
        os.replace(tmp, dest)
    except Exception as e:
        # 保留 .part 供断点续传
        sys.exit(f"下载失败 {url}: {e} (已保留 {tmp}，下次运行可断点续传)")

    # ---- SHA256 校验（供应链完整性） ----
    if getattr(args_ref(), "no_verify", False):
        return
    expected = expected_sha256
    if not expected and sha256_url:
        expected = _fetch_sha256(sha256_url)
        if expected is None:
            _warn(f"未找到校验文件 {sha256_url}，跳过 SHA256 校验（建议更新 Release 以包含 .sha256）")
            return
    if expected:
        _verify_sha256(dest, expected, desc or os.path.basename(dest))


def _fetch_sha256(url):
    """拉取 .sha256 边车，返回 hex；失败返回 None。"""
    try:
        with urllib.request.urlopen(gh_proxy_url(url), timeout=20) as r:
            return _parse_sha256_file(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def args_ref():
    """返回当前 argparse 命名空间（惰性，供 download_file 读取 --no-verify）。"""
    global _ARGS
    return _ARGS


def _download_verified(url, dest, desc=""):
    """下载 Release 产物并从同源 .sha256 边车校验（供应链完整性）。

    若 .sha256 缺失（旧版 Release），下载仍继续但发出警告（--no-verify 可强制跳过）。
    """
    download_file(url, dest, desc, sha256_url=url + ".sha256")


def _safe_join(dest, name):
    """防 zip-slip / path traversal：确保解压目标在 dest 内。"""
    dest_real = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(dest_real, name))
    if target != dest_real and not target.startswith(dest_real + os.sep):
        raise ValueError(f"非法解压路径: {name}")
    return target


def extract_archive(path, dest):
    """解压 zip / tar.gz 到 dest（带 zip-slip 防护，并保留 zip 记录的 Unix 权限位）。"""
    os.makedirs(dest, exist_ok=True)
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                _safe_join(dest, info.filename)
            z.extractall(dest)
            # Python zipfile 默认不保留 Unix 权限位，这里手动应用（如脚本执行位）
            for info in z.infolist():
                mode = (info.external_attr >> 16) & 0xFFFF
                if not mode:
                    continue
                target = os.path.join(dest, info.filename)
                if os.path.exists(target):
                    try:
                        os.chmod(target, mode)
                    except OSError:
                        pass
    else:
        with tarfile.open(path, "r:gz") as t:
            for m in t.getmembers():
                _safe_join(dest, m.name)
            t.extractall(dest)


def _make_zip(tree, out):
    """用 Python zipfile 生成标准 ZIP（Linux 无 bsdtar zip 支持时的可靠方案）。"""
    print(f"打包 {os.path.basename(tree)} -> {os.path.basename(out)} ...")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for root, _, files in os.walk(tree):
            for fn in files:
                fp = os.path.join(root, fn)
                arc = os.path.relpath(fp, tree).replace(os.sep, "/")
                z.write(fp, arc)


# DepotDownloader 下载源（与一键脚本保持一致）
DD_URLS = {
    "linux": "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_3.4.0/DepotDownloader-linux-x64.zip",
    "win64": "https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_3.4.0/DepotDownloader-windows-x64.zip",
}
# DepotDownloader 3.4.0 官方 Release 产物的 SHA256（钉死，供应链完整性；SteamRE 升级需同步更新）
DD_SHA256 = {
    "linux": "a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53",
    "win64": "41c9e9f0df54b3ad02e67a11726756e5c73283bd7c2e1b04acfa5ae4c2ed3767",
}
DD_NAME = {"linux": "DepotDownloader", "win64": "DepotDownloader.exe"}


def ensure_depot_tool(workdir, platform, allow_download=True):
    """返回 DepotDownloader 可执行文件路径；缺失时自动下载（allow_download=False 时不下载）。

    优先返回 workdir/tools/depotdownloader/<DD_NAME>；找不到则返回 None。
    """
    tool_dir = os.path.join(workdir, "tools", "depotdownloader")
    exe = os.path.join(tool_dir, DD_NAME[platform])
    if os.path.exists(exe):
        return exe
    if not allow_download:
        return None
    _info(f"未提供 depot_tool，自动下载 DepotDownloader ({platform}) ...")
    os.makedirs(tool_dir, exist_ok=True)
    zip_path = os.path.join(tool_dir, "dd.zip")
    download_file(DD_URLS[platform], zip_path, "DepotDownloader",
                  expected_sha256=DD_SHA256.get(platform))
    extract_archive(zip_path, tool_dir)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    if os.path.exists(exe):
        os.chmod(exe, 0o755)
        print(f"  DepotDownloader 就绪: {exe}")
        return exe
    sys.exit(f"自动下载后仍未找到 {exe}，请手动设置 depot_tool 或检查网络")


STEAM_MANIFEST_URL = "https://client-update.akamai.steamstatic.com/steam_client_ubuntu12"


def _is_valid_elf64(path, min_size=0):
    """检查文件是否为有效的 64 位 ELF（用于验证 steamclient.so）。"""
    try:
        if os.path.getsize(path) < min_size:
            return False
        with open(path, "rb") as f:
            magic = f.read(4)
            cls = f.read(1)
        return magic == b"\x7fELF" and cls == b"\x02"
    except Exception:
        return False


def ensure_steamclient(tree, workdir):
    """确保精简树根目录有有效的 64 位 steamclient.so。

    若缺失/损坏，从 Steam 客户端更新清单（bins_sdk_ubuntu12.zip）重新获取。
    供 prebuilt 模式使用（source 模式由 get-cs2slim 脚本负责下载）。
    """
    dst = os.path.join(tree, "steamclient.so")
    if _is_valid_elf64(dst, 40_000_000):
        print("  steamclient.so 已存在且有效 (ELF 64-bit)")
        return

    print("  steamclient.so 缺失或无效，从 Steam SDK 包重新获取 ...")
    manifest_path = os.path.join(workdir, "steam_client_ubuntu12")
    download_file(STEAM_MANIFEST_URL, manifest_path, "Steam manifest")
    with open(manifest_path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    m = re.search(r'"bins_sdk_ubuntu12\.zip\.([0-9a-f]+)"', content)
    if not m:
        sys.exit("无法在 Steam manifest 中找到 bins_sdk_ubuntu12")
    sdk_zip = os.path.join(workdir, "bins_sdk_ubuntu12.zip")
    download_file(f"https://steamcdn-a.akamaihd.net/client/bins_sdk_ubuntu12.zip.{m.group(1)}",
                  sdk_zip, "bins_sdk_ubuntu12")
    with zipfile.ZipFile(sdk_zip) as z:
        z.extract("linux64/steamclient.so", workdir)
    os.replace(os.path.join(workdir, "linux64", "steamclient.so"), dst)
    for p in (sdk_zip, manifest_path):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(os.path.join(workdir, "linux64"), ignore_errors=True)
    print("  steamclient.so 已更新")


CORE_PREBUILT = {
    "linux": {
        "name": "cs2-slim.tar.gz",
        "url": "https://github.com/cyqmq/cs2-slim-replica/releases/latest/download/cs2-slim.tar.gz",
        "tree": "slim",
    },
    "win64": {
        "name": "cs2-slim-win.zip",
        "url": "https://github.com/cyqmq/cs2-slim-replica/releases/latest/download/cs2-slim-win.zip",
        "tree": "slim-win",
    },
}


def _harden_perm(tree):
    """收紧精简树权限：树根 700，敏感文件（内嵌凭据/令牌）600。

    start_server.sh 内嵌面板账号密码、web_token.txt 存认证令牌，默认目录 755 会让同机
    其他用户可读。收紧后仅属主可访问。
    """
    try:
        os.chmod(tree, 0o700)
    except OSError:
        pass
    for root, _, files in os.walk(tree):
        for fn in files:
            low = fn.lower()
            if (fn in ("start_server.sh", "start_server.bat", "web_token.txt")
                    or low.endswith(".token") or "token" in low):
                p = os.path.join(root, fn)
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass


def _update_start_scripts(platform, tree, registry_base=None):
    """用主仓库最新的启动脚本覆盖精简树内的启动脚本（含网络优化参数）。

    从 raw.githubusercontent main 拉取，但按 core.json 中钉死的 SHA256 校验，
    防止脚本被篡改后直接执行（RCE 向量）。
    """
    try:
        if platform == "linux":
            url = "https://raw.githubusercontent.com/cyqmq/cs2-slim-replica/main/core/deploy/linux/start_server.sh"
            dst = os.path.join(tree, "start_server.sh")
        else:
            url = "https://raw.githubusercontent.com/cyqmq/cs2-slim-replica/main/core/deploy/windows/start_server.bat"
            dst = os.path.join(tree, "start_server.bat")
        reg_core = load_registry("core.json", registry_base)
        expected = ((reg_core or {}).get("start_scripts", {}).get(platform) or {}).get("sha256")
        tmp = dst + ".new"
        download_file(url, tmp, "启动脚本", expected_sha256=expected)
        os.replace(tmp, dst)
        if os.path.exists(dst):
            os.chmod(dst, 0o700)
        print("  已更新启动脚本（含 VPN/TUN 网络优化参数）")
    except SystemExit:
        raise
    except Exception as e:
        print(f"  WARN: 更新启动脚本失败: {e}")


def _patch_gameinfo_for_addon(tree, addon, label="Addon"):
    """在 game/csgo/gameinfo.gi 的 SearchPaths 中加入 Game <addon>。

    用于需要引擎优先加载自定义 GameDLL 的独立框架：
      - Metamod:Source  -> csgo/addons/metamod
      - SwiftlyS2        -> csgo/addons/swiftlys2
      - ModSharp        -> sharp
    """
    gi = os.path.join(tree, "game", "csgo", "gameinfo.gi")
    if not os.path.exists(gi):
        print(f"  WARN: 未找到 {gi}，无法自动打 {label} 补丁")
        return

    def _line_has_addon(ln):
        stripped = ln.lstrip()
        if not stripped.startswith("Game"):
            return False
        rest = stripped[4:].lstrip().split("//")[0].strip()
        return rest == addon

    with open(gi, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if any(_line_has_addon(ln) for ln in lines):
        print(f"  {label}: gameinfo.gi 已包含 {addon}，跳过补丁")
        return
    new_lines = []
    inserted = False
    for ln in lines:
        new_lines.append(ln)
        stripped = ln.lstrip()
        if stripped.startswith("Game_LowViolence"):
            indent = ln[: len(ln) - len(stripped)]
            new_lines.append(f"{indent}Game\t{addon}\n")
            inserted = True
    if not inserted:
        print(f"  WARN: gameinfo.gi 中未找到 Game_LowViolence 行，请手动添加 Game {addon}")
        return
    with open(gi, "w", encoding="utf-8", newline="") as f:
        f.writelines(new_lines)
    print(f"  {label}: 已在 gameinfo.gi 中添加 Game {addon}")


def _expand_feature_deps(features, reg_feat):
    """递归展开功能依赖（如 css -> metamod,metamod），依赖前置。"""
    expanded = []
    seen = set()

    def add(f):
        if f in seen:
            return
        seen.add(f)
        meta = reg_feat.get("features", {}).get(f)
        for dep in (meta or {}).get("requires") or []:
            add(dep)
        expanded.append(f)

    for f in features:
        add(f)
    return expanded


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------
def detect_platform():
    """按当前操作系统自动探测默认目标平台: win64 (Windows) 或 linux。"""
    return "win64" if os.name == "nt" else "linux"


def _platform_str(p):
    return f"{p} ({'Windows' if p == 'win64' else 'Linux'})"


def cmd_init(args, cfg):
    # 平台优先级: --platform 命令行 > 检测到的当前 OS；避免 Linux 用户误拿 win64 模板
    platform = args.platform or detect_platform()
    src = os.path.join(CLI_DIR, "slim.yaml.example")
    dst = args.output
    if not dst.endswith((".yaml", ".yml", ".json")):
        dst = os.path.join(dst, "slim.yaml")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if dst.lower().endswith(".json"):
        # 输出真正的 JSON 模板（load_config 对 .json 走 json.loads）
        template = {
            "platform": platform,
            "maps": ["de_dust2"],
            "features": [],
            "workdir": "./cs2-build",
            "server": {
                "hostname": "SlimTest",
                "maxplayers": 12,
                "port": 27015,
                "insecure": True,
                "sv_pure": 0,
                "sv_cheats": 1,
            },
        }
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"配置模板已生成: {dst} (platform: {_platform_str(platform)})")
    else:
        shutil.copy2(src, dst)
        # YAML 模板默认 win64，按探测结果重写 platform 行
        if platform != "win64":
            with open(dst, encoding="utf-8") as f:
                text = f.read()
            text = text.replace("platform: win64", f"platform: {platform}", 1)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(text)
        print(f"配置模板已生成: {dst} (platform: {_platform_str(platform)})")
    print("编辑后运行: python cs2slim.py download --config <path>")


def cmd_download(args, cfg):
    platform = cfg.get("platform") or args.platform or detect_platform()
    if platform not in PLATFORM_DEPOT:
        sys.exit(f"不支持的平台: {platform} (可选 linux/win64)")
    binary_depot, binary_filelist_name = PLATFORM_DEPOT[platform]

    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    os.makedirs(workdir, exist_ok=True)

    core_shared = os.path.join(CORE_DIR, "filelists", "filelist_2347770.txt")
    core_binary = os.path.join(CORE_DIR, "filelists", binary_filelist_name)
    if not (os.path.exists(core_shared) and os.path.exists(core_binary)):
        sys.exit(f"核心 filelist 缺失: {core_shared} 或 {core_binary}")

    combined = build_combined_filelist(core_shared, cfg, cfg.get("registry_base"))
    combined_path = os.path.join(workdir, "filelist_2347770_combined.txt")
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write("\n".join(combined) + "\n")
    print(f"组合 filelist: {combined_path} ({len(combined)} 行)")

    depot_tool = args.depot_tool or cfg.get("depot_tool")
    if depot_tool and not os.path.exists(depot_tool):
        sys.exit(f"depot_tool 不存在: {depot_tool}")
    if not depot_tool:
        # 缺省自动下载 DepotDownloader（--no-dl-tool 可关）
        depot_tool = ensure_depot_tool(workdir, platform, not getattr(args, "no_dl_tool", False))
    jobs = [
        ("2347770", combined_path),
        (binary_depot, core_binary),
    ]
    if depot_tool:
        for depot, filelist in jobs:
            cmd = [depot_tool, "-app", "730", "-depot", depot,
                   "-dir", os.path.join(workdir, "depot", depot),
                   "-filelist", filelist]
            print(">>", " ".join(cmd))
            r = subprocess.run(cmd, cwd=workdir)
            if r.returncode != 0:
                sys.exit(f"DepotDownloader 失败 (exit {r.returncode})")
        print("下载完成。下一步: python cs2slim.py extract --config <path>")
    else:
        tool_name = "DepotDownloader" if platform == "linux" else "DepotDownloader.exe"
        print("未提供 depot_tool，请手动执行以下命令（或编辑 slim.yaml 设置 depot_tool）:")
        for depot, filelist in jobs:
            cmd = [tool_name, "-app", "730", "-depot", depot,
                   "-dir", os.path.join(workdir, "depot", depot),
                   "-filelist", filelist]
            print("  " + " ".join(cmd))


def cmd_extract(args, cfg):
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    depot = os.path.join(workdir, "depot", "2347770")
    for sub, out in [("game/csgo", os.path.join(workdir, "loose", "game", "csgo")),
                     ("game/core", os.path.join(workdir, "loose", "game", "core"))]:
        dirvpk = os.path.join(depot, sub, "pak01_dir.vpk")
        if not os.path.exists(dirvpk):
            sys.exit(f"缺少 {dirvpk}，请先 download")
        print(f"提取 {sub} ...")
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS_DIR, "extract_vpk.py"),
                           "--dir-vpk", dirvpk,
                           "--archives-dir", os.path.dirname(dirvpk),
                           "--output", out])
        if r.returncode != 0:
            sys.exit(f"提取失败: {sub}")
    print("提取完成。下一步: python cs2slim.py build --config <path>")


def cmd_build(args, cfg):
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    platform = cfg.get("platform") or args.platform or detect_platform()
    maps = resolve_maps(cfg)
    extra = [m for m in maps if m != "de_dust2"]

    # 前置检查：depot 缺失时给出友好提示，而不是 Python traceback
    depot_shared = os.path.join(workdir, "depot", "2347770")
    depot_bin = os.path.join(workdir, "depot", PLATFORM_DEPOT[platform][0])
    missing = [d for d in (depot_shared, depot_bin) if not os.path.isdir(d)]
    if missing:
        sys.exit(
            "缺少 depot 目录:\n  " + "\n  ".join(missing)
            + "\n请先运行: python cs2slim.py download --config <path>"
        )

    print(f"组装平台 {platform}，地图: {maps}，额外: {extra or '无'}")
    rebuild_script = os.path.join(SCRIPTS_DIR, "rebuild_slim.py")
    if not os.path.exists(rebuild_script):
        sys.exit(
            f"缺少组装脚本: {rebuild_script}\n"
            "请确认 scripts/ 目录完整（主仓库可能被裁剪或损坏）。"
        )
    r = subprocess.run([sys.executable, rebuild_script,
                     "--base-dir", workdir,
                     "--maps", ",".join(extra),
                     "--platforms", platform])
    if r.returncode != 0:
        sys.exit("组装失败")
    # source 模式生成的启动脚本是旧版，用主仓库最新脚本覆盖（含菜单/端口/link-manager 支持）
    tree = os.path.join(workdir, "slim-win" if platform == "win64" else "slim")
    _update_start_scripts(platform, tree)
    print("组装完成。下一步: python cs2slim.py package --config <path> 或 run 启动")


def cmd_package(args, cfg):
    platform = cfg.get("platform") or args.platform or detect_platform()
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    tree = os.path.join(workdir, "slim-win" if platform == "win64" else "slim")
    if not os.path.exists(tree):
        sys.exit(f"精简树不存在: {tree}，请先 build")
    # 完整性检查：核心二进制存在才允许打包（避免打包空/残缺树）
    bin_rel = BIN_DIR[platform]
    if not os.path.exists(os.path.join(tree, bin_rel)):
        sys.exit(f"精简树不完整（缺少 {bin_rel}），请重新 build")

    fmt = args.format
    ext = "zip" if fmt == "zip" else "tar.gz"
    out = os.path.join(workdir, f"cs2-slim-{platform}.{ext}")
    if os.path.exists(out):
        if not getattr(args, "force", False):
            sys.exit(
                f"输出已存在: {out}\n"
                "使用 --force 覆盖，或先清理后再打包。"
            )
        print(f"覆盖已有包: {out}")
        os.remove(out)
    if fmt == "zip":
        # 用 Python zipfile 生成标准 ZIP（Linux 的 GNU tar -a 对 .zip 会产出无效文件）
        _make_zip(tree, out)
        print(f"打包完成: {out} ({os.path.getsize(out)/1024/1024/1024:.2f} GB)")
        return
    # Linux tar.gz 需保留 shell 脚本可执行位。
    # Windows 自带 bsdtar 会把 .sh 记成 666（丢失执行位），部署后 ./setup.sh 会 Permission denied；
    # 因此优先用 Git Bash GNU tar（记录 MSYS chmod 的 755），Linux 上直接使用系统 GNU tar。
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    if os.path.exists(git_bash):
        # Git Bash 的 tar 会把 `C:/...` 当作远程主机，必须用相对路径（subprocess 已设 cwd=workdir）
        cmd = [git_bash, "-c", f"tar -czf '{os.path.basename(out)}' -C '{os.path.basename(tree)}' ."]
    else:
        cmd = ["tar", "-czf", out, "-C", tree, "."]
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=workdir)
    if r.returncode != 0:
        sys.exit("打包失败")
    print(f"打包完成: {out} ({os.path.getsize(out)/1024/1024/1024:.2f} GB)")


def _port_udp_in_use(port):
    """检查 UDP 端口是否已被占用（CS2 服务端用 UDP 监听游戏端口）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def cmd_run(args, cfg):
    platform = cfg.get("platform") or args.platform or detect_platform()
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    # build 实际输出在 <workdir>/slim 或 <workdir>/slim-win 下
    tree = os.path.join(workdir, "slim-win" if platform == "win64" else "slim")
    exe = os.path.join(tree, BIN_DIR[platform])
    if not os.path.exists(exe):
        sys.exit(f"可执行文件不存在: {exe}，请先 build")
    srv = cfg.get("server") or {}
    # 端口优先级与启动脚本一致: SERVER_PORT > CS2_PORT > 配置 > 27015
    port = (os.environ.get("SERVER_PORT")
            or os.environ.get("CS2_PORT")
            or srv.get("port")
            or 27015)
    # 默认地图：--map 未显式指定时取配置 maps[0]（兼容 de_dust2 缺省）
    map_name = args.map
    if not map_name:
        maps_cfg = parse_csv(cfg.get("maps")) or ["de_dust2"]
        map_name = maps_cfg[0]
    # 端口占用前置告警
    if _port_udp_in_use(port):
        print(f">> WARN: UDP 端口 {port} 已被占用，服务端可能无法绑定")
    # ---------- 委托启动脚本（统一端口/网络/密码/地图逻辑） ----------
    start_sh = os.path.join(tree, "start_server.sh")
    if os.path.exists(start_sh):
        os.environ["CS2_MAP"] = str(map_name)
        if srv.get("hostname"):
            os.environ["CS2_HOSTNAME"] = str(srv.get("hostname"))
        if srv.get("maxplayers"):
            os.environ["CS2_MAXPLAYERS"] = str(srv.get("maxplayers"))
        print(f">> 委托启动脚本: bash {start_sh} auto")
        os.execv("/bin/bash", ["bash", start_sh, "auto"])
    # ---------- 回退：无启动脚本时内置参数构造 ----------
    params = [
        exe, "-dedicated", "+map", map_name,
        "+hostname", srv.get("hostname") or "SlimTest",
        "-maxplayers", str(srv.get("maxplayers") or 12),
        "-port", str(port),
        "-insecure", "-condebug", "+game_type", "0", "+game_mode", "0",
        "+sv_pure", str(srv.get("sv_pure") or 0),
        "+sv_cheats", str(srv.get("sv_cheats") or 1),
        "+sv_maxrate", "0", "+sv_minrate", "100000",
        "+sv_maxupdaterate", "128", "+sv_maxcmdrate", "128",
        "+net_maxroutable", "1200",
    ]
    # ---------- 网络模式 ----------
    # CS2_NET_MODE: 1(局域网) | 2(互联网, 默认) | 3(公开, 需 GSLT)
    #  1 = +sv_lan 1（不绑 -ip）；2 = +sv_lan 0 -ip 0.0.0.0（外网直连，不进公网列表）；
    #  3 = +sv_lan 0 + GSLT(+sv_setsteamaccount)，无 GSLT 回退档2。
    #      警告: 本精简服务端非认证/可能违规，公开绑定 GSLT 可能导致该账号被 GSLT 服务封禁。
    # GSLT 来源优先: CS2_GSLT > GSLT > CS2LM_GSLT
    net_mode = os.environ.get("CS2_NET_MODE", "2")
    if net_mode == "1":
        sv_lan, bind_ip, net_args = 1, [], []
    elif net_mode == "3":
        gslt = (os.environ.get("CS2_GSLT")
                or os.environ.get("GSLT")
                or os.environ.get("CS2LM_GSLT"))
        if gslt:
            sv_lan, bind_ip, net_args = 0, [], ["+sv_setsteamaccount", gslt]
        else:
            print(">> WARN: CS2_NET_MODE=3 无 GSLT（CS2_GSLT/GSLT/CS2LM_GSLT），回退档2")
            sv_lan, bind_ip, net_args = 0, ["-ip", "0.0.0.0"], []
    else:
        if net_mode != "2":
            print(f">> WARN: 未知 CS2_NET_MODE='{net_mode}'，回退档2")
        sv_lan, bind_ip, net_args = 0, ["-ip", "0.0.0.0"], []
    # ---------- 服务器密码 ----------
    # CS2_PASSWORD: 服务器进入密码，默认空 = 无密码。非空时追加 +sv_password。
    password = os.environ.get("CS2_PASSWORD")
    pass_args = []
    if password:
        pass_args = ["+sv_password", password]

    # -ip 插到 -port 前；+sv_lan 插到 +sv_maxrate 前；net_args/pass_args 追加末尾
    if bind_ip:
        pi = params.index("-port")
        params[pi:pi] = bind_ip
    params[params.index("+sv_maxrate") : params.index("+sv_maxrate")] = ["+sv_lan", str(sv_lan)]
    params += net_args
    params += pass_args
    print(">>", " ".join(params))
    print(f"服务端启动中 (工作目录 {tree})。日志: game/csgo/console.log")
    if platform == "win64":
        subprocess.run(params, cwd=tree)
    else:
        os.chdir(tree)
        os.execv(exe, params)


# ---------------------------------------------------------------------------
# 一键全流程: download + extract + build (+package)
# ---------------------------------------------------------------------------
def cmd_all(args, cfg):
    # depot_tool 可缺省：cmd_download 会自动下载 DepotDownloader（--no-dl-tool 除外）
    print("=" * 60)
    print(">>> [1/4] 下载 depot（按配置组合 filelist）")
    cmd_download(args, cfg)
    print("=" * 60)
    print(">>> [2/4] 提取 loose files")
    cmd_extract(args, cfg)
    print("=" * 60)
    print(">>> [3/4] 组装精简树")
    cmd_build(args, cfg)
    if getattr(args, "package", False):
        print("=" * 60)
        print(">>> [4/4] 打包")
        cmd_package(args, cfg)
    else:
        print("=" * 60)
        platform = cfg.get("platform") or args.platform or detect_platform()
        print("一键完成! 精简树已就绪:")
        if platform == "linux":
            print("  启动: bash <workdir>/slim/start_server.sh")
        else:
            print("  启动: <workdir>/slim-win/start_server.bat")
        print("  打包: python cs2slim.py package --config <path> --format zip")


# ---------------------------------------------------------------------------
# 预构建模式: 直接拉取 GitHub Release 包并自动拼装
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 版本自检 / 升级（附属仓库预构建版本对比）
# ---------------------------------------------------------------------------
def _semver_tuple(v):
    """'v1.2.3' / '1.2.3-beta' -> (1,2,3)；非法 -> (0,0,0)。"""
    s = str(v or "").lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _ver_cmp(a, b):
    ta, tb = _semver_tuple(a), _semver_tuple(b)
    return (ta > tb) - (ta < tb)


# ---------------------------------------------------------------------------
# CLI 自身版本自检 / 自升级（PyPI + GitHub Release）
# ---------------------------------------------------------------------------
def _pypi_latest(pkg="cs2slim"):
    """查询 PyPI 最新版本；网络/不可用返回 None。"""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=8) as r:
            return json.loads(r.read().decode("utf-8")).get("info", {}).get("version")
    except Exception:
        return None


def _gh_latest_tag(repo="cyqmq/cs2slim-cli"):
    """查询 GitHub releases/latest 的 tag；网络/不可用返回 None。"""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            headers={"User-Agent": "cs2slim"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8")).get("tag_name")
    except Exception:
        return None


def _cli_latest():
    """CLI 最新版本：优先 PyPI，回退 GitHub release tag。"""
    v = _pypi_latest()
    if v:
        return v
    t = _gh_latest_tag()
    return t.lstrip("vV") if t else None


def _install_mode():
    """返回安装方式：zipapp / pip / source。"""
    if _zipapp_zip_path():
        return "zipapp"
    try:
        import importlib.metadata as _md
        _md.version("cs2slim")
        return "pip"
    except Exception:
        return "source"


def cmd_selfupdate(args, cfg):
    cur = __version__
    latest = _cli_latest()
    print(f"当前 CLI 版本: {cur}")
    print(f"最新版本    : {latest or '未知'}")
    if not latest:
        print("无法获取最新版本（网络或 PyPI/GitHub 不可用）")
        return
    if _ver_cmp(latest, cur) <= 0:
        print("已是最新版本（无需升级）")
        return
    if not getattr(args, "yes", False):
        ans = input(f"升级 CLI 到 {latest}？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return
    mode = _install_mode()
    print(f"安装方式: {mode}")
    if mode == "pip":
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "cs2slim"],
            cwd=os.getcwd(),
        )
        if r.returncode == 0:
            print("升级完成（新版本下次运行生效）")
        else:
            sys.exit(f"pip 升级失败 (exit {r.returncode})，可手动执行: pip install -U cs2slim")
    elif mode == "zipapp":
        zip_path = _zipapp_zip_path()
        if not zip_path:
            sys.exit("无法定位当前 cs2slim.pyz")
        tmp = zip_path + ".new"
        pyz_url = "https://github.com/cyqmq/cs2slim-cli/releases/latest/download/cs2slim.pyz"
        download_file(pyz_url, tmp, "下载新版 cs2slim.pyz", sha256_url=pyz_url + ".sha256")
        os.chmod(tmp, 0o755)
        os.replace(tmp, zip_path)
        print(f"已替换: {zip_path}")
        print("新版本下次运行生效")
    else:
        print("检测到源码模式，请手动更新仓库并重装，例如:")
        print("  git -C <仓库根> pull")
        print("  pip install -U cs2slim")


def _version_file(tree):
    return os.path.join(tree, ".cs2slim-version.json")


def _read_version(tree):
    p = _version_file(tree)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_version(tree, reg_core, reg_maps, reg_feat, maps, features):
    """安装/升级后写入版本文件，供 status/upgrade 对比。"""
    import datetime
    data = {
        "core": (reg_core or {}).get("version", ""),
        "maps": {m: (reg_maps.get("maps", {}).get(m, {}).get("release_version", "")
                     or (reg_maps or {}).get("release_version", "")) for m in maps if m != "de_dust2"},
        "features": {f: (reg_feat.get("features", {}).get(f, {}).get("release_version", "")
                         or (reg_feat or {}).get("release_version", "")) for f in features},
        "checked": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(_version_file(tree), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError as e:
        print(f"  WARN: 版本文件写入失败: {e}")


def cmd_upgrade(args, cfg):
    platform = cfg.get("platform") or detect_platform()
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    tree = os.path.join(workdir, "slim-win" if platform == "win64" else "slim")
    if not os.path.exists(tree):
        sys.exit(f"精简树不存在: {tree}，请先 prebuilt")
    installed = _read_version(tree)
    reg_core = load_registry("core.json", cfg.get("registry_base"))
    reg_maps = load_registry("maps.json", cfg.get("registry_base"))
    reg_feat = load_registry("features.json", cfg.get("registry_base"))
    core_latest = (reg_core or {}).get("version", "")
    maps_latest = (reg_maps or {}).get("release_version", "")
    feat_latest = (reg_feat or {}).get("release_version", "")

    def _upd(label, cur, latest):
        return cur and latest and _ver_cmp(latest, cur) > 0

    updates = []
    if _upd("core", installed.get("core"), core_latest):
        updates.append(("core", installed.get("core"), core_latest))
    for m, v in (installed.get("maps") or {}).items():
        if _upd(f"地图 {m}", v, maps_latest):
            updates.append((f"地图 {m}", v, maps_latest))
    for f, v in (installed.get("features") or {}).items():
        if _upd(f"功能 {f}", v, feat_latest):
            updates.append((f"功能 {f}", v, feat_latest))
    if not updates:
        print("已是最新版本（无需升级）")
        _cli_upgrade_hint()
        return
    print("发现可升级组件:")
    for label, cur, latest in updates:
        print(f"  - {label}: {cur} -> {latest}")
    if not getattr(args, "yes", False):
        ans = input("执行升级？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消")
            return
    # 核心升级：删除缓存核心包后重跑 prebuilt（自动重下+重拼装）
    core_needs = any(u[0] == "core" for u in updates)
    maps_needs = {u[0].split(" ", 1)[-1] for u in updates if u[0].startswith("地图 ")}
    feat_needs = {u[0].split(" ", 1)[-1] for u in updates if u[0].startswith("功能 ")}
    core_name = (reg_core or {}).get("assets", {}).get(platform, {}).get("name") or CORE_PREBUILT[platform]["name"]
    if core_needs:
        cached = os.path.join(workdir, core_name)
        if os.path.exists(cached):
            print(f"删除旧核心包缓存: {cached}")
            os.remove(cached)
        print("重跑 prebuilt（核心升级）...")
        cmd_prebuilt(args, cfg)
    else:
        # 仅地图/功能升级：刷新对应 zip 并重解压
        reg_m = load_registry("maps.json", cfg.get("registry_base"))
        for m in maps_needs:
            meta = reg_m.get("maps", {}).get(m) or {}
            url = meta.get("prebuilt_url")
            if not url:
                print(f"  WARN: 地图 {m} 无预构建包，跳过"); continue
            mzip = os.path.join(workdir, f"{m}.zip")
            if os.path.exists(mzip):
                os.remove(mzip)
            download_file(url, mzip, f"地图 {m} (升级)", sha256_url=url + ".sha256")
            extract_archive(mzip, tree)
            print(f"  已升级地图 {m}")
        reg_f = load_registry("features.json", cfg.get("registry_base"))
        for f in feat_needs:
            meta = reg_f.get("features", {}).get(f) or {}
            url = meta.get("prebuilt_url")
            if not url:
                print(f"  WARN: 功能 {f} 无预构建包，跳过"); continue
            fzip = os.path.join(workdir, f"{f}-pack.zip")
            if os.path.exists(fzip):
                os.remove(fzip)
            download_file(url, fzip, f"功能 {f} (升级)", sha256_url=url + ".sha256")
            extract_archive(fzip, tree)
            print(f"  已升级功能 {f}")
        # 功能后置补丁
        if any(f in ("metamod", "metamod-win") for f in feat_needs):
            _patch_gameinfo_for_addon(tree, "csgo/addons/metamod", "Metamod")
        if any(f in ("swiftly", "swiftly-win") for f in feat_needs):
            _patch_gameinfo_for_addon(tree, "csgo/addons/swiftlys2", "SwiftlyS2")
        if any(f in ("modsharp", "modsharp-win") for f in feat_needs):
            _patch_gameinfo_for_addon(tree, "sharp", "ModSharp")
        # 更新版本文件（仅地图/功能分支）
        _write_version(tree, reg_core, reg_maps, reg_feat,
                        resolve_maps(cfg), parse_csv(cfg.get("features") or []))
    print("升级完成")
    _cli_upgrade_hint()


def _cli_upgrade_hint():
    """打印 CLI 自身更新提示（若有新版本）。"""
    try:
        latest = _cli_latest()
    except Exception:
        return
    if latest and _ver_cmp(latest, __version__) > 0:
        print(f"提示: CLI 有新版本 {__version__} -> {latest}，可执行 cs2slim selfupdate")



def cmd_prebuilt(args, cfg):
    platform = cfg.get("platform") or args.platform or detect_platform()
    if platform not in CORE_PREBUILT:
        sys.exit(f"不支持的平台: {platform} (可选 linux/win64)")
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    os.makedirs(workdir, exist_ok=True)

    core = CORE_PREBUILT[platform]
    tree = os.path.join(workdir, core["tree"])

    if args.dry_run:
        print("[dry-run] 预构建模式计划:")
        print(f"  核心包: {core['url']}")
        print(f"  解压到: {tree}")
        reg_maps = load_registry("maps.json", cfg.get("registry_base"))
        for m in resolve_maps(cfg):
            if m == "de_dust2":
                continue
            meta = reg_maps.get("maps", {}).get(m)
            if meta is None:
                sys.exit(f"错误: 未知地图组件 {m}，请检查 registry/maps.json")
            url = meta.get("prebuilt_url") or ""
            if not url:
                print(f"  [不可用] 地图 {m}: 无预构建包（prebuilt 模式不可用，请改用 source 模式）")
            else:
                print(f"  地图 {m}: {url}")
        reg_feat = load_registry("features.json", cfg.get("registry_base"))
        features = _expand_feature_deps(parse_csv(cfg.get("features") or []), reg_feat)
        for f in features:
            meta = reg_feat.get("features", {}).get(f)
            if meta is None:
                sys.exit(f"错误: 未知功能组件 {f}，请检查 registry/features.json")
            url = meta.get("prebuilt_url") or ""
            if not url:
                print(f"  [不可用] 功能 {f}: 无预构建包（prebuilt 模式不可用）")
            else:
                print(f"  功能 {f}: {url}")
        return

    # 1. 核心包
    core_path = os.path.join(workdir, core["name"])
    if not os.path.exists(core_path):
        download_file(core["url"], core_path, f"核心精简包 {core['name']}", sha256_url=core["url"] + ".sha256")

    # 2. 解压核心包
    if os.path.exists(tree):
        print(f"WARN: 已有精简树 {tree}，将被覆盖。若你修改过其中的文件，请先备份（即将删除）。")
        shutil.rmtree(tree)
    os.makedirs(tree, exist_ok=True)
    print(f"解压核心包到 {tree} ...")
    extract_archive(core_path, tree)
    # 确保 steamclient.so 有效（缺失/损坏时自动从 Steam SDK 包补下）
    if platform == "linux":
        ensure_steamclient(tree, workdir)
    _update_start_scripts(platform, tree, cfg.get("registry_base"))
    # 修复执行权限（面板场景默认只给 start.sh 权限）
    if platform == "linux":
        cs2_bin = os.path.join(tree, "game", "bin", "linuxsteamrt64", "cs2")
        if os.path.exists(cs2_bin):
            os.chmod(cs2_bin, 0o755)
        start_sh = os.path.join(tree, "start_server.sh")
        if os.path.exists(start_sh):
            os.chmod(start_sh, 0o755)

    # 3. 地图组件
    maps = resolve_maps(cfg)
    reg_maps = load_registry("maps.json", cfg.get("registry_base"))
    for m in maps:
        if m == "de_dust2":
            continue
        meta = reg_maps.get("maps", {}).get(m)
        if meta is None:
            sys.exit(f"错误: 未知地图组件 {m}，请检查 registry/maps.json")
        url = meta.get("prebuilt_url") or ""
        if not url:
            sys.exit(
                f"错误: 地图 {m} 暂无预构建包，prebuilt 模式无法安装。\n"
                "当前 prebuilt 模式仅支持核心包内置的 de_dust2。\n"
                "如需其他地图，请改用 source 模式（CS2_MODE=source）重新执行。"
            )
        mzip = os.path.join(workdir, f"{m}.zip")
        if not os.path.exists(mzip):
            download_file(url, mzip, f"地图 {m}", sha256_url=url + ".sha256")
        print(f"  拼装地图 {m} ...")
        extract_archive(mzip, tree)

    # 4. 功能组件
    features = parse_csv(cfg.get("features") or [])
    reg_feat = load_registry("features.json", cfg.get("registry_base"))
    features = _expand_feature_deps(features, reg_feat)
    for f in features:
        meta = reg_feat.get("features", {}).get(f)
        if meta is None:
            sys.exit(f"错误: 未知功能组件 {f}，请检查 registry/features.json")
        url = meta.get("prebuilt_url") or ""
        if not url:
            sys.exit(f"错误: 功能 {f} 暂无预构建包，prebuilt 模式无法安装（请改用 source 模式）")
        fzip = os.path.join(workdir, f"{f}-pack.zip")
        if not os.path.exists(fzip):
            download_file(url, fzip, f"功能 {f}", sha256_url=url + ".sha256")
        print(f"  拼装功能 {f} ...")
        extract_archive(fzip, tree)
    # link-manager 启动器兜底：确保 cs2lm 可执行（旧 zip/部分解压工具会丢失执行位）
    if platform == "linux" and any(f in ("link-manager",) for f in features):
        lm_launcher = os.path.join(tree, "cs2lm")
        if os.path.exists(lm_launcher):
            os.chmod(lm_launcher, 0o755)
    # 功能后置补丁（独立框架需要修改 gameinfo.gi 加载自定义 GameDLL）
    if any(f in ("metamod", "metamod-win") for f in features):
        _patch_gameinfo_for_addon(tree, "csgo/addons/metamod", "Metamod")
    if any(f in ("swiftly", "swiftly-win") for f in features):
        _patch_gameinfo_for_addon(tree, "csgo/addons/swiftlys2", "SwiftlyS2")
    if any(f in ("modsharp", "modsharp-win") for f in features):
        _patch_gameinfo_for_addon(tree, "sharp", "ModSharp")

    # 收紧权限（明文凭据/令牌文件）
    _harden_perm(tree)

    # 5. 报告
    print()
    print("预构建模式完成!")
    if platform == "linux":
        print(f"  启动: bash {tree}/start_server.sh")
    else:
        print(f"  启动: {tree}/start_server.bat")
    print(f"  提示: 地图/功能包已缓存到 {workdir}，重复执行可复用。")

    # 6. 记录版本（供 status/upgrade 对比）
    try:
        reg_core = load_registry("core.json", cfg.get("registry_base"))
    except SystemExit:
        reg_core = None
    reg_maps = load_registry("maps.json", cfg.get("registry_base"))
    reg_feat = load_registry("features.json", cfg.get("registry_base"))
    _write_version(tree, reg_core, reg_maps, reg_feat, maps, features)


# ---------------------------------------------------------------------------
# 状态 / 清理
# ---------------------------------------------------------------------------
def _tree_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _server_pid():
    """返回正在运行的 CS2 服务端 pid（按 BIN_DIR 可执行文件名匹配），无则 None。"""
    import glob
    names = {os.path.basename(p) for p in BIN_DIR.values()}  # cs2 / cs2.exe
    for p in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(p, "rb") as f:
                data = f.read().replace(b"\x00", b" ").decode(errors="replace")
            if any(n in data for n in names) and ("-dedicated" in data):
                return p.split("/")[2]
        except Exception:
            continue
    return None


def cmd_status(args, cfg):
    platform = cfg.get("platform") or detect_platform()
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    tree = os.path.join(workdir, "slim-win" if platform == "win64" else "slim")
    print(f"platform : {_platform_str(platform)}")
    print(f"workdir  : {workdir}")
    print(f"精简树   : {tree}")
    bin_path = os.path.join(tree, BIN_DIR[platform])
    if os.path.exists(bin_path):
        print(f"核心二进制: 存在 ({_tree_size(tree)/1024/1024/1024:.2f} GB)")
    else:
        print("核心二进制: 缺失（请先 build / prebuilt）")
    maps = resolve_maps(cfg)
    installed = [m for m in maps if os.path.exists(os.path.join(tree, "game", "csgo", "maps", f"{m}.vpk"))]
    print(f"地图     : {', '.join(installed) if installed else '无（精简树缺失）'}")
    pid = _server_pid()
    print(f"服务端   : {'运行中 (pid ' + pid + ')' if pid else '未运行'}")
    # 缓存占用
    cache_files = []
    if os.path.isdir(workdir):
        for fn in os.listdir(workdir):
            if fn.endswith((".zip", ".tar.gz", ".part")):
                cache_files.append(os.path.join(workdir, fn))
    if cache_files:
        total = sum(os.path.getsize(f) for f in cache_files if os.path.exists(f))
        print(f"下载缓存 : {len(cache_files)} 个文件 ({total/1024/1024:.0f} MB)  → clean 可清理")

    # 版本自检（已装 vs 附属仓库最新）
    installed = _read_version(tree)
    try:
        reg_core = load_registry("core.json", cfg.get("registry_base"))
        reg_maps = load_registry("maps.json", cfg.get("registry_base"))
        reg_feat = load_registry("features.json", cfg.get("registry_base"))
    except SystemExit:
        reg_core = reg_maps = reg_feat = {}
    if installed or reg_core or reg_maps or reg_feat:
        print()
        print("版本:")
        core_latest = (reg_core or {}).get("version", "?")
        core_cur = installed.get("core", "未安装")
        core_flag = "↑" if core_cur != "未安装" and core_latest != "?" and _ver_cmp(core_latest, core_cur) > 0 else ""
        print(f"  core     : 已装 {core_cur} / 最新 {core_latest} {core_flag}")
        maps_latest = (reg_maps or {}).get("release_version", "?")
        for m, v in (installed.get("maps") or {}).items():
            flag = "↑" if v and maps_latest != "?" and _ver_cmp(maps_latest, v) > 0 else ""
            print(f"  地图 {m:<12}: 已装 {v} / 最新 {maps_latest} {flag}")
        feat_latest = (reg_feat or {}).get("release_version", "?")
        for f, v in (installed.get("features") or {}).items():
            flag = "↑" if v and feat_latest != "?" and _ver_cmp(feat_latest, v) > 0 else ""
            print(f"  功能 {f:<12}: 已装 {v} / 最新 {feat_latest} {flag}")
        print("  提示: 出现 ↑ 表示可执行 upgrade 升级")
        # CLI 自身版本自检
        cli_latest = _cli_latest()
        if cli_latest:
            cli_flag = "↑" if _ver_cmp(cli_latest, __version__) > 0 else ""
            print(f"  CLI      : 当前 {__version__} / 最新 {cli_latest} {cli_flag}")
            if cli_flag:
                print("  提示: CLI 有新版本，可执行 cs2slim selfupdate")


def cmd_clean(args, cfg):
    platform = cfg.get("platform") or detect_platform()
    workdir = os.path.abspath(cfg.get("workdir") or "./cs2-build")
    tree = os.path.join(workdir, "slim-win" if platform == "win64" else "slim")
    if not os.path.isdir(workdir):
        print(f"工作目录不存在: {workdir}")
        return
    removed = 0
    freed = 0
    targets = [os.path.join(workdir, f) for f in os.listdir(workdir)
               if f.endswith((".zip", ".tar.gz", ".part"))]
    if args.all:
        # 移除工具缓存（DepotDownloader / steamclient）
        for sub in ("tools", "repo"):
            p = os.path.join(workdir, sub)
            if os.path.isdir(p):
                targets.append(p)
    for t in targets:
        if os.path.isdir(t):
            freed += _tree_size(t)
            shutil.rmtree(t, ignore_errors=True)
            removed += 1
        elif os.path.exists(t):
            freed += os.path.getsize(t)
            os.remove(t)
            removed += 1
    if removed:
        print(f"已清理 {removed} 项，释放约 {freed/1024/1024/1024:.2f} GB")
        print(f"精简树 {tree} 未受影响（如需删除请手动执行）")
    else:
        print("没有可清理的下载缓存（zip/tar.gz/.part 或 tools/）")
    print("提示: 已下载的 zip/tar.gz 会保留以支持断点缓存；clean 不会删除精简树。")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def read_buildid():
    """读取 state/buildid.json 中的 CS2 buildid，用于版本信息。"""
    try:
        with open(os.path.join(CLI_DIR, "state", "buildid.json"), encoding="utf-8") as f:
            return json.load(f).get("buildid", "?")
    except Exception:
        return "?"


def main():
    # --version 在 subparsers required=True 之前拦截，避免无子命令时报错
    if "--version" in sys.argv:
        print(f"cs2slim {__version__}  CS2 buildid: {read_buildid()}")
        print(f"python: {sys.version.split()[0]}  platform: {detect_platform()}")
        return
    ap = argparse.ArgumentParser(description="cs2slim - CS2 精简服务端配置驱动 CLI")
    ap.add_argument("--version", action="store_true", help="显示版本信息并退出")
    ap.add_argument("--config", help="配置文件 (slim.yaml / slim.json)")
    ap.add_argument("--platform", choices=["linux", "win64"], help="目标平台")
    ap.add_argument("--maps", help="地图列表, 逗号分隔, 如 de_dust2,de_mirage")
    ap.add_argument("--features", help="功能列表, 逗号分隔, 如 bots")
    ap.add_argument("--depot-tool", help="DepotDownloader 可执行文件路径")
    ap.add_argument("--no-dl-tool", action="store_true", help="不自动下载 DepotDownloader（仅打印手动命令）")
    ap.add_argument("--quiet", action="store_true", help="静默输出（仅错误/结果）")
    ap.add_argument("--no-progress", action="store_true", help="禁用实时进度条（非 TTY 自动降级为逐行）")
    sub = ap.add_subparsers(dest="command", required=True)

    # 通用选项（同时挂在顶层和每个子命令，便于 `cs2slim.py download --config slim.yaml`）
    def add_common(p):
        p.add_argument("--config", help="配置文件 (slim.yaml / slim.json)")
        p.add_argument("--platform", choices=["linux", "win64"], help="目标平台")
        p.add_argument("--maps", help="地图列表, 逗号分隔, 如 de_dust2,de_mirage")
        p.add_argument("--features", help="功能列表, 逗号分隔, 如 bots")
        p.add_argument("--depot-tool", help="DepotDownloader 可执行文件路径")
        p.add_argument("--no-dl-tool", action="store_true", help="不自动下载 DepotDownloader（仅打印手动命令）")
        p.add_argument("--quiet", action="store_true", help="静默输出（仅错误/结果）")
        p.add_argument("--no-progress", action="store_true", help="禁用实时进度条（非 TTY 自动降级为逐行）")
        p.add_argument("--registry-base", help="远程组件目录 raw URL 前缀（优先于本地 registry/）")
        p.add_argument("--no-verify", action="store_true",
                       help="禁用 SHA256 供应链校验（不安全，仅在信任下载源时使用）")

    p_init = sub.add_parser("init", help="生成配置模板")
    p_init.add_argument("--output", default=".", help="输出目录或文件路径")
    add_common(p_init)
    p_init.set_defaults(func=cmd_init)

    p_dl = sub.add_parser("download", help="按配置下载 depot")
    add_common(p_dl)
    p_dl.set_defaults(func=cmd_download)

    p_ex = sub.add_parser("extract", help="提取 loose files")
    add_common(p_ex)
    p_ex.set_defaults(func=cmd_extract)

    p_bd = sub.add_parser("build", help="组装精简树")
    add_common(p_bd)
    p_bd.set_defaults(func=cmd_build)

    p_pk = sub.add_parser("package", help="打包")
    p_pk.add_argument("--format", choices=["zip", "tar.gz"], default="tar.gz")
    p_pk.add_argument("--force", action="store_true", help="覆盖已存在的输出包")
    add_common(p_pk)
    p_pk.set_defaults(func=cmd_package)

    p_run = sub.add_parser("run", help="启动服务端")
    p_run.add_argument("--map", default=None, help="启动地图（缺省取配置 maps[0]，默认 de_dust2）")
    add_common(p_run)
    p_run.set_defaults(func=cmd_run)

    p_all = sub.add_parser("all", help="一键全流程: download + extract + build (+package)")
    p_all.add_argument("--package", action="store_true", help="完成后打包")
    p_all.add_argument("--format", choices=["zip", "tar.gz"], default="tar.gz")
    p_all.add_argument("--force", action="store_true", help="覆盖已存在的输出包")
    add_common(p_all)
    p_all.set_defaults(func=cmd_all)

    p_pb = sub.add_parser("prebuilt", help="预构建模式: 直接拉取 Release 包自动拼装")
    p_pb.add_argument("--dry-run", action="store_true", help="只打印下载计划, 不下载")
    add_common(p_pb)
    p_pb.set_defaults(func=cmd_prebuilt)

    p_st = sub.add_parser("status", help="查看当前工作目录状态（树/地图/服务端/缓存）")
    add_common(p_st)
    p_st.set_defaults(func=cmd_status)

    p_cl = sub.add_parser("clean", help="清理下载缓存（zip/tar.gz/.part 与 tools）")
    p_cl.add_argument("--all", action="store_true", help="同时清理 tools/repo 工具缓存")
    add_common(p_cl)
    p_cl.set_defaults(func=cmd_clean)

    p_up = sub.add_parser("upgrade", help="检查附属仓库预构建版本并自动升级")
    p_up.add_argument("--yes", "-y", action="store_true", help="跳过确认自动升级")
    add_common(p_up)
    p_up.set_defaults(func=cmd_upgrade)

    p_su = sub.add_parser("selfupdate", help="检查并升级 CLI 自身（PyPI/GitHub）")
    p_su.add_argument("--yes", "-y", action="store_true", help="跳过确认自动升级")
    add_common(p_su)
    p_su.set_defaults(func=cmd_selfupdate)

    args = ap.parse_args()
    global _ARGS
    _ARGS = args

    # 全局输出控制：--quiet 抑制进度/信息；--no-progress 关闭 \r 实时进度
    if getattr(args, "quiet", False):
        global QUIET
        QUIET = True
    if getattr(args, "no_progress", False):
        global PROGRESS
        PROGRESS = False

    # 配置驱动：--config 优先，命令行参数覆盖/补充
    # 子命令上的 --config 覆盖顶层 --config（顶层在前、子命令在后）
    cfg = {}
    config_path = getattr(args, "config", None)
    if config_path:
        cfg = load_config(config_path)
    if getattr(args, "platform", None):
        cfg["platform"] = args.platform
    if getattr(args, "maps", None):
        cfg["maps"] = args.maps
    if getattr(args, "features", None):
        cfg["features"] = args.features
    # --depot-tool 命令行优先，覆盖配置文件值（与其他选项行为一致）
    if getattr(args, "depot_tool", None):
        cfg["depot_tool"] = args.depot_tool
    if getattr(args, "registry_base", None):
        cfg["registry_base"] = args.registry_base

    args.func(args, cfg)


if __name__ == "__main__":
    main()