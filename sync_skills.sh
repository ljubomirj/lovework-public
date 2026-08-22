#!/usr/bin/env bash
# sync_skills.sh — distribute the canonical lovework skill to all agent paths.
#
# The canonical home is:
#   ~/LJ-work-2026/lovework/agents/skills/lovework/SKILL.md
#
# All agent-specific paths (in-repo and per-host) are symlinks pointing
# at the canonical file. This script is idempotent: it (re)creates the
# symlinks and copies the canonical to per-host paths.
#
# Usage:
#   bash sync_skills.sh            # symlinks in repo + copy to Hermes
#   bash sync_skills.sh --symlink  # symlinks in repo + symlink Hermes (one-way)
#
# Run this whenever you edit the canonical SKILL.md.

set -euo pipefail

# Resolve the repo root. Try explicit env var first, then probe principal
# locations, preferring the location-agnostic ~/LJ-work-2026/ (all hosts),
# then the legacy ~/Documents/ (macbook2).
if [[ -n "${LOVEWORK_REPO_ROOT:-}" ]]; then
  REPO_ROOT="$LOVEWORK_REPO_ROOT"
else
  for principal in "$HOME/LJ-work-2026/lovework" "$HOME/Documents/LJ-work-2026/lovework"; do
    if [[ -f "$principal/agents/skills/lovework/SKILL.md" ]]; then
      REPO_ROOT="$principal"
      break
    fi
  done
  if [[ -z "${REPO_ROOT:-}" ]]; then
    echo "FATAL: could not locate the lovework repo root. Set LOVEWORK_REPO_ROOT." >&2
    exit 1
  fi
fi

CANONICAL="$REPO_ROOT/agents/skills/lovework/SKILL.md"
MODE="${1:-copy}"

# Guard: the canonical must be a real file, not a symlink. A previous run with
# a wrong REPO_ROOT once turned it into a self-referential loop; refuse to
# proceed if it's not a plain file.
if [[ ! -f "$CANONICAL" || -L "$CANONICAL" ]]; then
  echo "FATAL: canonical $CANONICAL is missing or is a symlink (expected a real file)." >&2
  echo "       A backup may sit alongside it as SKILL.md.bak.* — restore before re-running." >&2
  exit 1
fi

echo "Canonical: $CANONICAL ($(wc -l < "$CANONICAL") lines)"

# ── In-repo directory-level symlinks (verify only) ──────────────────────
# Each agent's in-repo skills dir is a *directory-level* symlink:
#   .claude/skills/lovework -> ../../agents/skills/lovework
#   .codex/skills/lovework  -> ../../agents/skills/lovework
# So the SKILL.md inside each path resolves to the canonical automatically.
# We only verify these directory symlinks exist — we do NOT touch SKILL.md
# at this level. (A previous version of this script created file-level
# symlinks inside paths that were already directory-symlinked, which
# transparently clobbered the canonical and turned it into a self-loop.)
#
# If a dir-level symlink is missing, create it. That is the only write here.

REPO_DIR_LINKS=(
  "$REPO_ROOT/.claude/skills/lovework"
  "$REPO_ROOT/.codex/skills/lovework"
)
DIR_REL_TARGET="../../agents/skills/lovework"

for dlink in "${REPO_DIR_LINKS[@]}"; do
  mkdir -p "$(dirname "$dlink")"
  if [[ -L "$dlink" && "$(readlink "$dlink")" == "$DIR_REL_TARGET" ]]; then
    echo "  ✓ $dlink (dir symlink OK)"
  elif [[ -L "$dlink" ]]; then
    echo "  ! $dlink (symlink → $(readlink "$dlink"), expected $DIR_REL_TARGET)"
  elif [[ -e "$dlink" ]]; then
    echo "  ! $dlink exists but is not a symlink — leaving untouched (manual fix needed)"
  else
    ln -s "$DIR_REL_TARGET" "$dlink"
    echo "  + $dlink (new dir symlink → $DIR_REL_TARGET)"
  fi
done

# ── Per-host Hermes ──────────────────────────────────────────────────────
# The Hermes skill lives at ~/.hermes-<host>/profiles/<profile>/skills/.
# We can either:
#   (a) copy  — safe, idempotent, but needs re-running after edits
#   (b) symlink — instant, but ties the per-host config to the repo path
#
# Default: copy (safer for the "never break the per-host config" house
# rule). Pass --symlink to use symlinks instead.

# The Hermes skill lives at either:
#   ~/.hermes-<host>/skills/productivity/lovework/SKILL.md            (host-global)
#   ~/.hermes-<host>/profiles/<profile>/skills/productivity/lovework/SKILL.md  (per-profile)
# Match both shapes.
HERMES_GLOB="$HOME/.hermes-*/skills/productivity/lovework/SKILL.md
$HOME/.hermes-*/profiles/*/skills/productivity/lovework/SKILL.md"
shopt -s nullglob
HERMES_FILES=($HERMES_GLOB)
shopt -u nullglob

if [[ ${#HERMES_FILES[@]} -gt 0 ]]; then
  for hf in "${HERMES_FILES[@]}"; do
    if [[ "$MODE" == "--symlink" ]]; then
      rm -f "$hf"
      ln -s "$CANONICAL" "$hf"
      echo "  + $hf (symlink → $CANONICAL)"
    else
      cp "$CANONICAL" "$hf"
      echo "  ✓ $hf (copied, $(wc -l < "$hf") lines)"
    fi
  done
else
  echo "  (no per-host Hermes skill files found at $HERMES_GLOB)"
fi

# ── OpenCode skill path (Mac/Linux) ─────────────────────────────────────
# Some setups put skills under .opencode/skills/ or similar. We try a
# couple of likely locations; missing ones are silently skipped.
OPENCODE_PATHS=(
  "$REPO_ROOT/.opencode/skills/lovework/SKILL.md"
)
for of in "${OPENCODE_PATHS[@]}"; do
  if [[ -d "$(dirname "$of")" ]]; then
    rel_target="../../../agents/skills/lovework/SKILL.md"
    if [[ ! -L "$of" ]] && [[ -e "$of" ]]; then
      backup="${of}.bak.$(date +%Y%m%d-%H%M%S)"
      mv "$of" "$backup"
      ln -s "$rel_target" "$of"
      echo "  ! backed up + linked $of"
    else
      ln -sf "$rel_target" "$of"
      echo "  + $of"
    fi
  fi
done

echo
echo "Done. Re-run after editing $CANONICAL."
