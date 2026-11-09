#!/usr/bin/env bash
# Symlink every skill in this repo into ~/.claude/skills for development.
# Re-running is safe; an existing real directory is backed up first.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
skills_dir="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$skills_dir"

for src in "$here"/skills/*/; do
  src="${src%/}"
  name="$(basename "$src")"
  dst="$skills_dir/$name"
  chmod +x "$src"/scripts/* 2>/dev/null || true
  if [[ -L "$dst" ]]; then
    rm "$dst"
  elif [[ -d "$dst" ]]; then
    backup="$dst.bak.$(date +%s)"
    echo "existing directory moved to $backup"
    mv "$dst" "$backup"
  fi
  ln -s "$src" "$dst"
  echo "linked $dst -> $src"
done

command -v python3 >/dev/null || echo "warning: python3 not found; the skills need it"
echo "start a new Claude Code session"
