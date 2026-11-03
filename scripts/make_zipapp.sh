#!/usr/bin/env bash
# 构建单文件可执行 cs2slim.pyz（zipapp，仅需 Python 3 标准库，无第三方依赖）
#
# 用法:
#   bash scripts/make_zipapp.sh            # 生成 ./cs2slim.pyz
#   bash scripts/make_zipapp.sh /tmp/out   # 生成 /tmp/out/cs2slim.pyz
#
# 产物可直接运行（带 shebang）:
#   ./cs2slim.pyz --version
#   ./cs2slim.pyz prebuilt --dry-run --platform linux --maps de_dust2
#   python3 cs2slim.pyz status
set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="${1:-.}"
mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "== 构建 zipapp (cs2slim.pyz) =="
mkdir -p "$STAGE/app"

# 1. 打包 cs2slim/ 包（cli + registry/state + 模板，包数据随包）
cp -r cs2slim "$STAGE/app/cs2slim"

# 2. __main__.py 入口（zipapp 启动即运行 CLI）
printf 'from cs2slim import main\nmain()\n' > "$STAGE/app/__main__.py"

# 3. 打包为可执行 zipapp（带 shebang）
python3 -m zipapp "$STAGE/app" -o "$OUT_DIR/cs2slim.pyz" -p '/usr/bin/env python3'
chmod +x "$OUT_DIR/cs2slim.pyz"

echo "生成: $OUT_DIR/cs2slim.pyz ($(du -h "$OUT_DIR/cs2slim.pyz" | cut -f1))"
echo "试用: $OUT_DIR/cs2slim.pyz --version"
