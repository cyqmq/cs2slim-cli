#!/usr/bin/env bash
# ============================================================================
# cs2slim 纯 bash prebuilt 路径（无 Python 依赖）
#
# 用途: 直接拉取 GitHub Release 核心包并拼装精简服务端。仅依赖 curl/unzip/tar/sed。
# 与 get-cs2slim.sh 的 CS2_MODE=prebuilt 等价，但不调用 Python CLI。
#
# 用法（环境变量，与 get-cs2slim.sh 一致）:
#   CS2_WORKDIR   工作目录，默认 $HOME/cs2-slim-build
#   CS2_MAPS      逗号分隔地图，默认 de_dust2（核心包已内置）
#   CS2_FEATURES  逗号分隔功能，默认空
#   CS2_GH_PROXY  GitHub 加速代理前缀（如 https://ghproxy.com）
#   CS2_PACKAGE   1=完成后打包 tar.gz，默认 0
#
# 直接运行:
#   export CS2_WORKDIR=/path CS2_MAPS=de_dust2
#   bash scripts/prebuilt.sh
# ============================================================================
set -euo pipefail

WORKDIR="${CS2_WORKDIR:-$HOME/cs2-slim-build}"
MAPS="${CS2_MAPS:-de_dust2}"
FEATURES="${CS2_FEATURES:-}"
PACKAGE="${CS2_PACKAGE:-0}"
GH_PROXY="${CS2_GH_PROXY:-}"
CORE_URL="https://github.com/cyqmq/cs2-slim-replica/releases/latest/download/cs2-slim.tar.gz"
STEAM_MANIFEST_URL="https://client-update.akamai.steamstatic.com/steam_client_ubuntu12"
REG_BASE="https://raw.githubusercontent.com/cyqmq/cs2-slim-replica/main/registry"
CORE_NAME="cs2-slim.tar.gz"
TREE="$WORKDIR/slim"

# GitHub 代理前缀
gh_url() {
  local u="$1"
  if [ -n "$GH_PROXY" ] && { [ "${u#https://github.com/}" != "$u" ] || [ "${u#https://raw.githubusercontent.com/}" != "$u" ]; }; then
    echo "${GH_PROXY%/}/$u"
  else
    echo "$u"
  fi
}

# 带重试的下载（断点续传）
download() { # $1=url $2=dest $3=desc
  local url dest desc
  url="$(gh_url "$1")"; dest="$2"; desc="${3:-$(basename "$2")}"
  echo "下载 $desc ..."
  curl -fL --retry 3 -C - --connect-timeout 15 --max-time 1800 --progress-bar -o "$dest" "$url"
}

# 解析注册表 JSON 中某组件对象的 prebuilt_url（纯 sed，不依赖 jq/python）
json_prebuilt_url() { # $1=json_file $2=key
  sed -n "/\"$2\"[[:space:]]*:/,/}/ {
    /\"prebuilt_url\"/ {
      s/.*\"prebuilt_url\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p
      q
    }
  }" "$1"
}

# 确保精简树根目录有有效 steamclient.so（缺失/损坏时从 Steam SDK 包补下）
ensure_steamclient() {
  local dst="$TREE/steamclient.so"
  if [ -s "$dst" ] && head -c4 "$dst" | grep -q $'\x7fELF'; then
    echo "  steamclient.so 已存在且有效 (ELF 64-bit)"
    return
  fi
  echo "  steamclient.so 缺失或无效，从 Steam SDK 包重新获取 ..."
  local manifest="$WORKDIR/steam_client_ubuntu12" sdk_zip="$WORKDIR/bins_sdk.zip"
  download "$STEAM_MANIFEST_URL" "$manifest" "Steam manifest"
  local sdk_file
  sdk_file="$(grep -A4 '"bins_sdk_ubuntu12"' "$manifest" | grep '"file"' | sed -n 's/.*"file"[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ -z "$sdk_file" ]; then echo "错误: 无法在 Steam manifest 中找到 bins_sdk_ubuntu12" >&2; exit 1; fi
  download "https://steamcdn-a.akamaihd.net/client/$sdk_file" "$sdk_zip" "bins_sdk_ubuntu12"
  local tmp="$WORKDIR/steamclient_extract"
  rm -rf "$tmp"; mkdir -p "$tmp"
  unzip -qo "$sdk_zip" -d "$tmp"
  mv -f "$tmp/linux64/steamclient.so" "$dst" 2>/dev/null || cp -f "$tmp/linux64/steamclient.so" "$dst"
  rm -rf "$tmp" "$sdk_zip" "$manifest"
  echo "  steamclient.so 已更新"
}

# 更新启动脚本为最新版（含 VPN/TUN 网络优化参数）
update_start_scripts() {
  local url="https://raw.githubusercontent.com/cyqmq/cs2-slim-replica/main/core/deploy/linux/start_server.sh"
  local dst="$TREE/start_server.sh"
  if curl -fL --retry 3 -m 60 -o "$dst.new" "$(gh_url "$url")" 2>/dev/null && [ -s "$dst.new" ]; then
    mv -f "$dst.new" "$dst"
    chmod +x "$dst"
    echo "  已更新启动脚本（含 VPN/TUN 网络优化参数）"
  else
    rm -f "$dst.new"
    echo "  WARN: 更新启动脚本失败，使用包内脚本" >&2
  fi
}

# gameinfo.gi 打补丁：在 Game_LowViolence 后插入 Game <addon>（用于 Metamod/Swiftly/ModSharp）
patch_gameinfo() { # $1=addon $2=label
  local addon="$1" label="$2" gi="$TREE/game/csgo/gameinfo.gi"
  [ -f "$gi" ] || { echo "  WARN: 未找到 gameinfo.gi，跳过 $label 补丁"; return; }
  if grep -qE '^[[:space:]]*Game[[:space:]]+'"$addon" "$gi"; then
    echo "  $label: gameinfo.gi 已包含 $addon，跳过补丁"
    return
  fi
  # 在第一个 Game_LowViolence 行后插入
  awk -v addon="$addon" '
    { print }
    !done && /^[[:space:]]*Game_LowViolence/ {
      match($0, /^[[:space:]]*/)
      print substr($0, 1, RLENGTH) "Game\t" addon
      done = 1
    }
  ' "$gi" > "$gi.new" && mv -f "$gi.new" "$gi"
  echo "  $label: 已在 gameinfo.gi 中添加 Game $addon"
}

mkdir -p "$WORKDIR"

# 1. 核心包
CORE_PATH="$WORKDIR/$CORE_NAME"
if [ ! -f "$CORE_PATH" ]; then
  download "$CORE_URL" "$CORE_PATH" "核心精简包 $CORE_NAME"
else
  echo "复用已缓存核心包: $CORE_PATH"
fi

# 2. 解压核心包
if [ -d "$TREE" ]; then
  echo "WARN: 已有精简树 $TREE，将被覆盖（请先备份你改过的文件）。"
  rm -rf "$TREE"
fi
mkdir -p "$TREE"
echo "解压核心包到 $TREE ..."
tar xzf "$CORE_PATH" -C "$TREE"

# 3. steamclient + 启动脚本 + 执行权限
ensure_steamclient
update_start_scripts
chmod +x "$TREE/game/bin/linuxsteamrt64/cs2" 2>/dev/null || true
chmod +x "$TREE/start_server.sh" 2>/dev/null || true

# 4. 地图组件（核心包已含 de_dust2；其余从 maps 注册表拉预构建包）
REQ_MAPS="$(echo "$MAPS" | tr ',' ' ')"
MAPS_JSON="$WORKDIR/maps.json"
[ -f "$MAPS_JSON" ] || curl -fL --retry 2 -m 60 -o "$MAPS_JSON" "$(gh_url "$REG_BASE/maps.json")" 2>/dev/null || true
for m in $REQ_MAPS; do
  [ "$m" = "de_dust2" ] && continue
  url="$(json_prebuilt_url "$MAPS_JSON" "$m")"
  if [ -z "$url" ]; then
    echo "错误: 地图 $m 无预构建包或注册表获取失败，prebuilt 模式无法安装（请改用 source 模式）" >&2
    exit 1
  fi
  mzip="$WORKDIR/$m.zip"
  [ -f "$mzip" ] || download "$url" "$mzip" "地图 $m"
  echo "  拼装地图 $m ..."
  unzip -qo "$mzip" -d "$TREE"
done

# 5. 功能组件（features 注册表）
REQ_FEAT="$(echo "$FEATURES" | tr ',' ' ')"
FEAT_JSON="$WORKDIR/features.json"
[ -f "$FEAT_JSON" ] || curl -fL --retry 2 -m 60 -o "$FEAT_JSON" "$(gh_url "$REG_BASE/features.json")" 2>/dev/null || true
for f in $REQ_FEAT; do
  url="$(json_prebuilt_url "$FEAT_JSON" "$f")"
  if [ -z "$url" ]; then
    echo "错误: 功能 $f 无预构建包（请改用 source 模式）" >&2
    exit 1
  fi
  fzip="$WORKDIR/${f}-pack.zip"
  [ -f "$fzip" ] || download "$url" "$fzip" "功能 $f"
  echo "  拼装功能 $f ..."
  unzip -qo "$fzip" -d "$TREE"
done

# 6. 功能后置补丁（独立框架需加载自定义 GameDLL）
for f in $REQ_FEAT; do
  case "$f" in
    metamod|metamod-win)      patch_gameinfo "csgo/addons/metamod" "Metamod" ;;
    swiftly|swiftly-win)      patch_gameinfo "csgo/addons/swiftlys2" "SwiftlyS2" ;;
    modsharp|modsharp-win)    patch_gameinfo "sharp" "ModSharp" ;;
  esac
done
if [ -f "$TREE/cs2lm" ]; then chmod +x "$TREE/cs2lm" 2>/dev/null || true; fi

# 7. 可选打包
if [ "$PACKAGE" = "1" ]; then
  echo "打包 $TREE -> $WORKDIR/cs2-slim-linux.tar.gz ..."
  tar czf "$WORKDIR/cs2-slim-linux.tar.gz" -C "$TREE" .
  echo "打包完成: $WORKDIR/cs2-slim-linux.tar.gz"
fi

echo
echo "预构建模式完成!"
echo "  启动: bash $TREE/start_server.sh"
echo "  提示: 地图/功能包已缓存到 $WORKDIR，重复执行可复用。"
