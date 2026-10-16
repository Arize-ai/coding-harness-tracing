#!/usr/bin/env bash
# Checks 3 and 4 in one fixed throwaway HOME (recreated per run, so paths in the output match).
#   check34.sh venv  <venv312 dir> <repo>   baseline: `python -m core.setup.status` and the repo's
#                                           install.py run as a file, the way install.sh does
#   check34.sh probe <probe exe>            a probe build (build-probe.sh): `<exe> status`, `<exe> setup ...`
# Prints status (human + --json) and exit codes, each install's output, and the files it left in HOME.
set -uo pipefail
MODE=$1
H=/tmp/arize-check34-home
fresh() {
  rm -rf "$H"; mkdir -p "$H/.arize/harness" "$H/.config/opencode" "$H/.omp" "$H/.devin" "$H/project"
  cat > "$H/.arize/harness/config.json" <<'EOF'
{"user_id": "check34", "logging": {"prompts": true, "tool_details": true, "tool_content": true},
 "harnesses": {
  "claude-code": {"target": "arize", "endpoint": "otlp.arize.com:443", "api_key": "fake", "space_id": "fake", "project_name": "p"},
  "opencode": {"target": "phoenix", "endpoint": "http://localhost:6006", "project_name": "oc"},
  "omp": {"target": "phoenix", "endpoint": "http://localhost:6006", "project_name": "omp"},
  "devin": {"target": "phoenix", "endpoint": "http://localhost:6006", "project_name": "dv"}}}
EOF
}
run() { (cd "$H/project" && env -i HOME="$H" PATH=/usr/bin:/bin TERM=dumb ARIZE_NONINTERACTIVE=true "$@" 2>&1 </dev/null); echo "  [exit $?]"; }
if [ "$MODE" = venv ]; then
  VPY=$2/bin/python REPO=$3
  status() { run "$VPY" -m core.setup.status "$@"; }
  setup() { local h=$1; shift; run "$VPY" "$REPO/tracing/$h/install.py" "$@"; }
else
  EXE=$2
  status() { run "$EXE" status "$@"; }
  setup() { local h=$1; shift; run "$EXE" setup "$h" "$@"; }
fi

fresh
echo "## status"; status
echo "## status --json"; status --json
for h in opencode omp devin; do
  echo "## setup $h install --with-skills"; setup "$h" install --with-skills
done
echo "## files left in HOME (sha of contents)"
(cd "$H" && find . -type f -o -type l | LC_ALL=C sort | while read -r f; do
  if [ -L "$f" ]; then echo "$f -> $(readlink "$f")"; else echo "$f $(shasum "$f" | cut -c1-12)"; fi
done)
echo "## status after setup"; status
