#!/usr/bin/env bash
# tpot-expansion-pack.sh - set up the Sensor Deployer and plug it into a T-Pot Hive's own nginx.
#
# Deployer setup
#   1. Creates .env from .env.example if there is none, filling in what it can work out (UID/GID,
#      the Docker bridge IP, defaults) and prompting for the rest (T-Pot path, firewall IP for the
#      EDL). On later runs it only prompts for keys missing from .env.
#   2. Creates the secrets: secrets/do_token (asks for the DigitalOcean token, or moves an old
#      DO_TOKEN out of .env), secrets/ssh_key + ssh_key.pub (copies a key from ~/.ssh or generates
#      an ed25519 key) and secrets/gcp-sa.json (copies a GCP service account key you point it at).
#      Sets owner-only permissions on .env, secrets/ and everything secret in it.
#
# T-Pot nginx
#   3. Builds $TPOT_DATA_PATH/nginx/conf/tpotweb.conf from the stock copy in the installed T-Pot
#      nginx image plus the /sensors/ and /edl/ routes in nginx/tpot-location.conf.
#   4. Builds $TPOT_DATA_PATH/nginx/conf/index.html (the Hive landing page) from the stock copy,
#      with a "Sensor Deployer" link added to the tools box.
#   5. Mounts both files into T-Pot's nginx service in tpotce/docker-compose.yml.
#   6. Sets WEB_BIND in .env to the host's Docker bridge IP, the address T-Pot's nginx container
#      can reach the deployer on (127.0.0.1 inside that container is nginx itself).
#   7. Validates the new config in a throwaway nginx container, then starts or restarts the
#      deployer as needed, recreates or reloads nginx, and checks the routes.
#
# Safe to re-run; re-run it after a T-Pot update, since that can replace docker-compose.yml or
# ship a new stock tpotweb.conf / landing page. Anything it changes is backed up as *.bak-<time>.
#
# Usage: ./tpot-expansion-pack.sh [--dry-run] [--setup] [--no-prompt]
#   --dry-run    show what would change, change nothing (never prompts)
#   --setup      ask the optional questions again (DigitalOcean token, EDL firewall IPs, GCP key)
#   --no-prompt  never prompt: use defaults, and fail if a required value is missing

set -euo pipefail

myREPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
myENV="$myREPO/.env"
myEXAMPLE="$myREPO/.env.example"
mySNIPPET="$myREPO/nginx/tpot-location.conf"
myNGINX="nginx"
myLINK='<a href="/sensors/" class="link" target="_blank">Sensor Deployer</a>'
myDRYRUN=false
mySETUP=false
myPROMPT=true
mySTAMP="$(date +%Y%m%d-%H%M%S)"
myBACKUPS=()

for myARG in "$@"; do
  case "$myARG" in
    --dry-run) myDRYRUN=true ;;
    --setup) mySETUP=true ;;
    --no-prompt) myPROMPT=false ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $myARG (see --help)" >&2; exit 2 ;;
  esac
done
# Prompts need a terminal; a dry run never asks.
{ $myDRYRUN || ! [ -t 0 ]; } && myPROMPT=false

fuINFO() { echo "[*] $*"; }
fuWARN() { echo "[!] $*" >&2; }
fuDIE()  { echo "[x] $*" >&2; exit 1; }

# Last value of KEY in an env file, without surrounding quotes.
fuENVGET() {
  local myVAL
  myVAL="$(grep -E "^$1=" "$2" 2>/dev/null | tail -n 1 | cut -d= -f2-)" || true
  myVAL="${myVAL%\"}"; myVAL="${myVAL#\"}"; myVAL="${myVAL%\'}"; myVAL="${myVAL#\'}"
  echo "$myVAL"
}

# Set KEY=VALUE in an env file: replace the first KEY= line, or append one.
fuENVSET() {
  local myKEY="$1" myFILE="$3"
  if grep -qE "^$myKEY=" "$myFILE"; then
    myVAL="$2" awk -v k="$myKEY" '
      !done && index($0, k "=") == 1 { print k "=" ENVIRON["myVAL"]; done = 1; next }
      { print }
    ' "$myFILE" > "$myFILE.new"
    cat "$myFILE.new" > "$myFILE"; rm -f "$myFILE.new"
  else
    printf '%s=%s\n' "$myKEY" "$2" >> "$myFILE"
  fi
}

# fuASK "question" [default] [secret] - read an answer from the terminal (default on Enter).
fuASK() {
  local myANS
  if [ "${3:-}" = "secret" ]; then
    read -r -s -p "    $1: " myANS < /dev/tty; echo >&2
  else
    read -r -e -p "    $1${2:+ [$2]}: " myANS < /dev/tty
  fi
  echo "${myANS:-${2:-}}"
}

# Back up $1 once per run, then overwrite it in place with $2 (mode $3, default 644). Writing in
# place (not mv) keeps the inode, so a file already bind-mounted into a container sees the change.
fuINSTALL() {
  local myDST="$1" mySRC="$2" myMODE="${3:-644}"
  if [ -f "$myDST" ] && cmp -s "$myDST" "$mySRC"; then
    fuINFO "unchanged: $myDST"
    return 1
  fi
  if $myDRYRUN; then
    fuINFO "would update: $myDST"
    if [ -f "$myDST" ]; then diff -u "$myDST" "$mySRC" | sed -E 's/^([+-][A-Z_]*TOKEN=).+/\1<hidden>/' || true
    else echo "    (new file)"; fi
    return 0
  fi
  if [ -f "$myDST" ]; then
    cp -a "$myDST" "$myDST.bak-$mySTAMP"
    chmod "$myMODE" "$myDST.bak-$mySTAMP"
    myBACKUPS+=("$myDST")
  fi
  cat "$mySRC" > "$myDST"
  chmod "$myMODE" "$myDST"
  fuINFO "updated: $myDST"
  return 0
}

fuROLLBACK() {
  local myFILE
  fuWARN "rolling back"
  for myFILE in "${myBACKUPS[@]}"; do
    cat "$myFILE.bak-$mySTAMP" > "$myFILE"
    fuWARN "restored $myFILE"
  done
}

command -v docker >/dev/null || fuDIE "docker not found"
[ -f "$myEXAMPLE" ] || fuDIE "$myEXAMPLE not found"
[ -f "$mySNIPPET" ] || fuDIE "$mySNIPPET not found"

myTMP="$(mktemp -d)"
myCID=""
trap 'rm -rf "$myTMP"; [ -z "$myCID" ] || docker rm -f "$myCID" >/dev/null 2>&1 || true' EXIT
umask 077

myBRIDGE="$(docker network inspect bridge -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' 2>/dev/null || true)"
[ -n "$myBRIDGE" ] || myBRIDGE="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
[ -n "$myBRIDGE" ] || fuDIE "could not find the Docker bridge (docker0) IP"

# ================================================================================================
# Deployer setup: .env
# ================================================================================================
# All .env edits go to a working copy ($myWENV), installed with the other files at the end.
myWENV="$myTMP/env"
myFRESH=false
if [ -f "$myENV" ]; then
  cp "$myENV" "$myWENV"
else
  cp "$myEXAMPLE" "$myWENV"
  myFRESH=true
  fuINFO "no .env yet: creating one from .env.example"
fi

# Keys to decide: every key on a fresh .env, otherwise only keys missing from .env (new ones in
# .env.example), plus TPOT_HOST_DIR if it's empty and the optional questions with --setup.
myKEYS=()
while IFS= read -r myKEY; do
  if $myFRESH || ! grep -qE "^$myKEY=" "$myENV"; then myKEYS+=("$myKEY"); fi
done < <(grep -oE '^[A-Z_][A-Z0-9_]*=' "$myEXAMPLE" | tr -d = | awk '!seen[$0]++')
[ -n "$(fuENVGET TPOT_HOST_DIR "$myWENV")" ] || myKEYS=(TPOT_HOST_DIR "${myKEYS[@]}")
$mySETUP && myKEYS+=(EDL_ALLOW)

fuWANT() { local k; for k in "${myKEYS[@]}"; do [ "$k" = "$1" ] && return 0; done; return 1; }

if [ ${#myKEYS[@]} -gt 0 ] && $myPROMPT; then
  echo
  fuINFO "Deployer settings (Enter keeps the value in brackets)"
fi

# TPOT_HOST_DIR first: several defaults below derive from it.
if fuWANT TPOT_HOST_DIR; then
  myDEF="$(fuENVGET TPOT_HOST_DIR "$myWENV")"
  [ -n "$myDEF" ] || { [ -f "$HOME/tpotce/docker-compose.yml" ] && myDEF="$HOME/tpotce"; } || true
  while true; do
    if $myPROMPT; then myVAL="$(fuASK "Path to your T-Pot install (tpotce)" "$myDEF")"; else myVAL="$myDEF"; fi
    myVAL="${myVAL/#\~/$HOME}"; myVAL="${myVAL%/}"
    [ -f "$myVAL/docker-compose.yml" ] && break
    $myPROMPT || fuDIE "TPOT_HOST_DIR is not set in .env and no T-Pot install found at ~/tpotce"
    fuWARN "no docker-compose.yml in '$myVAL'; try again"
  done
  fuENVSET TPOT_HOST_DIR "$myVAL" "$myWENV"
fi

myTPOT="$(fuENVGET TPOT_HOST_DIR "$myWENV")"
myTPOT="${myTPOT/#\~/$HOME}"
myCOMPOSE="$myTPOT/docker-compose.yml"
[ -f "$myCOMPOSE" ] || fuDIE "$myCOMPOSE not found (is TPOT_HOST_DIR in .env right?)"
[ "$(fuENVGET TPOT_TYPE "$myTPOT/.env")" = "HIVE" ] || fuWARN "TPOT_TYPE in $myTPOT/.env is not HIVE"
myDATA="$(fuENVGET TPOT_DATA_PATH "$myTPOT/.env")"
myDATA="${myDATA:-./data}"
case "$myDATA" in /*) ;; *) myDATA="$myTPOT/${myDATA#./}" ;; esac
myCONF="$myDATA/nginx/conf"
[ -d "$myCONF" ] || fuDIE "$myCONF not found (has T-Pot been started at least once?)"

for myKEY in "${myKEYS[@]}"; do
  myCUR="$(fuENVGET "$myKEY" "$myWENV")"
  myEXDEF="$(fuENVGET "$myKEY" "$myEXAMPLE")"
  case "$myKEY" in
    TPOT_HOST_DIR) continue ;;
    # The containers run as the owner of the T-Pot directory so Hive files stay writable.
    PUID) myVAL="$(stat -c %u "$myTPOT")" ;;
    PGID) myVAL="$(stat -c %g "$myTPOT")" ;;
    TPOT_GID) myVAL="$(stat -c %g "$myCONF")" ;;
    WEB_BIND) myVAL="$myBRIDGE" ;;
    GCP_PROJECT_ID) continue ;;   # taken from the GCP key below
    EDL_ALLOW)
      myVAL="$myCUR"
      if $myPROMPT; then
        # A Palo Alto polls the EDL with an empty user agent; offer the addresses that already have.
        myGUESS="$(grep -h '/edl/' "$myDATA"/nginx/log/access.log* 2>/dev/null \
          | grep -E '"http_user_agent": *""' | grep -oE '"src_ip": *"[^"]+"' | cut -d'"' -f4 \
          | sort -u | paste -sd, - | sed 's/,/, /g')" || true
        echo "    Firewall address(es) allowed to fetch /edl/sensors.txt, comma-separated. Empty = only this Hive."
        [ -z "$myGUESS" ] || echo "    (seen polling the EDL with an empty user agent, like a Palo Alto: $myGUESS)"
        myVAL="$(fuASK "EDL_ALLOW" "${myCUR:-$myGUESS}")"
      fi
      ;;
    TPOT_HIVE_IP) myVAL="$myCUR" ;;   # blank = auto-detect
    *)
      # Any other key: keep the current value, else the example default; ask about new keys.
      myVAL="${myCUR:-$myEXDEF}"
      if $myPROMPT && ! $myFRESH; then myVAL="$(fuASK "$myKEY (new in .env.example)" "$myVAL")"; fi
      ;;
  esac
  fuENVSET "$myKEY" "$myVAL" "$myWENV"
done

# ================================================================================================
# Deployer setup: secrets
# ================================================================================================
mySECRETS="$(fuENVGET SECRETS_HOST_DIR "$myWENV")"; mySECRETS="${mySECRETS:-./secrets}"
case "$mySECRETS" in /*) ;; *) mySECRETS="$myREPO/${mySECRETS#./}" ;; esac
mySECRETSCHANGED=false
myPUID="$(fuENVGET PUID "$myWENV")"
[ -z "$myPUID" ] || [ "$myPUID" = "$(id -u)" ] || fuWARN "PUID ($myPUID) is not you ($(id -u)): the containers can't read owner-only files you create in $mySECRETS"

# Where a secret is written: straight into secrets/, or nowhere on a dry run.
fuSECRET() { # fuSECRET <name> <source file> <mode>
  if $myDRYRUN; then fuINFO "would create: $mySECRETS/$1"; return; fi
  install -m "$3" "$2" "$mySECRETS/$1"
  mySECRETSCHANGED=true
  fuINFO "created: $mySECRETS/$1"
}

$myDRYRUN || { mkdir -p "$mySECRETS" && chmod 700 "$mySECRETS"; }

# DigitalOcean token: only ever in secrets/do_token. Older setups had DO_TOKEN in .env, which puts
# it in the containers' environment (visible in `docker inspect`), so move it out.
myTOKEN="$mySECRETS/do_token"
myOLDTOKEN="$(fuENVGET DO_TOKEN "$myWENV")"
grep -qE '^DO_TOKEN=' "$myWENV" && { grep -vE '^DO_TOKEN=' "$myWENV" > "$myWENV.new"; cat "$myWENV.new" > "$myWENV"; rm -f "$myWENV.new"; }
myNEWTOKEN=""
if [ -s "$myTOKEN" ]; then
  $myDRYRUN || chmod 600 "$myTOKEN"
  [ -z "$myOLDTOKEN" ] || [ "$myOLDTOKEN" = "$(tr -d '[:space:]' < "$myTOKEN")" ] || fuWARN "DO_TOKEN in .env differs from $myTOKEN; keeping the file and dropping the .env copy"
  if $mySETUP && $myPROMPT; then
    echo "    DigitalOcean API token (read + write). Enter keeps the current one."
    myNEWTOKEN="$(fuASK "New token (hidden)" "" secret)"
  fi
elif [ -n "$myOLDTOKEN" ]; then
  fuINFO "moving DO_TOKEN from .env to $myTOKEN"
  myNEWTOKEN="$myOLDTOKEN"
elif $myPROMPT; then
  echo "    DigitalOcean API token (read + write), saved to $myTOKEN. Enter to skip if you only use GCP."
  myNEWTOKEN="$(fuASK "DO token (hidden)" "" secret)"
fi
if [ -n "$myNEWTOKEN" ]; then
  if command -v curl >/dev/null; then
    myCODE="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $myNEWTOKEN" https://api.digitalocean.com/v2/account || true)"
    [ "$myCODE" = "200" ] && fuINFO "DigitalOcean token works" || fuWARN "DigitalOcean rejected the token (HTTP $myCODE); saving it anyway, replace it with --setup"
  fi
  printf '%s\n' "$myNEWTOKEN" > "$myTMP/do_token"
  fuSECRET do_token "$myTMP/do_token" 600
elif [ ! -s "$myTOKEN" ]; then
  fuINFO "no DigitalOcean token (optional if you only use GCP): re-run with --setup to add one"
fi

# SSH key pair: the private key is what Ansible logs in to sensors with; the public key is
# uploaded to DigitalOcean and put in GCP instance metadata.
myKEY="$mySECRETS/ssh_key"
if [ -f "$myKEY" ]; then
  ssh-keygen -y -P '' -f "$myKEY" > "$myTMP/pub" 2>/dev/null || fuDIE "$myKEY is passphrase-protected or not a private key (Ansible needs one without a passphrase)"
  if [ ! -f "$myKEY.pub" ]; then
    fuSECRET ssh_key.pub "$myTMP/pub" 644
  elif [ "$(cut -d' ' -f1-2 "$myTMP/pub")" != "$(cut -d' ' -f1-2 "$myKEY.pub")" ]; then
    fuDIE "$myKEY.pub does not belong to $myKEY; delete the wrong one and re-run"
  fi
elif [ -f "$myKEY.pub" ]; then
  fuDIE "$myKEY.pub exists without its private key $myKEY; add it or delete the .pub and re-run"
else
  # Offer the usable keys in ~/.ssh (private keys without a passphrase, which Ansible needs);
  # with none, or no terminal to ask on, generate one.
  mySRC=""
  myFOUND=()
  for myCAND in "$HOME"/.ssh/*; do
    [ -f "$myCAND" ] || continue
    case "$myCAND" in *.pub|*/known_hosts*|*/authorized_keys*|*/config) continue ;; esac
    ssh-keygen -y -P '' -f "$myCAND" >/dev/null 2>&1 && myFOUND+=("$myCAND")
  done
  if $myPROMPT && [ ${#myFOUND[@]} -gt 0 ]; then
    echo "    No SSH key for the deployer yet. It logs in to sensors with it. Keys found in ~/.ssh:"
    for i in "${!myFOUND[@]}"; do
      echo "      $((i + 1))) ${myFOUND[$i]}  ($(ssh-keygen -l -f "${myFOUND[$i]}" 2>/dev/null | awk '{print $NF, $2}'))"
    done
    echo "      g) generate a new ed25519 key for the deployer"
    echo "      p) enter another path"
    while true; do
      myANS="$(fuASK "Copy which key" "1")"
      case "$myANS" in
        g|G) mySRC=""; break ;;
        p|P)
          mySRC="$(fuASK "Path to a private key without a passphrase" "")"; mySRC="${mySRC/#\~/$HOME}"
          [ -n "$mySRC" ] && ssh-keygen -y -P '' -f "$mySRC" >/dev/null 2>&1 && break
          fuWARN "'$mySRC' isn't a private key without a passphrase; try again" ;;
        *)
          if [[ "$myANS" =~ ^[0-9]+$ ]] && [ "$myANS" -ge 1 ] && [ "$myANS" -le ${#myFOUND[@]} ]; then
            mySRC="${myFOUND[$((myANS - 1))]}"; break
          fi
          fuWARN "pick 1-${#myFOUND[@]}, g or p" ;;
      esac
    done
  elif [ ${#myFOUND[@]} -eq 0 ]; then
    fuINFO "no usable key in ~/.ssh; generating one for the deployer"
  fi
  [ -z "$mySRC" ] || ssh-keygen -y -P '' -f "$mySRC" > "$myTMP/pub"
  if [ -n "$mySRC" ]; then
    cp "$mySRC" "$myTMP/key"
  else
    ssh-keygen -q -t ed25519 -N '' -C "tpot-sensor-deployer@$(hostname -s)" -f "$myTMP/key"
    mv "$myTMP/key.pub" "$myTMP/pub"
  fi
  fuSECRET ssh_key "$myTMP/key" 600
  fuSECRET ssh_key.pub "$myTMP/pub" 644
fi
if [ -f "$myKEY" ] && ! $myDRYRUN; then
  chmod 600 "$myKEY"; chmod 644 "$myKEY.pub"
  fuINFO "SSH key: $(ssh-keygen -l -f "$myKEY.pub")"
fi

# GCP service account key (optional). Asked for as a file path rather than pasted: the JSON
# holds a private key with embedded \n escapes that terminal pastes mangle easily.
myGCP="$mySECRETS/gcp-sa.json"
fuGCPCHECK() { # prints the project id of a valid service account key, fails otherwise
  python3 - "$1" <<'EOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if d.get("type") != "service_account" or not all(d.get(k) for k in ("project_id", "private_key", "client_email")):
    sys.exit(1)
print(d["project_id"])
EOF
}
if [ -f "$myGCP" ]; then
  myPROJECT="$(fuGCPCHECK "$myGCP")" || fuDIE "$myGCP is not a GCP service account key"
  $myDRYRUN || chmod 600 "$myGCP"
elif $myPROMPT && { $myFRESH || $mySETUP; }; then
  echo "    GCP service account key (JSON, Compute Admin role). Enter to skip if you only use DigitalOcean."
  while true; do
    mySRC="$(fuASK "Path to the downloaded key file" "")"
    mySRC="${mySRC/#\~/$HOME}"
    [ -z "$mySRC" ] && break
    myPROJECT="$(fuGCPCHECK "$mySRC")" && { fuSECRET gcp-sa.json "$mySRC" 600; break; }
    fuWARN "'$mySRC' isn't a GCP service account key file; try again"
  done
fi
if [ -n "${myPROJECT:-}" ] && [ -z "$(fuENVGET GCP_PROJECT_ID "$myWENV")" ]; then
  fuENVSET GCP_PROJECT_ID "$myPROJECT" "$myWENV"
  fuINFO "GCP project: $myPROJECT (from the key file)"
fi
[ -f "$myGCP" ] || [ -n "${myPROJECT:-}" ] || fuINFO "GCP not set up (optional): re-run with --setup to add a key"

# ================================================================================================
# T-Pot nginx
# ================================================================================================
$myDRYRUN || [ -w "$myCONF" ] || fuDIE "$myCONF is not writable by $(id -un) (add yourself to the tpot group)"
myWEBPORT="$(fuENVGET WEB_PORT "$myWENV")"; myWEBPORT="${myWEBPORT:-8880}"
myEDLALLOW="$(fuENVGET EDL_ALLOW "$myWENV" | tr ',' ' ')"

docker inspect "$myNGINX" >/dev/null 2>&1 || fuDIE "no '$myNGINX' container; start T-Pot first (systemctl start tpot)"
myIMAGE="$(docker inspect -f '{{.Config.Image}}' "$myNGINX")"

echo
fuINFO "T-Pot:  $myTPOT (nginx image $myIMAGE)"
fuINFO "Deployer upstream for T-Pot nginx: http://$myBRIDGE:$myWEBPORT"

# Stock files from the installed image (not the running container, which may already have our
# files mounted over them).
myCID="$(docker create "$myIMAGE")"
docker cp -q "$myCID:/etc/nginx/conf.d/tpotweb.conf" "$myTMP/tpotweb.stock"
docker cp -q "$myCID:/var/lib/nginx/html/index.html" "$myTMP/index.stock"
docker rm -f "$myCID" >/dev/null; myCID=""

# --- tpotweb.conf ------------------------------------------------------------------------------
[ "$(grep -c '^server' "$myTMP/tpotweb.stock")" = "1" ] || fuDIE "stock tpotweb.conf no longer has exactly one server block; update this script"
grep -q 'listen 64297' "$myTMP/tpotweb.stock" || fuDIE "stock tpotweb.conf does not listen on 64297; update this script"

# Snippet body (header comments dropped), proxied to the bridge IP, EDL allow list filled in.
awk -v allow="$myEDLALLOW" '
  !body && (/^#/ || /^[[:space:]]*$/) { next }
  { body = 1 }
  /# EDL_ALLOW[[:space:]]*$/ {
    if (allow == "") { print; next }
    n = split(allow, a, /[[:space:]]+/)
    for (i = 1; i <= n; i++) if (a[i] != "") printf "        allow %s;\n", a[i]
    next
  }
  { print }
' "$mySNIPPET" | sed -E "s#proxy_pass http://[^/;]+/#proxy_pass http://$myBRIDGE:$myWEBPORT/#" > "$myTMP/snippet"

myLAST="$(grep -n '^}' "$myTMP/tpotweb.stock" | tail -n 1 | cut -d: -f1)"
{
  head -n $((myLAST - 1)) "$myTMP/tpotweb.stock"
  echo
  echo "    ### T-Pot Sensor Deployer: generated by tpot-expansion-pack.sh from nginx/tpot-location.conf"
  cat "$myTMP/snippet"
  tail -n +"$myLAST" "$myTMP/tpotweb.stock"
} > "$myTMP/tpotweb.conf"

# --- Landing page ------------------------------------------------------------------------------
if grep -q 'href="/sensors/"' "$myTMP/index.stock"; then
  cp "$myTMP/index.stock" "$myTMP/index.html"
else
  # Add the link as the last entry of the tools box, indented like the links above it.
  awk -v link="$myLINK" '
    /class="[^"]*tools-box/ { inbox = 1 }
    inbox && /class="link"/ { match($0, /^[[:space:]]*/); indent = substr($0, 1, RLENGTH) }
    inbox && /<\/div>/ { print indent link; inbox = 0; done = 1 }
    { print }
    END { if (!done) exit 1 }
  ' "$myTMP/index.stock" > "$myTMP/index.html" || fuDIE "no tools box found in the stock landing page; update this script"
fi

# --- Compose mounts ----------------------------------------------------------------------------
myANCHOR='/etc/nginx/lswebpasswd:ro'
myMOUNTS=(
  '     - ${TPOT_DATA_PATH}/nginx/conf/tpotweb.conf:/etc/nginx/conf.d/tpotweb.conf:ro'
  '     - ${TPOT_DATA_PATH}/nginx/conf/index.html:/var/lib/nginx/html/index.html:ro'
)
cp "$myCOMPOSE" "$myTMP/compose.yml"
for myMOUNT in "${myMOUNTS[@]}"; do
  grep -qF -- "$myMOUNT" "$myTMP/compose.yml" && continue
  grep -qF -- "$myANCHOR" "$myTMP/compose.yml" || fuDIE "no '$myANCHOR' mount in $myCOMPOSE to anchor on; add the nginx mounts by hand"
  awk -v anchor="$myANCHOR" -v mount="$myMOUNT" '
    { print }
    !done && index($0, anchor) { print mount; done = 1 }
  ' "$myTMP/compose.yml" > "$myTMP/compose.new" && mv "$myTMP/compose.new" "$myTMP/compose.yml"
done

# --- WEB_BIND ----------------------------------------------------------------------------------
fuENVSET WEB_BIND "$myBRIDGE" "$myWENV"

# ================================================================================================
# Validate before touching anything
# ================================================================================================
fuINFO "testing generated nginx config"
if ! myTEST="$(docker run --rm --entrypoint nginx \
      -v "$myTMP/tpotweb.conf:/etc/nginx/conf.d/tpotweb.conf:ro" \
      -v "$myDATA/nginx/cert/:/etc/nginx/cert/:ro" \
      -v "$myCONF/nginxpasswd:/etc/nginx/nginxpasswd:ro" \
      -v "$myCONF/lswebpasswd:/etc/nginx/lswebpasswd:ro" \
      "$myIMAGE" -t 2>&1)"; then
  echo "$myTEST" >&2
  fuDIE "generated tpotweb.conf fails nginx -t; nothing was changed"
fi

# ================================================================================================
# Apply
# ================================================================================================
myCONFCHANGED=false; myCOMPOSECHANGED=false; myENVCHANGED=false
fuINSTALL "$myCONF/tpotweb.conf" "$myTMP/tpotweb.conf" && myCONFCHANGED=true
fuINSTALL "$myCONF/index.html"   "$myTMP/index.html" || true   # served per request, no reload
fuINSTALL "$myCOMPOSE"           "$myTMP/compose.yml" && myCOMPOSECHANGED=true
fuINSTALL "$myENV"               "$myWENV" 600 && myENVCHANGED=true
$myDRYRUN || chmod 600 "$myENV"

if [ -z "$myEDLALLOW" ]; then
  fuWARN "EDL_ALLOW is empty in .env, so nothing outside the Hive can fetch /edl/sensors.txt."
  fuWARN "Set it to your firewall's address(es), or re-run with --setup."
fi

if $myDRYRUN; then
  fuINFO "dry run: nothing changed"
  exit 0
fi

# --- Deployer ----------------------------------------------------------------------------------
if ! docker inspect tpot-deployer-web >/dev/null 2>&1; then
  fuINFO "building and starting the deployer"
  (cd "$myREPO" && docker compose up -d --build) || { fuROLLBACK; fuDIE "could not start the deployer"; }
elif $myENVCHANGED || $mySECRETSCHANGED; then
  # Recreating the worker kills an in-flight deploy, so don't while one is running.
  myBUSY="$(cd "$myREPO" && docker compose exec -T redis redis-cli ZCARD rq:wip:tpot-deployer-tasks 2>/dev/null | tr -dc 0-9)" || true
  if [ "${myBUSY:-0}" -gt 0 ]; then
    fuWARN "the worker is running $myBUSY task(s); not restarting it. When it's idle run: docker compose up -d --force-recreate web worker"
  else
    fuINFO "restarting the deployer to pick up the new settings"
    (cd "$myREPO" && docker compose up -d --force-recreate web worker) || { fuROLLBACK; fuDIE "docker compose up failed for the deployer"; }
  fi
fi

# --- T-Pot nginx -------------------------------------------------------------------------------
if $myCOMPOSECHANGED || ! docker inspect -f '{{range .Mounts}}{{.Destination}} {{end}}' "$myNGINX" | grep -q 'conf.d/tpotweb.conf'; then
  fuINFO "recreating T-Pot nginx with the new mounts"
  (cd "$myTPOT" && docker compose -f "$myCOMPOSE" up -d --no-deps "$myNGINX") || { fuROLLBACK; fuDIE "could not recreate T-Pot nginx"; }
elif $myCONFCHANGED; then
  fuINFO "reloading T-Pot nginx"
  docker exec "$myNGINX" nginx -s reload
fi

# ================================================================================================
# Verify
# ================================================================================================
for i in $(seq 1 15); do
  [ "$(docker inspect -f '{{.State.Running}}' "$myNGINX" 2>/dev/null)" = "true" ] && break
  sleep 1
done
docker exec "$myNGINX" nginx -t >/dev/null 2>&1 || { fuROLLBACK; (cd "$myTPOT" && docker compose -f "$myCOMPOSE" up -d --no-deps "$myNGINX"); fuDIE "nginx config check failed in the running container"; }

myOK=true
for i in $(seq 1 20); do
  docker exec "$myNGINX" wget -q -T 5 -O /dev/null "http://$myBRIDGE:$myWEBPORT/" 2>/dev/null && break
  sleep 2
done
if docker exec "$myNGINX" wget -q -T 5 -O /dev/null "http://$myBRIDGE:$myWEBPORT/" 2>/dev/null; then
  fuINFO "nginx can reach the deployer at http://$myBRIDGE:$myWEBPORT"
else
  fuWARN "nginx cannot reach http://$myBRIDGE:$myWEBPORT (is the deployer up? a host firewall blocking docker0?)"
  myOK=false
fi
myCODE="$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:64297/sensors/" || true)"
if [ "$myCODE" = "401" ]; then
  fuINFO "/sensors/ is served and asks for the T-Pot login"
else
  fuWARN "/sensors/ returned HTTP $myCODE (expected 401 without a login)"
  myOK=false
fi
if docker exec "$myNGINX" grep -q 'href="/sensors/"' /var/lib/nginx/html/index.html; then
  fuINFO "landing page has the Sensor Deployer link"
else
  fuWARN "landing page is missing the Sensor Deployer link"
  myOK=false
fi

echo
$myOK && fuINFO "Done. Open https://<hive>:64297/ and use the Sensor Deployer link." || fuDIE "finished with warnings (see above)"
