#!/usr/bin/env bash
#
# go-live.sh — productionize research-api on a custom domain (Cloud Run).
#
# Wires the DSA VLOP transparency dashboard + API onto its public hostname:
#   - applies production env (PUBLIC_BASE_URL / GOOGLE_CLIENT_ID / ADMIN_EMAILS)
#   - ensures the Cloud Run domain mapping exists
#   - adds the DNS record (if the zone is reachable from this account)
#   - prints the manual steps gcloud can't do (OAuth JS origin; off-account DNS)
#   - verifies the service is live over the custom domain
#
# Idempotent: safe to re-run. Requires an authenticated gcloud (`gcloud auth login`).
#
# Usage:
#   ADMIN_EMAILS=you@example.com ./scripts/go-live.sh [all|env|domain|dns|verify]
#   (ADMIN_EMAILS is read from the environment or prompted for — never committed.)

set -euo pipefail

# ── Config (public values — safe to commit) ──────────────────────────────────
PROJECT="${PROJECT:-transparency-site}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-research-api}"
DOMAIN="${DOMAIN:-transparency.kieranmaynard.com}"
CLIENT_ID="${CLIENT_ID:-694282548149-5ro3pokkjgp8d4ht5n8jmm8o9gqug961.apps.googleusercontent.com}"
# ADMIN_EMAILS is PII → not hardcoded (public repo). Sourced from env or prompt.
ADMIN_EMAILS="${ADMIN_EMAILS:-}"

# ── Pretty logging ───────────────────────────────────────────────────────────
bold=$(printf '\033[1m'); dim=$(printf '\033[2m'); grn=$(printf '\033[32m')
ylw=$(printf '\033[33m'); red=$(printf '\033[31m'); rst=$(printf '\033[0m')
step() { printf '\n%s==> %s%s\n' "$bold" "$1" "$rst"; }
info() { printf '    %s\n' "$1"; }
ok()   { printf '    %s✓ %s%s\n' "$grn" "$1" "$rst"; }
warn() { printf '    %s! %s%s\n' "$ylw" "$1" "$rst"; }
die()  { printf '\n%sERROR: %s%s\n' "$red" "$1" "$rst" >&2; exit 1; }

g() { gcloud "$@" --project "$PROJECT" --region "$REGION"; }   # run+region scoped

# ── Preflight ────────────────────────────────────────────────────────────────
preflight() {
  step "Preflight"
  command -v gcloud >/dev/null || die "gcloud not found on PATH."
  local acct
  acct="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
  [[ -n "$acct" ]] || die "No active gcloud account. Run: gcloud auth login"
  ok "Authenticated as $acct"
  g run services describe "$SERVICE" --format='value(metadata.name)' >/dev/null 2>&1 \
    || die "Service '$SERVICE' not found in $PROJECT/$REGION (deploy it first)."
  ok "Service $SERVICE found in $PROJECT/$REGION"
}

# ── Step: production env ─────────────────────────────────────────────────────
# Patches just these keys; ALLOW_DEMO_KEYS=0, LOG_FORMAT=json, and the secret
# refs set via service.yaml are preserved.
apply_env() {
  step "Apply production env to $SERVICE"
  if [[ -z "$ADMIN_EMAILS" ]]; then   # only this step needs it; verify/dns don't
    read -rp "    Admin email(s) for ADMIN_EMAILS (comma-separated): " ADMIN_EMAILS
    [[ -n "$ADMIN_EMAILS" ]] || die "ADMIN_EMAILS is required."
  fi
  # Use a non-comma delimiter (^;^) so a comma-separated ADMIN_EMAILS list isn't
  # mis-split by gcloud's dict parser. ';' appears in none of these values.
  g run services update "$SERVICE" --quiet \
    --update-env-vars "^;^PUBLIC_BASE_URL=https://${DOMAIN};GOOGLE_CLIENT_ID=${CLIENT_ID};ADMIN_EMAILS=${ADMIN_EMAILS}"
  ok "Set PUBLIC_BASE_URL=https://${DOMAIN}, GOOGLE_CLIENT_ID, ADMIN_EMAILS"
  local demo
  demo="$(g run services describe "$SERVICE" \
    --format='value(spec.template.spec.containers[0].env.filter("name:ALLOW_DEMO_KEYS").extract(value))' 2>/dev/null || true)"
  demo="${demo//[\[\]\' ]/}"   # gcloud renders list values as ['0']; normalize to 0
  if [[ "$demo" == "0" ]]; then ok "ALLOW_DEMO_KEYS=0 (Google sign-in only)"
  else warn "ALLOW_DEMO_KEYS is '${demo:-unset}' — set it to 0 for production."; fi
}

# ── Step: domain mapping ─────────────────────────────────────────────────────
ensure_domain_mapping() {
  step "Ensure Cloud Run domain mapping for $DOMAIN"
  if g beta run domain-mappings describe --domain "$DOMAIN" --format='value(metadata.name)' >/dev/null 2>&1; then
    ok "Mapping already exists"
  else
    g beta run domain-mappings create --service "$SERVICE" --domain "$DOMAIN" --quiet
    ok "Mapping created"
  fi
  RR_TYPE="$(g beta run domain-mappings describe --domain "$DOMAIN" --format='value(status.resourceRecords[0].type)' 2>/dev/null || true)"
  RR_DATA="$(g beta run domain-mappings describe --domain "$DOMAIN" --format='value(status.resourceRecords[0].rrdata)' 2>/dev/null || true)"
  RR_TYPE="${RR_TYPE:-CNAME}"; RR_DATA="${RR_DATA:-ghs.googlehosted.com.}"
  info "Required DNS record:  ${DOMAIN}.  ${RR_TYPE}  ${RR_DATA}"
}

# ── Step: DNS record ─────────────────────────────────────────────────────────
# The zone may live in a project this account can't see (it currently does not
# resolve). Search every accessible project; create the record if found, else
# print exactly what to add wherever kieranmaynard.com DNS is managed.
ensure_dns() {
  step "Ensure DNS record ${DOMAIN} -> ${RR_DATA:-ghs.googlehosted.com.}"
  local rtype="${RR_TYPE:-CNAME}" rdata="${RR_DATA:-ghs.googlehosted.com.}"
  local apex="kieranmaynard.com." zone="" zproj=""
  for p in $(gcloud projects list --format='value(projectId)' 2>/dev/null); do
    zone="$(gcloud dns managed-zones list --project "$p" \
      --filter="dnsName=${apex}" --format='value(name)' 2>/dev/null | head -n1 || true)"
    if [[ -n "$zone" ]]; then zproj="$p"; break; fi
  done

  if [[ -z "$zone" ]]; then
    warn "No Cloud DNS zone for ${apex} in any accessible project."
    cat <<EOF
    → Add this record where you manage kieranmaynard.com DNS (do NOT touch apex/www):
          Name:  ${DOMAIN}
          Type:  ${rtype}
          Value: ${rdata}
          TTL:   300
EOF
    return 0
  fi

  ok "Zone '$zone' in project '$zproj'"
  if gcloud dns record-sets list --zone "$zone" --project "$zproj" \
       --name "${DOMAIN}." --type "$rtype" --format='value(name)' 2>/dev/null | grep -q .; then
    ok "Record ${DOMAIN}. ${rtype} already exists"
  else
    gcloud dns record-sets create "${DOMAIN}." --zone "$zone" --project "$zproj" \
      --type "$rtype" --ttl 300 --rrdatas "$rdata"
    ok "Created ${DOMAIN}. ${rtype} -> ${rdata}"
  fi
}

# ── Step: OAuth origin (manual — not exposed via gcloud) ─────────────────────
oauth_origin() {
  step "OAuth: add the domain to Authorized JavaScript origins (manual)"
  cat <<EOF
    gcloud cannot edit an OAuth web client's JS origins. In the console:
      https://console.cloud.google.com/apis/credentials?project=${PROJECT}
    Open the client ending in ...${CLIENT_ID%%.*} and under
    "Authorized JavaScript origins" add:
          https://${DOMAIN}
    (Without this, Google sign-in fails on the custom domain.)
EOF
}

# ── Step: verify ─────────────────────────────────────────────────────────────
verify() {
  step "Verify go-live on https://${DOMAIN}"
  info "Checking managed-TLS cert status (can take up to ~15–60 min on first map)…"
  local ready="" i
  for i in $(seq 1 5); do
    ready="$(g beta run domain-mappings describe --domain "$DOMAIN" \
      --format='value(status.conditions.filter("type:Ready").extract(status))' 2>/dev/null || true)"
    ready="${ready//[\[\]\' ]/}"   # normalize gcloud list rendering, e.g. ['True'] -> True
    [[ "$ready" == "True" ]] && break
    info "  cert not ready yet (attempt $i/5); sleeping 20s…"; sleep 20
  done
  if [[ "$ready" == "True" ]]; then ok "Domain mapping Ready (cert provisioned)"
  else warn "Mapping not Ready yet — DNS may still be propagating. Re-run: $0 verify"; fi

  local base="https://${DOMAIN}"
  if curl -fsS --max-time 15 "${base}/readyz" >/dev/null 2>&1; then
    ok "GET /readyz -> 200"
    info "version: $(curl -fsS --max-time 15 "${base}/version" 2>/dev/null || echo '?')"
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
      -H 'X-API-Key: alice' "${base}/api/tables" || true)"
    if [[ "$code" == "401" || "$code" == "403" ]]; then
      ok "Demo key rejected (HTTP $code) — ALLOW_DEMO_KEYS=0 in effect"
    else
      warn "Demo key returned HTTP $code (expected 401/403). Check ALLOW_DEMO_KEYS."
    fi
  else
    warn "${base}/readyz not reachable yet (DNS/cert still settling)."
  fi

  printf '\n%sManual final checks:%s sign in at %s/portal with an ADMIN_EMAILS\n' "$bold" "$rst" "$base"
  printf '  account, then run a query end-to-end (submit -> poll -> signed download).\n'
}

# ── Dispatch ─────────────────────────────────────────────────────────────────
main() {
  local what="${1:-all}"
  preflight
  case "$what" in
    env)    apply_env ;;
    domain) ensure_domain_mapping ;;
    dns)    ensure_domain_mapping; ensure_dns ;;
    verify) verify ;;
    all)
      apply_env
      ensure_domain_mapping
      ensure_dns
      oauth_origin
      verify
      step "Done"
      info "If DNS/OAuth steps were manual above, complete them, then: $0 verify"
      ;;
    *) die "Unknown step '$what'. Use: all | env | domain | dns | verify" ;;
  esac
}

main "$@"
