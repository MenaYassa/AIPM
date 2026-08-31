"""Shot 25A — identity-setup idempotency and fail-closed semantics.

Exercises ops/setup-aipm-identity.sh IDENTITY-stage behavior in a fixture
sandbox (non-root, stubbed getent/id/useradd backed by a mutable state file),
proving the required semantics:

  S1  fresh creation           (no groups, no users -> groupadd + useradd --gid)
  S2  pre-existing groups + missing users (groups reused; useradd --gid, NOT
      --user-group — the exact Checkpoint-1 failure mode)
  S3  already-existing users with correct primary group + shell (no recreation)
  S4  repeated/idempotent execution (no useradd/groupadd/usermod on re-run)
  S5  wrong primary group on an existing user -> fail closed, no recreation
  S6  privileged group contamination -> fail closed, non-zero exit
  S7  wrong shell -> fail closed, non-zero exit
  S8  failure propagation (useradd failure -> non-zero exit + stage report;
      dry-run never mutates)

The stub system starts EMPTY (GETENT_DB starts as a bare header), so each
scenario constructs the precise pre-state it needs.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "ops" / "setup-aipm-identity.sh"

SUDOERS_RULE = "aipm-executor ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"

# ---------------------------------------------------------------------------
# Stub command implementations. State lives in TWO flat files:
#   $GETENT_DB.group  — group lines  (name:x:gid:members)
#   $GETENT_DB.passwd — passwd lines (name:x:uid:gid::home:shell)
# Flat files (no section markers) so appends can never leak across namespaces.
# ---------------------------------------------------------------------------

STUB_GETENT = r"""#!/bin/bash
# getent stub backed by $GETENT_DB.{group,passwd}
case "$1" in
  passwd)
    DB="${GETENT_DB:?}.passwd"
    if [ -z "$2" ]; then
      [ -f "$DB" ] && cat "$DB"
      exit 0
    fi
    line="$(awk -F: -v u="$2" '$1==u{print;exit}' "$DB" 2>/dev/null)"
    [ -n "$line" ] && { echo "$line"; exit 0; }
    exit 2 ;;
  group)
    DB="${GETENT_DB:?}.group"
    if [ -z "$2" ]; then
      [ -f "$DB" ] && cat "$DB"
      exit 0
    fi
    line="$(awk -F: -v k="$2" '$1==k || $3==k{print;exit}' "$DB" 2>/dev/null)"
    [ -n "$line" ] && { echo "$line"; exit 0; }
    exit 2 ;;
esac
exit 2
"""

STUB_GROUPADD = r"""#!/bin/bash
DB="${GETENT_DB:?}.group"
echo "groupadd $*" >> "$STUB_LOG/invocations"
# last argument is the group name
name=""
for a in "$@"; do name="$a"; done
if [ -f "$DB" ] && awk -F: -v g="$name" '$1==g{found=1} END{exit found?0:1}' "$DB"; then
  echo "groupadd: group '$name' already exists" >&2
  exit 9
fi
n="$( [ -f "$DB" ] && wc -l < "$DB" || echo 0 )"
printf '%s:x:17%03d:\n' "$name" "$((n + 1))" >> "$DB"
exit 0
"""

STUB_USERADD = r"""#!/bin/bash
GROUPS_DB="${GETENT_DB:?}.group"
USERS_DB="${GETENT_DB:?}.passwd"
echo "useradd $*" >> "$STUB_LOG/invocations"
# parse --gid <group> and the trailing username
gid=""
name=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "--gid" ]; then
    i=$((i+1)); gid="${args[$i]}"
  fi
done
name="${args[${#args[@]}-1]}"
if [ -f "$USERS_DB" ] && awk -F: -v u="$name" '$1==u{found=1} END{exit found?0:1}' "$USERS_DB"; then
  echo "useradd: user '$name' already exists" >&2
  exit 9
fi
if [ -n "$gid" ]; then
  gnum="$(awk -F: -v g="$gid" '$1==g{print $3; exit}' "$GROUPS_DB" 2>/dev/null)"
  if [ -z "$gnum" ]; then
    echo "useradd: group '$gid' does not exist" >&2
    exit 6
  fi
else
  gnum="16099"
fi
n="$( [ -f "$USERS_DB" ] && wc -l < "$USERS_DB" || echo 0 )"
uid=$((21000 + n + 1))
printf '%s:x:%d:%s::/var/lib/%s:/usr/sbin/nologin\n' "$name" "$uid" "$gnum" "$name" >> "$USERS_DB"
exit 0
"""

STUB_USERMOD = r"""#!/bin/bash
GROUPS_DB="${GETENT_DB:?}.group"
echo "usermod $*" >> "$STUB_LOG/invocations"
# usermod -a -G <groups> <user>: append the user to each group's member list
user=""
glist=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "-G" ]; then
    i=$((i+1)); glist="${args[$i]}"
  fi
done
user="${args[${#args[@]}-1]}"
OLDIFS="$IFS"; IFS=','
for g in $glist; do
  tmp="$(mktemp)"
  awk -v g="$g" -v u="$user" 'BEGIN { FS = ":"; OFS = ":" }
    $1==g {
      if ($4 == "") $4 = u
      else {
        n = split($4, m, ","); found = 0
        for (i = 1; i <= n; i++) if (m[i] == u) found = 1
        if (!found) $4 = $4 "," u
      }
    }
    { print }
  ' "$GROUPS_DB" > "$tmp"
  cat "$tmp" > "$GROUPS_DB"; rm -f "$tmp"
done
IFS="$OLDIFS"
exit 0
"""


STUB_ID = r"""#!/bin/bash
# id stub backed by $GETENT_DB.{passwd,group}
GROUPS_DB="${GETENT_DB:?}.group"
USERS_DB="${GETENT_DB:?}.passwd"
case "$1" in
  -u)
    uid="$(awk -F: -v u="$2" '$1==u{print $3; exit}' "$USERS_DB" 2>/dev/null)"
    [ -n "$uid" ] && { echo "$uid"; exit 0; }
    echo "id: '$2': no such user" >&2
    exit 1 ;;
  -Gn)
    uline="$(awk -F: -v u="$2" '$1==u{print; exit}' "$USERS_DB" 2>/dev/null)"
    [ -z "$uline" ] && { echo "id: '$2': no such user" >&2; exit 1; }
    pgid="$(printf '%s' "$uline" | cut -d: -f4)"
    names="$(awk -F: -v g="$pgid" '$3==g{print $1; exit}' "$GROUPS_DB" 2>/dev/null)"
    # supplementary memberships: groups whose member list contains the user
    sup="$(awk -F: -v u="$2" -F: '$4 ~ ("(^|,)" u "(,|$)") && $1 != "" {print $1}' "$GROUPS_DB" 2>/dev/null)"
    # shell ordering: primary group first, then supplementary (dedup)
    out="$names"
    OLDIFS="$IFS"; IFS=','
    for g in $sup; do
      if [ "$g" != "$names" ] && ! printf '%s' "$out" | grep -qw "$g"; then
        out="$out $g"
      fi
    done
    IFS="$OLDIFS"
    printf '%s\n' "$out"
    exit 0 ;;
esac
echo "id: unsupported invocation: $*" >&2
exit 1
"""


STUB_CHOWN = r"""#!/bin/bash
echo "chown $*" >> "$STUB_LOG/invocations"
exit 0
"""

STUB_CHGRP = r"""#!/bin/bash
echo "chgrp $*" >> "$STUB_LOG/invocations"
exit 0
"""

STUB_VISUDO = r"""#!/bin/bash
if [ "$1" = "-cf" ] && [ -n "$2" ]; then
  content="$(cat "$2" 2>/dev/null)"
  if [ "$content" = "aipm-executor ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service" ]; then
    exit 0
  fi
  exit 1
fi
if [ "$1" = "-c" ]; then
  exit 0
fi
exit 1
"""


def _makedirs(root: Path) -> None:
    (root / "src" / "aipm").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "src" / "aipm" / "__init__.py").write_text("# app\n")
    (root / "config" / "aipm.yaml").write_text("key: value\n")
    vbin = root / ".venv" / "bin"
    vbin.mkdir(parents=True)
    (vbin / "python").write_text("#!/bin/sh\n")
    (vbin / "aipm").write_text("#!/bin/sh\n")


@pytest.fixture()
def sandbox(tmp_path: Path):
    root = tmp_path / "sandbox"
    bin_dir = root / "bin"; bin_dir.mkdir(parents=True)
    sudoers_dir = root / "sudoers.d"; sudoers_dir.mkdir(parents=True)
    app_code = root / "aipm"
    _makedirs(app_code)
    log_dir = root / "stubs"
    log_dir.mkdir()
    (log_dir / "invocations").write_text("")

    # Empty system state: no groups, no users (two flat namespace files).
    db = root / "getent_db"

    stubs = {
        "getent": STUB_GETENT,
        "groupadd": STUB_GROUPADD,
        "useradd": STUB_USERADD,
        "usermod": STUB_USERMOD,
        "id": STUB_ID,
        "chown": STUB_CHOWN,
        "chgrp": STUB_CHGRP,
        "visudo": STUB_VISUDO,
    }
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text(body.replace("$STUB_LOG", str(log_dir)))
        p.chmod(0o755)

    # Real groupadd/useradd never run (stubbed); sed/awk/sh come from /usr/bin.
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AIPM_TEST_ALLOW_NON_ROOT": "1",
        "AIPM_APP_CODE": str(app_code),
        "AIPM_HOME": str(root / "var" / "lib" / "aipm"),
        "AIPM_EXECUTOR_HOME": str(root / "var" / "lib" / "aipm-executor"),
        "AIPM_SUDOERS_DIR": str(sudoers_dir),
        "AIPM_SUDOERS_VALIDATE": "visudo",
        "GETENT_DB": str(db),
        "STUB_LOG": str(log_dir),
    }
    return {
        "root": root, "bin": bin_dir, "sudoers": sudoers_dir, "app": app_code,
        "stubs": log_dir, "env": env, "db": db,
    }


def run_script(script: Path, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        env=env, capture_output=True, text=True, timeout=120,
    )


def invocations(sandbox) -> list[str]:
    return (sandbox["stubs"] / "invocations").read_text().splitlines()


def users(sandbox) -> dict[str, str]:
    """user -> gid (3rd passwd field) from the stub DB."""
    out: dict[str, str] = {}
    for line in subprocess.run(
        ["getent", "passwd"], env=sandbox["env"], capture_output=True, text=True, check=False
    ).stdout.splitlines():
        f = line.split(":")
        if len(f) >= 4:
            out[f[0]] = f[3]
    return out


def gid_of(sandbox, group: str) -> str:
    r = subprocess.run(
        ["getent", "group", group], env=sandbox["env"], capture_output=True, text=True, check=False
    )
    return r.stdout.split(":")[2] if r.returncode == 0 else ""


def add_group(sandbox, name: str, members: str = "") -> str:
    """Seed a pre-existing group with a fixed GID; returns GID."""
    gids = {"aipm": 20001, "aipm-executor": 20002, "aipm-runtime": 20003,
            "wronggroup": 20004, "x-exec": 20005}
    gid = gids.get(name, str(20999))
    with open(f"{sandbox['db']}.group", "a") as fh:
        fh.write(f"{name}:x:{gid}:{members}\n")
    return gid


def add_user(sandbox, name: str, gid: str, shell: str = "/usr/sbin/nologin",
             home: str | None = None) -> None:
    if home is None:
        home = f"/var/lib/{name}"
    with open(f"{sandbox['db']}.passwd", "a") as fh:
        fh.write(f"{name}:x:21001:{gid}::{home}:{shell}\n")


# ---------------------------------------------------------------------------
# S1 — fresh creation
# ---------------------------------------------------------------------------

def test_s1_fresh_creation_uses_explicit_gid(sandbox):
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode == 0, result.stderr
    log = "\n".join(invocations(sandbox))
    # groups created, users created with EXPLICIT --gid (never --user-group)
    assert "groupadd --system aipm" in log
    assert "groupadd --system aipm-executor" in log
    assert "groupadd --system aipm-runtime" in log
    assert "useradd" in log
    assert "--user-group" not in log
    aipm_line = [l for l in invocations(sandbox) if l.startswith("useradd ") and l.rstrip().endswith(" aipm")]
    exec_line = [l for l in invocations(sandbox) if l.startswith("useradd ") and l.rstrip().endswith(" aipm-executor")]
    assert aipm_line and "--gid aipm" in aipm_line[0]
    assert exec_line and "--gid aipm-executor" in exec_line[0]
    # primary groups actually recorded
    gid_aipm = gid_of(sandbox, "aipm")
    gid_exec = gid_of(sandbox, "aipm-executor")
    u = users(sandbox)
    assert u.get("aipm") == gid_aipm
    assert u.get("aipm-executor") == gid_exec
    # nologin shells
    for name in ("aipm", "aipm-executor"):
        line = subprocess.run(["getent", "passwd", name], env=sandbox["env"],
                              capture_output=True, text=True, check=False).stdout
        assert line.rstrip().endswith("/usr/sbin/nologin")
    assert "STOP" not in result.stderr


def test_s1_dry_run_never_mutates_identity_state(sandbox):
    result = run_script(SETUP, "--dry-run", env=sandbox["env"])
    assert result.returncode == 0, result.stderr
    assert invocations(sandbox) == []
    assert users(sandbox) == {}
    assert "[DRY RUN] useradd --system --home-dir" in result.stdout
    assert "--gid aipm" in result.stdout


# ---------------------------------------------------------------------------
# S2 — pre-existing groups + missing users (the Checkpoint-1 scenario)
# ---------------------------------------------------------------------------

def test_s2_preexisting_groups_missing_users_reuses_groups(sandbox):
    g_aipm = add_group(sandbox, "aipm")
    g_exec = add_group(sandbox, "aipm-executor")
    g_run = add_group(sandbox, "aipm-runtime")
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode == 0, result.stderr
    log = "\n".join(invocations(sandbox))
    assert "groupadd" not in log, "existing groups must be REUSED, not recreated"
    assert "reusing it" in result.stdout
    # users created with explicit --gid against the EXISTING groups
    assert "useradd" in log
    assert "--user-group" not in log
    aipm_line = [l for l in invocations(sandbox) if l.startswith("useradd ") and l.rstrip().endswith(" aipm")]
    exec_line = [l for l in invocations(sandbox) if l.startswith("useradd ") and l.rstrip().endswith(" aipm-executor")]
    assert aipm_line and "--gid aipm" in aipm_line[0]
    assert exec_line and "--gid aipm-executor" in exec_line[0]
    # primary GIDs match the pre-existing groups exactly
    u = users(sandbox)
    assert u.get("aipm") == str(g_aipm)
    assert u.get("aipm-executor") == str(g_exec)
    # memberships applied
    assert "usermod" in log


# ---------------------------------------------------------------------------
# S3 — already-existing users (correct state) are verified, not recreated
# ---------------------------------------------------------------------------

def test_s3_existing_users_verified_not_recreated(sandbox):
    g_aipm = add_group(sandbox, "aipm")
    g_exec = add_group(sandbox, "aipm-executor")
    g_run = add_group(sandbox, "aipm-runtime")
    add_user(sandbox, "aipm", g_aipm)
    add_user(sandbox, "aipm-executor", g_exec)
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode == 0, result.stderr
    log = "\n".join(invocations(sandbox))
    assert "useradd" not in log
    assert "primary group 'aipm', shell OK" in result.stdout
    # memberships still enforced idempotently (no-op when already member)
    # via usermod only if missing — with seeded memberships there should be none


def test_s3b_idempotent_rerun_creates_nothing(sandbox):
    assert run_script(SETUP, "--apply", env=sandbox["env"]).returncode == 0
    log_after_first = (sandbox["stubs"] / "invocations").read_text()
    second = run_script(SETUP, "--apply", env=sandbox["env"])
    assert second.returncode == 0, second.stderr
    new_lines = (sandbox["stubs"] / "invocations").read_text()[len(log_after_first):]
    adds = [l for l in new_lines.splitlines() if l.startswith(("useradd", "groupadd", "usermod"))]
    assert adds == [], f"second run re-created identities: {adds}"


# ---------------------------------------------------------------------------
# S5 — wrong primary group on an existing user fails closed
# ---------------------------------------------------------------------------

def test_s5_wrong_primary_group_fails_closed(sandbox):
    g_aipm = add_group(sandbox, "aipm")
    add_group(sandbox, "aipm-executor")
    add_group(sandbox, "aipm-runtime")
    other = add_group(sandbox, "wronggroup")
    add_user(sandbox, "aipm", other)          # primary group is WRONG
    add_user(sandbox, "aipm-executor", add_group(sandbox, "x-exec"))  # executor wrong too
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode != 0
    assert "STOP" in result.stderr
    assert "primary group 'wronggroup', expected 'aipm'" in result.stderr
    log = "\n".join(invocations(sandbox))
    assert "useradd" not in log, "must never recreate an existing user"


def test_s5b_executor_wrong_primary_group_fails_closed(sandbox):
    add_group(sandbox, "aipm")
    g_exec = add_group(sandbox, "aipm-executor")
    add_group(sandbox, "aipm-runtime")
    other = add_group(sandbox, "wronggroup")
    add_user(sandbox, "aipm", add_group(sandbox, "aipm"))
    add_user(sandbox, "aipm-executor", other)  # executor's primary group is WRONG
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode != 0
    assert "STOP" in result.stderr
    assert "user aipm-executor exists with primary group 'wronggroup', expected 'aipm-executor'" in result.stderr
    # the mis-primaried user was NOT recreated with the correct group
    u = users(sandbox)
    assert u.get("aipm-executor") == str(other)
    log = "\n".join(invocations(sandbox))
    assert "useradd" not in log


# ---------------------------------------------------------------------------
# S6 — privileged group contamination fails closed
# ---------------------------------------------------------------------------

def test_s6_privileged_group_contamination_fails(sandbox):
    g_aipm = add_group(sandbox, "aipm")
    g_exec = add_group(sandbox, "aipm-executor")
    add_group(sandbox, "aipm-runtime")
    add_user(sandbox, "aipm", g_aipm)
    add_user(sandbox, "aipm-executor", g_exec)
    # Contaminate: put aipm-executor into sudo via a supplementary group entry.
    # The script consults `id -Gn` — stub it via a fake id after the real one.
    fake_id = sandbox["bin"] / "id"
    fake_id.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-Gn" ] && [ "$2" = "aipm-executor" ]; then\n'
        '  echo "aipm-executor sudo aipm-runtime"; exit 0\n'
        "fi\n"
        'exec /usr/bin/id "$@"\n'
    )
    fake_id.chmod(0o755)
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode != 0
    assert "STOP" in result.stderr
    assert "aipm-executor is in a privileged group" in result.stderr


# ---------------------------------------------------------------------------
# S7 — wrong shell fails closed
# ---------------------------------------------------------------------------

def test_s7_wrong_shell_fails_closed(sandbox):
    g_aipm = add_group(sandbox, "aipm")
    g_exec = add_group(sandbox, "aipm-executor")
    add_group(sandbox, "aipm-runtime")
    add_user(sandbox, "aipm", g_aipm)
    add_user(sandbox, "aipm-executor", g_exec, shell="/bin/bash")
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode != 0
    assert "STOP" in result.stderr
    assert "shell '/bin/bash', expected '/usr/sbin/nologin'" in result.stderr


# ---------------------------------------------------------------------------
# S8 — failure propagation: useradd failure must exit non-zero with stage report
# ---------------------------------------------------------------------------

def test_s8_useradd_failure_propagates_nonzero(sandbox):
    # Replace the useradd stub so it exits 1 for aipm
    ua = sandbox["bin"] / "useradd"
    ua.write_text(
        "#!/bin/bash\n"
        'echo "useradd $*" >> "$STUB_LOG/invocations"\n'
        'for a in "$@"; do last="$a"; done\n'
        'if [ "$last" = "aipm" ]; then echo "simulated useradd failure" >&2; exit 1; fi\n'
        'exit 0\n'
    )
    ua.chmod(0o755)
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode != 0
    assert "STOP" in result.stderr
    assert "useradd failed for aipm" in result.stderr


def test_s8b_no_unconditional_success_after_failure(sandbox):
    ua = sandbox["bin"] / "useradd"
    ua.write_text(
        "#!/bin/bash\n"
        'echo "useradd $*" >> "$STUB_LOG/invocations"\n'
        'for a in "$@"; do last="$a"; done\n'
        'if [ "$last" = "aipm" ]; then echo "simulated useradd failure" >&2; exit 1; fi\n'
        'exit 0\n'
    )
    ua.chmod(0o755)
    result = run_script(SETUP, "--apply", env=sandbox["env"])
    assert result.returncode != 0
    assert "IDENTITY SETUP COMPLETE" not in result.stdout


# ---------------------------------------------------------------------------
# S4 — repeated/idempotent execution (full-loop, three consecutive applies)
# ---------------------------------------------------------------------------

def test_s4_three_consecutive_applies_idempotent(sandbox):
    for i in (1, 2, 3):
        result = run_script(SETUP, "--apply", env=sandbox["env"])
        assert result.returncode == 0, result.stderr
    log = "\n".join(invocations(sandbox))
    assert log.count("useradd ") == 2, "exactly two useradd calls across three applies"
    assert log.count("groupadd --system") == 3, "exactly three groupadd calls across three applies"
