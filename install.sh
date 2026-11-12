#!/bin/sh
# Install hc-orchestrate for Claude Code and Codex, plus its hc agent templates.
#   sh install.sh            copy into place (safe to rerun; asks before replacing changed templates)
#   sh install.sh --link     symlink the skill instead of copying (edits here apply everywhere)
#   sh install.sh --force    replace existing templates without asking
set -eu

here=$(cd "$(dirname "$0")" && pwd -P)   # physical path: $HOME or a parent may be a symlink
mode=copy
force=0
for arg in "$@"; do
  case "$arg" in
    --link) mode=link ;;
    --force) force=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

install_skill() { # $1 = skills folder
  dest="$1/hc-orchestrate"
  mkdir -p "$1"
  if [ "$(cd "$1" && pwd -P)/hc-orchestrate" = "$here" ]; then
    echo "skill already lives at $dest"
    return
  fi
  if [ "$mode" = link ]; then
    rm -rf "$dest"
    ln -s "$here" "$dest"
  else
    # Only what the skill needs, built next to the destination and moved in, so a failed copy keeps the old install.
    new="$1/.hc-orchestrate.new.$$"
    rm -rf "$new"
    if ! { mkdir "$new" &&
           cp "$here/SKILL.md" "$here/README.md" "$here/install.sh" "$new/" &&
           cp -R "$here/scripts" "$here/references" "$here/templates" "$new/" &&
           find "$new" -name __pycache__ -prune -exec rm -rf {} + ; }; then
      rm -rf "$new"
      echo "copy to $dest failed; the old install (if any) is untouched" >&2
      exit 1
    fi
    rm -rf "$dest"
    mv "$new" "$dest"
  fi
  echo "skill -> $dest ($mode)"
}

install_skill "$HOME/.claude/skills"   # Claude Code: /hc-orchestrate <task>
install_skill "$HOME/.agents/skills"   # Codex:       $hc-orchestrate <task>

tdir="${XDG_CONFIG_HOME:-$HOME/.config}/herdr-core/templates"
mkdir -p "$tdir"
kept=""
for t in "$here"/templates/*.md; do
  name=$(basename "$t")
  if [ -f "$tdir/$name" ] && ! cmp -s "$t" "$tdir/$name"; then
    if [ "$force" -ne 1 ]; then
      printf "template %s differs from yours. Replace it? [y/N] " "$name"
      read -r answer || answer=n
      case "$answer" in y|Y) ;; *) echo "kept your $name"; kept="$kept $name"; continue ;; esac
    fi
  fi
  cp "$t" "$tdir/$name"
  echo "template -> $tdir/$name"
done

if [ -n "$kept" ]; then
  echo
  echo "NOTE: these templates differ from the ones in this checkout and were kept:$kept"
  echo "      The skill expects the checkout's version. To replace them: sh install.sh --force"
fi
echo
if command -v hc >/dev/null 2>&1; then
  hc templates || true
else
  echo "hc is not installed yet: curl -fsSL https://releases.herdr.dev/core/install.sh | sh"
fi
echo
echo "Next: start your agent inside hc, then run the skill's preflight:"
echo "  hc run --bg --name lead --cwd \"\$PWD\" claude && hc attach lead"
echo "  python3 ~/.claude/skills/hc-orchestrate/scripts/hco.py preflight"
echo "Then, in Claude Code: /hc-orchestrate <task>   (Codex: \$hc-orchestrate <task>)"
