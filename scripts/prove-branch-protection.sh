#!/usr/bin/env bash
#
# Prove that this repository's branch protection actually refuses a force-push
# and a branch deletion -- without ever pointing either operation at main.
#
# The refusal lives in GitHub's protection enforcement, not in this repo, so
# "just try it on main" is the only way to prove main's copy works and the only
# way to risk destroying main. Instead: stand up a disposable branch carrying a
# byte-for-byte copy of main's rule set (.github/branch-protection.json), point
# the two operations at THAT branch, and record what the server says.
#
# What that establishes: the recorded rule set, when enforced, refuses a
# force-push and a deletion. Same enforcement path GitHub applies to main.
# What it does NOT establish: anything about main's live state at probe time.
# It is a strong argument, not a demonstration of main.
#
# Two controls run, because a refusal proves nothing on its own -- a typo'd
# refspec and a broken credential also produce refusals:
#
#   control 1  a normal fast-forward push to the stand-in SUCCEEDS before
#              protection is applied, proving the push path, the credential and
#              the refspec are all good before anything is attributed to a hook
#   control 2  an unprotected sibling branch is created and DELETED successfully,
#              proving the delete path works before a delete refusal is
#              attributed to protection
#
# Neither operation uses --dry-run. A dry run never reaches the server's hooks,
# so it cannot observe a hook decision; the entire point here is the server's
# answer, so the probes are real.
#
# Usage:  scripts/prove-branch-protection.sh [standin-branch-name]
# Requires: gh (authenticated with repo scope), git, python.
# Exit 0 = every control behaved and every probe was refused, repo left clean.
# Exit 1 = something did not hold; the transcript says what.

set -u

PROBE_REF="${1:-protection-probe}"
CONTROL_REF="${PROBE_REF}-delete-control"
ROOT="$(pwd)"
RECORD=".github/branch-protection.json"
REPO="pvnkmnk/linnetworker"
PROTECT_REPO="repos/${REPO}"

# gh's API paths are full of slashes, which MSYS rewrites into backslashes
# unless path conversion is off for that one command. git is left alone: its
# worktree paths ARE posix paths and need the conversion.
gh_api() { MSYS_NO_PATHCONV=1 gh api "$@"; }

# ---------------------------------------------------------------- safety rails

# Pre-flight abort: the trap is not registered yet, so nothing can have run.
fail() { echo; echo "PROBE ABORTED (before any change): $*"; echo "No probe ran; main was never a target."; exit 1; }
# In-flight abort: steps may already have run. Cleanup is registered and runs.
stop() { echo; echo "PROBE STOPPED EARLY: $*"; echo "Some steps above did run; main was never a target. Cleanup follows."; exit 1; }

case "$PROBE_REF" in
  main|master|refs/heads/main|refs/heads/master)
    fail "refusing to run: the stand-in name must never be main." ;;
esac
[ -f "$RECORD" ] || fail "no $RECORD to copy the rule set from."

# Never adopt or clobber a branch somebody else owns.
existing="$(git ls-remote --heads origin "$PROBE_REF" "$CONTROL_REF" 2>/dev/null)"
if [ -n "$existing" ]; then
  fail "refusing to run: $PROBE_REF or $CONTROL_REF already exists on the remote.
$existing
Delete it by hand first; this script will not touch a branch it did not create."
fi

MAIN_SHA_BEFORE="$(git ls-remote origin refs/heads/main | cut -f1)"
MAIN_PROT_BEFORE="$(gh_api "$PROTECT_REPO/branches/main/protection" | python -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])')"
[ -n "$MAIN_SHA_BEFORE" ] || fail "could not read main's sha; refusing to start."

WORKTREE="$(mktemp -d)"
BRANCH_GONE=0

# --------------------------------------------------------------------- cleanup
# Registered before the first mutation so an abort anywhere below still cleans up.
cleanup() {
  # The trap fires with cwd wherever the script last stood -- which is INSIDE
  # the probe worktree once step 1 has cd'd there. Git refuses to remove the
  # worktree that is the current directory, so step back to the checkout first.
  cd "$ROOT" || { echo "PROBE ABORTED: cannot return to $ROOT to clean up."; exit 1; }
  echo
  echo "=============================================================="
  echo "CLEANUP"
  echo "=============================================================="
  if [ "$BRANCH_GONE" = "1" ]; then
    echo "stand-in $PROBE_REF already removed; skipping (run completed)"
  else
    echo "\$ gh api -X DELETE $PROTECT_REPO/branches/$PROBE_REF/protection"
    gh_api -X DELETE "$PROTECT_REPO/branches/$PROBE_REF/protection" 2>&1 | sed 's/^/    /'
    echo "[protection removed: exit ${PIPESTATUS[0]}]"

    echo "\$ git push origin --delete $PROBE_REF"
    git push origin --delete "$PROBE_REF" 2>&1 | sed 's/^/    /'
    echo "[delete exit ${PIPESTATUS[0]}]"
  fi
  git worktree remove --force "$WORKTREE" 2>/dev/null
  git branch -D "local/${PROBE_REF}" 2>/dev/null | sed 's/^/    /'
  rm -rf "$WORKTREE"

  echo
  echo "--- state of the remote after cleanup ---"
  echo "\$ git ls-remote --heads origin"
  git ls-remote --heads origin 2>&1 | sed 's/^/    /'

  MAIN_SHA_AFTER="$(git ls-remote origin refs/heads/main | cut -f1)"
  MAIN_PROT_AFTER="$(gh_api "$PROTECT_REPO/branches/main/protection" | python -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])')"

  echo
  echo "--- main, before vs after ---"
  echo "  sha        $MAIN_SHA_BEFORE -> $MAIN_SHA_AFTER"
  echo "  protection $MAIN_PROT_BEFORE -> $MAIN_PROT_AFTER"
  if [ "$MAIN_SHA_BEFORE" = "$MAIN_SHA_AFTER" ] && [ "$MAIN_PROT_BEFORE" = "$MAIN_PROT_AFTER" ]; then
    echo "  RESULT: main UNCHANGED (same sha, same protection)."
  else
    echo "  RESULT: MAIN CHANGED. This probe should never have done that."
  fi
}
trap cleanup EXIT

# --------------------------------------------------------------------- helpers
run() {
  echo "\$ $*"
  "$@" 2>&1 | sed 's/^/    /'
  local rc="${PIPESTATUS[0]}"
  echo "[exit $rc]"
  echo
  return $rc
}

RESULTS=""
record() { RESULTS="${RESULTS}$1"$'\n'; }

echo "=============================================================="
echo "BRANCH PROTECTION PROBE"
echo "=============================================================="
echo "repo         $REPO"
echo "stand-in     $PROBE_REF   (disposable; main is never a target)"
echo "rule source  $RECORD"
echo "main sha     $MAIN_SHA_BEFORE"
echo "main prot    $MAIN_PROT_BEFORE  (sha256 prefix of the live protection)"
echo
echo "Refspecs this script will push -- and nothing else:"
echo "  create stand-in   $MAIN_SHA_BEFORE:refs/heads/$PROBE_REF"
echo "  control push      refs/heads/local/${PROBE_REF}:refs/heads/$PROBE_REF"
echo "  force-push probe  +refs/heads/local/${PROBE_REF}:refs/heads/$PROBE_REF --force"
echo "  delete probe      refs/heads/${PROBE_REF}  (git push origin --delete)"
echo
echo "No refspec naming main appears in this script."
echo

# ------------------------------------------------------------------- 1. stand-up
echo "--------------------------------------------------------------"
echo "1. CREATE THE STAND-IN (unprotected)"
echo "--------------------------------------------------------------"
echo "\$ git push origin $MAIN_SHA_BEFORE:refs/heads/$PROBE_REF"
git push origin "$MAIN_SHA_BEFORE:refs/heads/$PROBE_REF" 2>&1 | sed 's/^/    /'
rc=${PIPESTATUS[0]}; echo "[exit $rc]"; echo
if [ "$rc" != "0" ]; then stop "could not create the stand-in branch; refusing to continue."; fi

# An isolated worktree so the operator's main checkout is never checked out,
# moved or dirtied by any of this.
git worktree add -b "local/${PROBE_REF}" "$WORKTREE" "$MAIN_SHA_BEFORE" >/dev/null 2>&1 \
  || stop "could not create the probe worktree."
cd "$WORKTREE" || fail "probe worktree vanished."

# ------------------------------------------------------------------- 2. control 1
echo "--------------------------------------------------------------"
echo "2. CONTROL 1 -- a normal push must SUCCEED (branch still unprotected)"
echo "--------------------------------------------------------------"
echo "Proves the push path, the credential and the refspec are sound BEFORE"
echo "any refusal is attributed to a protection hook."
echo
echo "probe-marker" > probe-marker.txt
git -c core.autocrlf=false add probe-marker.txt
git -c core.autocrlf=false -c user.name=probe -c user.email=probe@example.invalid commit -q -m "control: a plain fast-forward push" || fail "control commit failed."
run git push origin "refs/heads/local/${PROBE_REF}:refs/heads/${PROBE_REF}"
if [ "$?" != "0" ]; then
  stop "CONTROL 1 failed: an ordinary push was refused on an UNPROTECTED branch.
That means a refusal in step 4 would prove nothing. Stopping."
fi
record "control 1  plain push to unprotected stand-in      -> SUCCEEDED (expected)"

# ------------------------------------------------------------------- 3. protect
echo "--------------------------------------------------------------"
echo "3. APPLY THE RECORDED RULE SET TO THE STAND-IN"
echo "--------------------------------------------------------------"
echo "\$ gh api -X PUT $PROTECT_REPO/branches/$PROBE_REF/protection --input $RECORD"
gh_api -X PUT "$PROTECT_REPO/branches/$PROBE_REF/protection" --input "$RECORD" >/dev/null 2>&1
rc=$?; echo "[exit $rc]"; echo
if [ "$rc" != "0" ]; then stop "could not protect the stand-in; refusing to continue."; fi

echo "Rule set now live on $PROBE_REF:"
gh_api "$PROTECT_REPO/branches/$PROBE_REF/protection" 2>&1 \
  | python -c '
import json, sys
p = json.load(sys.stdin)
for k in ("allow_force_pushes", "allow_deletions", "enforce_admins",
          "required_linear_history", "lock_branch", "block_creations"):
    print("    %-30s %s" % (k, p[k]["enabled"]))
rsc = p.get("required_status_checks")
print("    %-30s %s" % ("required_status_checks",
      ("strict=%s contexts=%s" % (rsc["strict"], rsc["contexts"])) if rsc else None))
' 2>&1
echo

# -------------------------------------------------------------------- 4. probe A
echo "--------------------------------------------------------------"
echo "4. PROBE A -- REAL force-push at the stand-in (expect: REFUSED)"
echo "--------------------------------------------------------------"
echo "A real history rewrite, no --dry-run: the commit is amended so the push"
echo "is genuinely non-fast-forward."
echo
echo "probe-marker" > probe-marker.txt
echo "rewritten" >> probe-marker.txt
git -c core.autocrlf=false add probe-marker.txt
git -c core.autocrlf=false -c user.name=probe -c user.email=probe@example.invalid commit -q --amend -m "control: a plain fast-forward push (rewritten)" \
  || stop "could not rewrite the control commit."
echo "local tip is now $(git rev-parse HEAD), which is NOT a descendant of the remote tip:"
git log --oneline -1 | sed 's/^/    /'
echo
echo "\$ git push --force origin +refs/heads/local/${PROBE_REF}:refs/heads/${PROBE_REF}"
git push --force origin "+refs/heads/local/${PROBE_REF}:refs/heads/${PROBE_REF}" 2>&1 | sed 's/^/    /'
FORCE_RC=${PIPESTATUS[0]}; echo "[exit $FORCE_RC]"; echo
if [ "$FORCE_RC" != "0" ]; then
  record "probe A    --force push to PROTECTED stand-in    -> REFUSED (expected)"
else
  record "probe A    --force push to PROTECTED stand-in    -> SUCCEEDED  <-- UNEXPECTED"
fi

# -------------------------------------------------------------------- 5. probe B
echo "--------------------------------------------------------------"
echo "5. PROBE B -- REAL deletion of the stand-in (expect: REFUSED)"
echo "--------------------------------------------------------------"
echo "\$ git push origin --delete $PROBE_REF"
git push origin --delete "$PROBE_REF" 2>&1 | sed 's/^/    /'
DEL_RC=${PIPESTATUS[0]}; echo "[exit $DEL_RC]"; echo
if [ "$DEL_RC" != "0" ]; then
  record "probe B    delete of PROTECTED stand-in           -> REFUSED (expected)"
else
  record "probe B    delete of PROTECTED stand-in           -> SUCCEEDED  <-- UNEXPECTED"
fi

# ------------------------------------------------------------------- 6. control 2
echo "--------------------------------------------------------------"
echo "6. CONTROL 2 -- delete an UNPROTECTED branch (expect: SUCCEEDS)"
echo "--------------------------------------------------------------"
echo "The stand-in is still protected, so a refusal in step 5 is attributable"
echo "to that. This shows the delete path itself is functional."
echo
echo "\$ git push origin $MAIN_SHA_BEFORE:refs/heads/$CONTROL_REF"
git push origin "$MAIN_SHA_BEFORE:refs/heads/$CONTROL_REF" 2>&1 | sed 's/^/    /'
rc=${PIPESTATUS[0]}; echo "[exit $rc]"; echo
[ "$rc" = "0" ] || stop "could not create the control branch; the delete control is meaningless without it."
echo "\$ git push origin --delete $CONTROL_REF"
git push origin --delete "$CONTROL_REF" 2>&1 | sed 's/^/    /'
DELCTL_RC=${PIPESTATUS[0]}; echo "[exit $DELCTL_RC]"; echo
if [ "$DELCTL_RC" = "0" ]; then
  record "control 2  delete of UNPROTECTED sibling          -> SUCCEEDED (expected)"
else
  record "control 2  delete of UNPROTECTED sibling          -> REFUSED  <-- UNEXPECTED"
fi

# ---------------------------------------------------------------------- verdict
echo "=============================================================="
echo "VERDICT (cleanup runs after this)"
echo "=============================================================="
printf '%s' "$RESULTS"
echo
if [ "$FORCE_RC" != "0" ] && [ "$DEL_RC" != "0" ] && [ "$DELCTL_RC" = "0" ]; then
  echo "Both refusals landed and both controls behaved. The recorded rule set"
  echo "refuses a force-push and a deletion."
  exit 0
fi
echo "AT LEAST ONE EXPECTATION FAILED -- see the markers above."
exit 1
