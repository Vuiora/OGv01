#!/bin/bash

set -e

API_KEY="${CODEXZH_API_KEY:-__API_KEY__}"
if [ -z "$API_KEY" ] || [ "$API_KEY" = "__API_KEY__" ]; then
  echo "错误：未检测到 API Key，请从控制台重新复制脚本"
  exit 1
fi

CODEX_DIR="$HOME/.codex"
mkdir -p "$CODEX_DIR"
CFG="$CODEX_DIR/config.toml"
AUTH="$CODEX_DIR/auth.json"

# 已有文件先备份为 .bak
if [ -f "$CFG" ]; then
  cp "$CFG" "$CFG.bak"
  echo "检测到已有 config.toml，原文件已备份为：$CFG.bak"
fi
if [ -f "$AUTH" ]; then
  cp "$AUTH" "$AUTH.bak"
  echo "检测到已有 auth.json，原文件已备份为：$AUTH.bak"
fi

if [ -f "$CFG" ]; then
  # 合并 config.toml：先删掉旧的 [model_providers.codexzh] 段（稍后整段重写）
  TMP1="$(mktemp)"
  TMP2="$(mktemp)"
  awk '
    /^\[model_providers\.codexzh\]/ { skip = 1; next }
    /^\[/ { skip = 0 }
    !skip { print }
  ' "$CFG" > "$TMP1"
  # 再合并顶层键：已有同名键原地改值，缺失的插到首个 [ 段之前，其余配置全部保留
  awk '
    function flush_missing(  i) {
      for (i = 1; i <= nk; i++) if (!done[i]) { print v[i]; done[i] = 1 }
    }
    BEGIN {
      nk = 8
      k[1] = "model_provider";           v[1] = "model_provider = \"codexzh\""
      k[2] = "model";                    v[2] = "model = \"gpt-5.6-terra\""
      k[3] = "model_reasoning_effort";   v[3] = "model_reasoning_effort = \"high\""
      k[4] = "disable_response_storage"; v[4] = "disable_response_storage = false"
      k[5] = "cache_size_mb";            v[5] = "cache_size_mb = 512"
      k[6] = "cache_ttl";                v[6] = "cache_ttl = \"30m\""
      k[7] = "smart_cache";              v[7] = "smart_cache = true"
      k[8] = "cache_compression";        v[8] = "cache_compression = true"
      intop = 1
    }
    /^\[/ { if (intop) { flush_missing(); print ""; intop = 0 } }
    {
      if (intop) {
        for (i = 1; i <= nk; i++) {
          if ($0 ~ ("^[ \t]*" k[i] "[ \t]*=")) {
            if (!done[i]) { print v[i]; done[i] = 1 }
            next
          }
        }
      }
      print
    }
    END { if (intop) flush_missing() }
  ' "$TMP1" > "$TMP2"
  mv "$TMP2" "$CFG"
  rm -f "$TMP1"
  echo "已合并 config.toml（中转站配置已写入，其余配置保留）：$CFG"
else
  cat > "$CFG" << 'EOF'
model_provider = "codexzh"
model = "gpt-5.6-terra"
model_reasoning_effort = "high"
disable_response_storage = false

# 缓存优化配置
cache_size_mb = 512
cache_ttl = "30m"
smart_cache = true
cache_compression = true
EOF
  echo "已创建 config.toml：$CFG"
fi

# 统一在文件末尾重写 [model_providers.codexzh] 段（旧段已在上面删除）
cat >> "$CFG" << 'EOF'

[model_providers.codexzh]
name = "codexzh"
base_url = "https://api.codexzh.com/v1"
wire_api = "responses"
requires_openai_auth = true
web_search = "live"
EOF

# auth.json：重建为只含 OPENAI_API_KEY 的新文件
cat > "$AUTH" << EOF
{
  "OPENAI_API_KEY": "$API_KEY"
}
EOF
echo "已写入 auth.json（仅含 OPENAI_API_KEY）：$AUTH"

chmod 600 "$CFG" "$AUTH" || true

echo ""
echo "配置完成！可以打开 Codex 使用了。"
