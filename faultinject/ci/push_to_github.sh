#!/usr/bin/env bash
# Push the BMC platform repo to GitHub and verify CI will trigger.
#
# Prerequisite (ONE manual step, only your GitHub account can do it):
#   1. open https://github.com/new
#   2. Repository name: bmc-qemu-platform   (leave empty: no README/gitignore)
#   3. Create repository
#   4. If your fine-grained PAT is not "All repositories" scoped, add this
#      repo to the token: https://github.com/settings/personal-access-tokens
#      (Contents: Read and write). A classic PAT with 'repo' scope also works.
#
# Then run this script (or ask the agent to run it):
#   bash faultinject/ci/push_to_github.sh
set -euo pipefail

OWNER="${OWNER:-WangXianzhen}"
REPO="${REPO:-bmc-qemu-platform}"
URL="https://github.com/$OWNER/$REPO.git"
ACTIONS_URL="https://github.com/$OWNER/$REPO/actions"

cd "$(dirname "$0")/../.."

git remote set-url origin "$URL"

if ! git ls-remote "$URL" >/dev/null 2>&1; then
  echo "ERROR: repo $OWNER/$REPO does not exist yet."
  echo "  Create it (1 min): https://github.com/new  -> name: $REPO (empty repo)"
  echo "  Then re-run this script."
  exit 1
fi

echo "== repo exists; pushing =="
export GIT_TERMINAL_PROMPT=0
if git push -u origin main; then
  echo
  echo "== pushed OK =="
  git ls-remote origin | sed 's/\t/  /' | head -3
  echo
  echo "CI 将在 push 后自动触发（约 15-20 分钟）："
  echo "  $ACTIONS_URL"
  echo "首次运行：构建 QEMU+补丁 -> 控制平面检查 -> AST2700 启动 + pytest -> 性能门禁"
else
  echo
  echo "ERROR: push failed. 常见原因与处理："
  echo "  - Repository not found : 先创建仓库（上面步骤）"
  echo "  - 403/403 Resource not accessible : 给 PAT 添加该仓库的 Contents:write 权限"
  echo "    (https://github.com/settings/personal-access-tokens)，或改用 classic PAT(repo scope)"
  exit 1
fi
