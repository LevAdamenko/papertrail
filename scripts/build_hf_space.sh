#!/bin/sh
# Assemble a Hugging Face Space (Docker SDK) for the judge demo into an empty directory.
# Usage: scripts/build_hf_space.sh OUT_DIR
# The Space holds only what the offline demo needs: no keys, no tests, no git history.
# Push it with: cd OUT_DIR && git init && git remote add origin https://huggingface.co/spaces/<user>/papertrail
#               && git add -A && git commit -m demo && git push origin HEAD:main
set -eu
out=${1:?usage: build_hf_space.sh OUT_DIR}
root=$(cd "$(dirname "$0")/.." && pwd)
if [ -e "$out" ] && [ -n "$(ls -A "$out")" ]; then
    echo "refusing to write into non-empty $out" >&2
    exit 1
fi
mkdir -p "$out"
cp -R "$root/papertrail" "$root/samples" "$out/"
cp "$root/Dockerfile" "$root/.dockerignore" "$root/LICENSE" "$out/"
find "$out" -name __pycache__ -type d -prune -exec rm -rf {} +
{
    cat <<'META'
---
title: PaperTrail
emoji: 📬
colorFrom: green
colorTo: gray
sdk: docker
app_port: 8000
license: mit
short_description: Official letter photo to deadlines, checked on gov sites
---

> Demo Space: sample letters replayed from recorded model responses and one recorded live
> verification run. No API keys here, no outbound calls, uploads disabled.

META
    cat "$root/README.md"
} > "$out/README.md"
echo "Space ready in $out"
