#!/usr/bin/env bash
# Smoke-test the codex CLI + deepseek provider: text Q&A, apply_patch tool
# call, and image-input grading. Requires DEEPSEEK_API_KEY in the environment.
set -euo pipefail

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "error: DEEPSEEK_API_KEY is not set (try: set -a; . ~/.bashrc; set +a)" >&2
  exit 2
fi

WORK="$(mktemp -d /tmp/codex-smoke.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

echo "=== [1/3] text Q&A ==="
codex exec --skip-git-repo-check "1+1等于几？只回答数字。"

echo "=== [2/3] apply_patch tool call ==="
codex exec --skip-git-repo-check \
  "用 apply_patch 在当前目录创建 hello.txt，内容为一行 hello，然后结束。"
test -f hello.txt && grep -q hello hello.txt && echo "hello.txt created OK"

echo "=== [3/3] image input grading ==="
python3 - <<'PY'
from PIL import Image, ImageDraw
img = Image.new("RGB", (400, 200), "white")
d = ImageDraw.Draw(img)
d.text((20, 60), "1+1=3", fill="black")
img.save("math.png")
PY
codex exec --skip-git-repo-check \
  '这是一道小学生数学作业的照片。判断对错，只输出一行 JSON：{"correct": true|false, "correct_answer": "..."}' \
  -i math.png

echo "=== all smoke checks passed ==="
