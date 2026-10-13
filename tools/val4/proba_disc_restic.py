#!/usr/bin/env python3
"""Proba pe disc restic (H-R-10 / HIR-REQ-040) — runner portabil SC00.

Sursa canonica: tools/val4/proba_disc_restic.py (repo BOAgents).
Copiile din agenti/BO-A01/instrumente-f5/ sunt derivate, cu hash
consemnat. Ruleaza pe orice Linux/macOS cu python3 + restic + bash.

Este o PROBA PE DISC — un repository restic local, intr-o radacina de
test aleasa de SC00. NU este o proba S3: partea S3 ramane
NEEXECUTAT/HOLD indiferent de verdictul acestei probe.

    python3 proba_disc_restic.py \
        --zona <radacina-de-test-aleasa-de-SC00> \
        --repository <zona>/repo-test \
        --password-file <fisier-parola-repo> \
        --adapter <cale catre backups/restic-backup.sh din pachet> \
        [--restic-bin <cale-restic>] [--keep-daily N] [--keep-weekly N]

Preflight refuza cu exit=2 INAINTE de orice mkdir/restic/efect:
- --zona lipsa; orice cale (--repository/--sources-dir/--restore-dir/
  --out) in afara zonei (realpath+commonpath: sibling, .., symlink)
- repository existent NEGOL si fara config restic (repo strain — nu
  se suprascrie); repository identic/suprapus cu surse/restore/out
- --password-file/--restic-bin/--adapter lipsa
- --keep-daily/--keep-weekly non-intreg sau in afara 0..3650

Efectul: surse sintetice generate in <zona>/surse (inclusiv un fisier
cu spatii), `restic init`, `restic check` independent daca init esueaza,
backup PRIN ADAPTORUL DE PRODUCTIE (restic-backup.sh) cu env construit
EXCLUSIV de runner — RESTIC_REPOSITORY/RESTIC_PASSWORD_FILE/RESTIC_BIN/
BACKUP_SOURCES/BACKUP_EXCLUDE/BACKUP_EXTRA_ARGS/BACKUP_KEEP_* — fara
valori mostenite si fara implicitele de productie S3 ale adaptorului.
`snapshots --json` alege NUMAI snapshotul ale carui `paths` contin
sursele curente (nu ultimul, nu latest); `restore <id>` intr-un
director nou; hash sha256 comparat cu sursa.

Exit: 0 PASS / 1 FAIL / 2 REFUZ. Parola nu apare in argv, log sau
raport; detaliile sunt trunchiate si elidate.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

_RETENTIE_MAX = 3650

# Adaptorul de productie pinat (Hire@3f30640 backups/restic-backup.sh).
# --adapter-sha256 il suprascrie pentru o revizie viitoare a adaptorului.
_EXPECTED_ADAPTER_SHA256 = \
    "04a8a4aa6f11e98198b9e87ac86b9881a8f38ad93d93f76d9ca62b4de6418d08"
_MARKER = ".proba-disc-f5"
_TIMEOUT = 600

_ENV_MINIM = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


def _curata(text) -> str:
    t = re.sub(
        r'((?:password|access_token|aws_secret_access_key|secret|'
        r'authorization)["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+',
        r'\1***', str(text), flags=re.IGNORECASE)
    return t[:200]


def in_zona(cale: str, zona: str) -> bool:
    try:
        rz = os.path.realpath(zona)
        r = os.path.realpath(cale)
        return os.path.commonpath([r, rz]) == rz
    except (ValueError, OSError):
        return False


def straine(a: str, b: str) -> bool:
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    if ra == rb:
        return False
    try:
        return (os.path.commonpath([ra, rb]) != ra
                and os.path.commonpath([ra, rb]) != rb)
    except ValueError:
        return True


def refuza(motiv: str, cod: int = 2) -> int:
    print(json.dumps({"verdict": "REFUZ", "tip": "PROBĂ PE DISC",
                      "s3": "NEEXECUTAT/HOLD", "motiv": _curata(motiv),
                      "efect": "zero"}, ensure_ascii=False))
    return cod


def sha_fisier(cale: str) -> str:
    h = hashlib.sha256()
    with open(cale, "rb") as f:
        for buc in iter(lambda: f.read(1 << 20), b""):
            h.update(buc)
    return h.hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Proba pe disc restic (H-R-10) — repository local "
                    "dedicat, NU proba S3")
    p.add_argument("--zona", required=True,
                   help="radacina de test aleasa de SC00 — toate caile "
                        "trebuie sa fie in interiorul ei")
    p.add_argument("--repository", required=True,
                   help="director dedicat pentru repo-ul restic local")
    p.add_argument("--password-file", required=True,
                   help="fisier cu parola repo-ului de test")
    p.add_argument("--adapter", required=True,
                   help="calea catre adaptorul de productie "
                        "restic-backup.sh din pachetul predat")
    p.add_argument("--restic-bin", default="restic",
                   help="executabilul restic (implicit: restic din PATH)")
    p.add_argument("--sources-dir", default=None,
                   help="implicit <zona>/surse")
    p.add_argument("--restore-dir", default=None,
                   help="implicit <zona>/restore")
    p.add_argument("--out", default=None,
                   help="implicit <zona>/raport-disc")
    p.add_argument("--keep-daily", default="1",
                   help="retentie zilnica, intreg 0..3650")
    p.add_argument("--keep-weekly", default="0",
                   help="retentie saptamanala, intreg 0..3650")
    p.add_argument("--adapter-sha256",
                   default=_EXPECTED_ADAPTER_SHA256,
                   help="sha256 asteptat al adaptorului de productie")
    args = p.parse_args(argv)

    surse = args.sources_dir or os.path.join(args.zona, "surse")
    restore = args.restore_dir or os.path.join(args.zona, "restore")
    out = args.out or os.path.join(args.zona, "raport-disc")

    # --- refuzuri inainte de efect -----------------------------------
    if not re.fullmatch(r"[0-9]+", args.keep_daily or "") \
            or not re.fullmatch(r"[0-9]+", args.keep_weekly or "") \
            or int(args.keep_daily) > _RETENTIE_MAX \
            or int(args.keep_weekly) > _RETENTIE_MAX:
        return refuza(f"retentie invalida — intreg 0..{_RETENTIE_MAX}")
    if int(args.keep_daily) == 0 and int(args.keep_weekly) == 0:
        return refuza("retentie 0/0 ar sterge snapshotul probei")
    if os.path.realpath(args.zona) == os.path.abspath(os.sep):
        return refuza("--zona nu poate fi radacina sistemului")
    if os.path.exists(args.zona) and not os.path.isdir(args.zona):
        return refuza("--zona exista si nu e director")
    # Caracterele de control din cai ar sparge protocolul linie-cu-linie
    # al adaptorului (BACKUP_SOURCES se separa pe \n) — un fragment ar
    # deveni sursa din afara zonei.
    for nume, cale in (("zona", args.zona), ("repository", args.repository),
                       ("sources-dir", surse), ("restore-dir", restore),
                       ("out", out), ("password-file", args.password_file),
                       ("adapter", args.adapter),
                       ("restic-bin", args.restic_bin)):
        if re.search(r"[\x00-\x1f\x7f]", cale):
            return refuza(f"caracter de control in --{nume}")

    if not os.path.isfile(args.password_file):
        return refuza("--password-file inexistent")
    restic_r = shutil.which(args.restic_bin)
    if restic_r is None:
        return refuza("--restic-bin inexistent sau neexecutabil")
    bash_r = shutil.which("bash")
    if bash_r is None:
        return refuza("bash lipseste — adaptorul de productie il cere")
    if not os.path.isfile(args.adapter):
        return refuza("adaptorul de productie lipseste (--adapter)")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", args.adapter_sha256 or ""):
        return refuza("--adapter-sha256 invalid (64 hex)")
    try:
        potrivit = sha_fisier(args.adapter) == args.adapter_sha256.lower()
    except OSError:
        return refuza("adaptorul nu poate fi citit")
    if not potrivit:
        return refuza("sha256 --adapter nu e cel pinat — adaptor "
                      "strain/modificat")

    for nume, cale in (("repository", args.repository),
                       ("sources-dir", surse), ("restore-dir", restore),
                       ("out", out)):
        if not in_zona(cale, args.zona):
            return refuza(f"--{nume} in afara zonei (sibling/traversal/"
                          "symlink)")
    for a, b in ((args.repository, surse), (args.repository, restore),
                 (args.repository, out), (surse, restore), (surse, out),
                 (restore, out)):
        if not straine(a, b):
            return refuza("repository/sources/restore/out se suprapun")

    repo_r = os.path.realpath(args.repository)
    if os.path.exists(repo_r) and not os.path.isdir(repo_r):
        return refuza("--repository exista si nu e director")
    try:
        continut_repo = os.listdir(repo_r) if os.path.exists(repo_r) \
            else []
    except OSError:
        return refuza("--repository existent dar necitibil")
    if continut_repo:
        # Refolosim numai un repo pe care l-am creat noi: are config
        # restic si marcajul probei (cu continutul exact). Orice altceva
        # (config lipsa → dir strain; fara marcaj → repo restic al
        # produsului) e refuzat — `forget --prune` ar sterge snapshoturi
        # care nu ne apartin.
        try:
            with open(os.path.join(repo_r, _MARKER)) as f:
                marcaj_ok = f.read().strip() == "proba-disc-f5"
        except OSError:
            marcaj_ok = False
        dedicat = os.path.isfile(os.path.join(repo_r, "config")) \
            and marcaj_ok
        if not dedicat:
            return refuza("--repository existent negol fara marcajul "
                          "probei — repo strain, nu se suprascrie")
    for nume, cale in (("sources-dir", surse), ("restore-dir", restore),
                       ("out", out)):
        r = os.path.realpath(cale)
        if os.path.exists(r):
            if not os.path.isdir(r):
                return refuza(f"--{nume} exista si nu e director")
            try:
                gol = not os.listdir(r)
            except OSError:
                return refuza(f"--{nume} existent dar necitibil")
            if not gol:
                return refuza(f"--{nume} existent negol — probele "
                              "folosesc directoare dedicate")

    cache_r = os.path.join(os.path.realpath(args.zona), ".cache-restic")
    tmp_r = os.path.join(os.path.realpath(args.zona), ".tmp")
    # .cache-restic/.tmp se creeaza in zona — un symlink pre-pus ar
    # redirectiona scrierile restic in afara; iar caile probei nu au
    # voie sa le atinga.
    for nume, cale in ((".cache-restic", cache_r), (".tmp", tmp_r)):
        if os.path.islink(cale):
            return refuza(f"{nume} e symlink — ar iesi din zona")
        if os.path.exists(cale) and not os.path.isdir(cale):
            return refuza(f"{nume} exista si nu e director")
    for cale in (args.repository, surse, restore, out):
        if os.path.realpath(cale) in (os.path.realpath(cache_r),
                                      os.path.realpath(tmp_r)):
            return refuza("caile probei se suprapun cu "
                          ".cache-restic/.tmp")
    env = {k: os.environ[k] for k in _ENV_MINIM if k in os.environ}
    env.update(RESTIC_BIN=restic_r, TMPDIR=tmp_r,
               RESTIC_CACHE_DIR=cache_r,
               RESTIC_REPOSITORY=repo_r,
               RESTIC_PASSWORD_FILE=os.path.realpath(args.password_file),
               BACKUP_SOURCES=os.path.realpath(surse),
               BACKUP_KEEP_DAILY=args.keep_daily,
               BACKUP_KEEP_WEEKLY=args.keep_weekly,
               BACKUP_EXCLUDE="", BACKUP_EXTRA_ARGS="")

    raport = {"verdict": "PASS", "tip": "PROBĂ PE DISC",
              "s3": "NEEXECUTAT/HOLD", "pasi": [],
              "repository": repo_r, "zona": os.path.realpath(args.zona),
              "keep": {"daily": int(args.keep_daily),
                       "weekly": int(args.keep_weekly)}}

    def pas(nume, ok, detalii=""):
        raport["pasi"].append({"pas": nume, "ok": bool(ok),
                               "detalii": _curata(detalii)})
        if not ok:
            raport["verdict"] = "FAIL"

    def ruleaza(cmd):
        try:
            return subprocess.run(cmd, env=env, capture_output=True,
                                  text=True, timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 127, "", str(e))

    try:
        surse_r = os.path.realpath(surse)
        os.makedirs(surse_r, exist_ok=True)
        os.makedirs(os.path.realpath(restore), exist_ok=True)
        os.makedirs(os.path.realpath(out), exist_ok=True)
        os.makedirs(cache_r, exist_ok=True)
        os.makedirs(tmp_r, exist_ok=True)
        for i in range(3):
            with open(os.path.join(surse_r, f"sintetic-{i}.txt"),
                      "w") as f:
                f.write(f"proba-disc-f5 continut sintetic {i}\n")
        with open(os.path.join(surse_r, "sintetic cu spatii.txt"),
                  "w") as f:
            f.write("proba-disc-f5 fisier cu spatii in nume\n")
        hash_inainte = {f: sha_fisier(os.path.join(surse_r, f))
                        for f in sorted(os.listdir(surse_r))}
        pas("surse sintetice generate", len(hash_inainte) == 4)

        os.makedirs(repo_r, exist_ok=True)
        with open(os.path.join(repo_r, _MARKER), "w") as f:
            f.write("proba-disc-f5\n")

        r = ruleaza([restic_r, "-r", repo_r, "init"])
        if r.returncode == 0:
            pas("restic init", True)
        else:
            c = ruleaza([restic_r, "-r", repo_r, "check"])
            pas("restic init", c.returncode == 0,
                f"init rc={r.returncode}, check rc={c.returncode}: "
                + _curata((r.stderr or r.stdout)[:120]))
            if c.returncode != 0:
                raise RuntimeError("init esuat si repo-ul nu e confirmat")

        r = ruleaza([bash_r, args.adapter])
        pas("backup+retentie prin adaptorul de productie",
            r.returncode == 0,
            (r.stderr or r.stdout).strip().splitlines()[-1:])

        r = ruleaza([restic_r, "-r", repo_r, "snapshots", "--json"])
        snap_id = None
        if r.returncode == 0:
            try:
                snap = json.loads(r.stdout)
                if isinstance(snap, list):
                    ale_nostre = [s for s in snap
                                  if isinstance(s, dict)
                                  and surse_r in (s.get("paths") or [])]
                    if ale_nostre:
                        nou = max(ale_nostre,
                                  key=lambda s: s.get("time") or "")
                        snap_id = nou.get("id") or nou.get("short_id")
            except Exception:
                pass
        pas("snapshotul probei legat de surse", bool(snap_id),
            f"rc={r.returncode} id={snap_id}")
        if not snap_id:
            raise RuntimeError("niciun snapshot asociat surselor probei")

        r = ruleaza([restic_r, "-r", repo_r,
                     "restore", snap_id, "--target",
                     os.path.realpath(restore)])
        pas("restore snapshotul exact", r.returncode == 0,
            (r.stderr or r.stdout).strip().splitlines()[-1:])

        restaurate = {}
        for radacina, _d, fisiere in os.walk(os.path.realpath(restore)):
            for f in fisiere:
                restaurate[f] = sha_fisier(
                    os.path.join(radacina, f))
        ok = all(restaurate.get(f) == h
                 for f, h in hash_inainte.items())
        pas("hash restore == sursa", ok,
            json.dumps(sorted(restaurate))[:160])
        raport["snapshot_id"] = snap_id
        raport["hashuri"] = hash_inainte
    except Exception as e:
        pas("lant proba", False, _curata(e))
        raport["verdict"] = "FAIL"

    try:
        with open(os.path.join(os.path.realpath(out),
                               "PROBA-PE-DISC.json"), "w") as f:
            json.dump(raport, f, indent=1, ensure_ascii=False)
    except OSError as e:
        raport["verdict"] = "FAIL"
        raport["pasi"].append({"pas": "scriere raport", "ok": False,
                               "detalii": _curata(e)})
    print(json.dumps(raport, ensure_ascii=False))
    return 0 if raport["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
