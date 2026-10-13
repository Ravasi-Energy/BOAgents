#!/usr/bin/env bash
# @requirement HIR-REQ-040
# Adaptorul de backup al produsului — parametrizat, fail-closed.
#
# Toate valorile se injectează prin mediu; implicitele sunt cele de
# producție (S3 suveran, Hetzner Falkenstein — HIR-DEC-006). O probă
# le poate suprascrie toate fără să atingă configurația vie:
#
#   RESTIC_BIN           — executabilul restic (implicit: restic din PATH)
#   RESTIC_REPOSITORY    — repo (implicit: galeata S3 suverană)
#   RESTIC_PASSWORD_FILE — fișierul cu parola repo (obligatoriu să existe)
#   BACKUP_SOURCES       — căile de backup, câte una pe LINIE
#                          (implicit: volumele + configul de producție).
#                          Separatorul e linia nouă, nu spațiul: o cale
#                          cu spații e o singură sursă, nu mai multe.
#   BACKUP_EXCLUDE       — exclude suplimentare, câte unul pe linie
#   BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY — retenția (implicit 7 / 4),
#                          întregi 0..3650; 0 înseamnă politică explicită
#                          „nu păstra nimic", nu lipsă de politică
#   BACKUP_EXTRA_ARGS    — flaguri restic suplimentare pentru `backup`,
#                          câte unul pe linie
#
# Refuzuri explicite (exit≠0), toate ÎNAINTE de orice invocare restic:
# executabil lipsă, fișier-parolă lipsă, repository gol, nicio sursă,
# sursă inexistentă, retenție neîntreagă sau în afara intervalului.
# Nu se „repară" nimic pe tăcute.
set -euo pipefail

RESTIC_BIN="${RESTIC_BIN:-restic}"
export RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-s3:fsn1.your-objectstorage.com/hire-backups-sovereign}"
export RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/etc/restic/password.txt}"
BACKUP_SOURCES="${BACKUP_SOURCES:-/var/lib/docker/volumes/hire_pgdata
/etc/hire/config}"
BACKUP_EXCLUDE="${BACKUP_EXCLUDE:-/var/lib/docker/overlay2}"
# Fără „:" — o valoare explicit goală NU înseamnă implicitul, înseamnă
# configurare stricată și se refuză la validare, nu trece ca 7/4.
BACKUP_KEEP_DAILY="${BACKUP_KEEP_DAILY-7}"
BACKUP_KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY-4}"
BACKUP_EXTRA_ARGS="${BACKUP_EXTRA_ARGS:-}"
# Pragul retenției: 10 ani de păstrări zilnice e peste orice politică
# rezonabilă; mai sus e aproape sigur o tastare greșită sau un atac asupra
# configului, nu o alegere reală.
RETENTIE_MAX=3650

refuza() { echo "[!] $1" >&2; exit 2; }

# Listele vin câte un element pe linie — singurul delimitator care nu
# poate apărea în interiorul unei căi sau al unui flag.
listeaza() {
  local var="$1" linie
  while IFS= read -r linie; do
    [ -n "$linie" ] && printf '%s\n' "$linie"
  done <<< "$var"
}

retentie_valida() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;            # text, negativ, fracție, gol
  esac
  [ "$1" -le "$RETENTIE_MAX" ] 2>/dev/null || return 1
  return 0
}

command -v "$RESTIC_BIN" >/dev/null 2>&1 || refuza "restic lipsește: $RESTIC_BIN"
[ -n "$RESTIC_REPOSITORY" ] || refuza "RESTIC_REPOSITORY gol"
[ -f "$RESTIC_PASSWORD_FILE" ] || refuza "fișier-parolă lipsă: $RESTIC_PASSWORD_FILE"

# Retenția se validează înainte de ORICE invocare restic — un forget/prune
# cu o valoare nevalidată poate șterge păstrări legitime.
retentie_valida "$BACKUP_KEEP_DAILY" \
  || refuza "retenție zilnică invalidă (întreg 0..$RETENTIE_MAX): $BACKUP_KEEP_DAILY"
retentie_valida "$BACKUP_KEEP_WEEKLY" \
  || refuza "retenție săptămânală invalidă (întreg 0..$RETENTIE_MAX): $BACKUP_KEEP_WEEKLY"

SURSE=()
while IFS= read -r src; do SURSE+=("$src"); done < <(listeaza "$BACKUP_SOURCES")
[ "${#SURSE[@]}" -gt 0 ] || refuza "nicio sursă de backup"
for src in "${SURSE[@]}"; do
  [ -e "$src" ] || refuza "sursa de backup nu există: $src"
done

EXCLUDE_ARGS=()
while IFS= read -r e; do EXCLUDE_ARGS+=("--exclude=$e"); done < <(listeaza "$BACKUP_EXCLUDE")
EXTRA=()
while IFS= read -r a; do EXTRA+=("$a"); done < <(listeaza "$BACKUP_EXTRA_ARGS")

echo "[+] Backup Hire — repo: $RESTIC_REPOSITORY"
# bash 3.2 (macOS): "${arr[@]}" pe array gol pică sub `set -u`
"$RESTIC_BIN" backup ${EXCLUDE_ARGS[@]+"${EXCLUDE_ARGS[@]}"} \
  ${EXTRA[@]+"${EXTRA[@]}"} "${SURSE[@]}"

echo "[+] Retenție: $BACKUP_KEEP_DAILY zilnic / $BACKUP_KEEP_WEEKLY săptămânal"
"$RESTIC_BIN" forget --keep-daily "$BACKUP_KEEP_DAILY" \
  --keep-weekly "$BACKUP_KEEP_WEEKLY" --prune
