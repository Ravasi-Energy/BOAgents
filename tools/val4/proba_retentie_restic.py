#!/usr/bin/env python3
"""Proba retentiei cu eliminare efectiva (H-R-10 / HIR-REQ-040) — revizie.

Sursa canonica: tools/val4/proba_retentie_restic.py (repo BOAgents).
Complementara lui proba_disc_restic.py: acea proba a aratat init/
backup/restore/hash pe Mac si server, dar 15.10 a eliminat ZERO
snapshoturi — prune a rulat fara sa stearga nimic. Aceasta proba
demonstreaza eliminarea efectiva: mai multe snapshoturi in acelasi
grup host/paths, politica determinista, si blob-urile nepartajate ale
snapshotului eliminat nu mai exista in repository dupa prune.

    python3 proba_retentie_restic.py \
        --zona <radacina-de-test-aleasa-de-SC00> \
        --repository <zona>/repo-test \
        --password-file <fisier-parola-repo> \
        --adapter <cale catre backups/restic-backup.sh din pachet> \
        [--restic-bin <cale-restic>] [--keep-daily N] [--keep-weekly N]
        [--runde N]   # 2..10, implicit 3

Mecanismul determinist: fiecare runda rescrie sursele (continut comun
cu versiune noua + un fisier unic al rundei sters inainte de runda
urmatoare + fisierul cu spatii constant) si cheama adaptorul de
productie, care face backup + forget --prune. Rundele intermediare
ruleaza cu retentie maxima (3650/3650 = „pastreaza tot") ca grupul sa
acumuleze snapshoturi; ultima runda aplica politica ceruta (implicit
keep-daily=1 — aceeasi zi => acelasi grup zilnic => se pastreaza doar
cel mai nou). Datele unice din snapshoturile eliminate nu sunt
partajate de cel pastrat, deci prune TREBUIE sa le stearga.

Dovada eliminarii efective (nu scadere bruta de bytes, nu doar exit0):
- id-urile snapshoturilor eliminate lipsesc din `snapshots --json` dupa
- blob-ul fisierului unic al fiecarui snapshot eliminat: `cat blob`
  reuseste INAINTE de forget si esueaza DUPA prune
- contoarele prune din iesirea adaptorului sunt inregistrate
- `restic check` dupa prune; restore al snapshotului pastrat cu
  hashuri sursa/restaurare comparate separat

Preflight-ul refuza cu exit=2 INAINTE de orice efect — aceleasi
refuzuri ca proba_disc_restic.py (containment in zona, adaptor pinat
sha256, caractere de control, directoare dedicate, repo strain,
marcaj, symlink-uri, retentie), plus --runde in afara intervalului.
Niciodata nu se dezactiveaza un refuz pentru a forta scenariul.

PROBA PE DISC — partea S3 ramane NEEXECUTAT/HOLD indiferent de
verdictul acestei probe.
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
_RUNDE_MIN, _RUNDE_MAX = 2, 10

# Adaptorul de productie pinat (Hire@3f30640 backups/restic-backup.sh).
# --adapter-sha256 il suprascrie pentru o revizie viitoare a adaptorului.
_EXPECTED_ADAPTER_SHA256 = \
    "04a8a4aa6f11e98198b9e87ac86b9881a8f38ad93d93f76d9ca62b4de6418d08"
_MARKER = ".proba-retentie-f5"
_MARKER_TEXT = "proba-retentie-f5"
_TIMEOUT = 600

_ENV_MINIM = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


def _curata(text, limit=200) -> str:
    t = re.sub(
        r'((?:password|access_token|aws_secret_access_key|secret|'
        r'authorization)["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+',
        r'\1***', str(text), flags=re.IGNORECASE)
    return t[:limit]


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
    print(json.dumps({"verdict": "REFUZ", "tip": "PROBĂ RETENȚIE PE DISC",
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
        description="Proba retentiei cu eliminare efectiva (H-R-10) — "
                    "repository local dedicat, NU proba S3")
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
                   help="implicit <zona>/raport-retentie")
    p.add_argument("--keep-daily", default="1",
                   help="politica finala: retentie zilnica, intreg "
                        "0..3650")
    p.add_argument("--keep-weekly", default="0",
                   help="politica finala: retentie saptamanala, intreg "
                        "0..3650")
    p.add_argument("--runde", default="3",
                   help="snapshoturi create in acelasi grup inainte de "
                        "politica finala, intreg 2..10")
    p.add_argument("--adapter-sha256",
                   default=_EXPECTED_ADAPTER_SHA256,
                   help="sha256 asteptat al adaptorului de productie")
    args = p.parse_args(argv)

    surse = args.sources_dir or os.path.join(args.zona, "surse")
    restore = args.restore_dir or os.path.join(args.zona, "restore")
    out = args.out or os.path.join(args.zona, "raport-retentie")

    # --- refuzuri inainte de efect -----------------------------------
    if not re.fullmatch(r"[0-9]+", args.keep_daily or "") \
            or not re.fullmatch(r"[0-9]+", args.keep_weekly or "") \
            or int(args.keep_daily) > _RETENTIE_MAX \
            or int(args.keep_weekly) > _RETENTIE_MAX:
        return refuza(f"retentie invalida — intreg 0..{_RETENTIE_MAX}")
    if int(args.keep_daily) == 0 and int(args.keep_weekly) == 0:
        return refuza("retentie 0/0 ar sterge si snapshotul pastrat")
    if not re.fullmatch(r"[0-9]+", args.runde or "") \
            or not (_RUNDE_MIN <= int(args.runde) <= _RUNDE_MAX):
        return refuza(f"--runde invalid — intreg {_RUNDE_MIN}.."
                      f"{_RUNDE_MAX}")
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
        # Refolosim numai un repo pe care l-am creat noi: config restic
        # + marcajul probei DE RETENTIE (cu continutul exact). Un repo
        # al probei de disc (alt marcaj) sau fara marcaj e strain —
        # `forget --prune` ar sterge snapshoturi care nu ne apartin.
        try:
            with open(os.path.join(repo_r, _MARKER)) as f:
                marcaj_ok = f.read().strip() == _MARKER_TEXT
        except OSError:
            marcaj_ok = False
        dedicat = os.path.isfile(os.path.join(repo_r, "config")) \
            and marcaj_ok
        if not dedicat:
            return refuza("--repository existent negol fara marcajul "
                          "probei de retentie — repo strain, nu se "
                          "suprascrie")
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

    runde = int(args.runde)

    env = {k: os.environ[k] for k in _ENV_MINIM if k in os.environ}
    env.update(RESTIC_BIN=restic_r, TMPDIR=tmp_r,
               RESTIC_CACHE_DIR=cache_r,
               RESTIC_REPOSITORY=repo_r,
               RESTIC_PASSWORD_FILE=os.path.realpath(args.password_file),
               BACKUP_SOURCES=os.path.realpath(surse),
               BACKUP_EXCLUDE="", BACKUP_EXTRA_ARGS="")

    raport = {"verdict": "PASS", "tip": "PROBĂ RETENȚIE PE DISC",
              "s3": "NEEXECUTAT/HOLD", "pasi": [],
              "repository": repo_r, "zona": os.path.realpath(args.zona),
              "politica_finala": {"daily": int(args.keep_daily),
                                  "weekly": int(args.keep_weekly)},
              "runde": runde,
              "eliminate": [], "inainte": {}, "dupa": {}}

    def pas(nume, ok, detalii=""):
        raport["pasi"].append({"pas": nume, "ok": bool(ok),
                               "detalii": _curata(detalii)})
        if not ok:
            raport["verdict"] = "FAIL"

    def ruleaza(cmd, env_extra=None):
        e = env if env_extra is None else {**env, **env_extra}
        try:
            return subprocess.run(cmd, env=e, capture_output=True,
                                  text=True, timeout=_TIMEOUT)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")
        except OSError as ex:
            return subprocess.CompletedProcess(cmd, 127, "", str(ex))

    def snapshoturi():
        """Snapshoturile legate de sursele probei: id → (time, host)."""
        r = ruleaza([restic_r, "-r", repo_r, "snapshots", "--json"])
        if r.returncode != 0:
            return {}
        try:
            toate = json.loads(r.stdout)
        except (json.JSONDecodeError, TypeError):
            return {}
        ale_nostre = {}
        if isinstance(toate, list):
            for s in toate:
                if isinstance(s, dict) \
                        and surse_r in (s.get("paths") or []):
                    sid = s.get("id") or s.get("short_id")
                    if sid:
                        ale_nostre[sid] = {"time": s.get("time"),
                                           "hostname": s.get("hostname"),
                                           "paths": s.get("paths")}
        return ale_nostre

    def bloburi_fisier(snap_id, nume_fisier):
        """Blob-urile unui fisier dintr-un snapshot, citite din arborele
        snapshotului: `restic cat tree <id>:<dir>` intoarce nodurile cu
        content=bloburi — legatura exacta fisier→date care dovedeste
        ca prune a sters datele nepartajate (cat blob cade dupa)."""
        r = ruleaza([restic_r, "-r", repo_r, "cat", "tree",
                     f"{snap_id}:{surse_r}"])
        if r.returncode != 0:
            return []
        try:
            arbore = json.loads(r.stdout)
        except (json.JSONDecodeError, TypeError):
            return []
        ids = []
        for nod in (arbore.get("nodes") or []):
            if (nod.get("type") == "file"
                    and nod.get("name") == nume_fisier):
                ids.extend(nod.get("content") or [])
        return ids

    try:
        surse_r = os.path.realpath(surse)
        os.makedirs(surse_r, exist_ok=True)
        os.makedirs(os.path.realpath(restore), exist_ok=True)
        os.makedirs(os.path.realpath(out), exist_ok=True)
        os.makedirs(cache_r, exist_ok=True)
        os.makedirs(tmp_r, exist_ok=True)

        with open(os.path.join(surse_r, "sintetic cu spatii.txt"),
                  "w") as f:
            f.write("proba-retentie-f5 fisier cu spatii in nume\n")

        os.makedirs(repo_r, exist_ok=True)
        with open(os.path.join(repo_r, _MARKER), "w") as f:
            f.write(_MARKER_TEXT + "\n")

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

        # --- rundele: acelasi grup host/paths, continut distinct ------
        # Adaptorul face backup si apoi forget --prune IN ACELASI apel,
        # deci ordinea masuratorii e: rundele 1..runde-1 ruleaza cu
        # retentie maxima (grupul creste, nimic uitat) si fiecare
        # snapshot intermediar isi captureaza blob-urile fisierului unic
        # cat inca exista; starea "inainte" se citeste imediat inainte
        # de apelul final, iar "dupa" — dupa prune-ul lui.
        unic = {}          # fisier unic → {snapshot, sha256, bloburi}
        snap_runde = []    # id-urile snapshoturilor in ordinea rundelor
        vazute = set()
        inainte_forget = {}
        for i in range(1, runde + 1):
            fis_unic = f"unic-runda-{i}.txt"
            for sters in os.listdir(surse_r):
                if sters.startswith("unic-runda-"):
                    os.remove(os.path.join(surse_r, sters))
            with open(os.path.join(surse_r, "comun.txt"), "w") as f:
                f.write(f"proba-retentie-f5 continut comun versiunea "
                        f"{i}\n")
            with open(os.path.join(surse_r, fis_unic), "w") as f:
                # continut unic pe runda => blob nepartajat, tinta
                # exacta a eliminarii; determinist, fara aleator
                f.write(f"proba-retentie-f5 unic runda {i} din "
                        f"{runde}\n")
            finala = (i == runde)
            if finala:
                inainte_forget = snapshoturi()
            env_runda = dict(env)
            env_runda["BACKUP_KEEP_DAILY"] = (
                args.keep_daily if finala else str(_RETENTIE_MAX))
            env_runda["BACKUP_KEEP_WEEKLY"] = (
                args.keep_weekly if finala else str(_RETENTIE_MAX))
            r = ruleaza([bash_r, args.adapter], env_extra=env_runda)
            pas(f"runda {i}/{runde}: adaptorul de productie"
                + (" — backup + retentia finala" if finala
                   else " — backup, retentie maxima, grupul creste"),
                r.returncode == 0,
                _curata((r.stderr or r.stdout).strip())[:200])
            if finala:
                bruta = (r.stdout or "") + "\n" + (r.stderr or "")
                raport["iesire_retentie"] = _curata(bruta, 8000)
                raport["contoare_prune"] = re.findall(
                    r"remove[sd]?\s+\d+\s+[^,\n]*"
                    r"|\d+\s+snapshots?\s+(?:have been )?removed"
                    r"|freed\s+[0-9.,]+\s*\w+", bruta)
            if r.returncode != 0:
                raise RuntimeError(f"adaptorul rc={r.returncode} "
                                   f"la runda {i}")
            dupa = snapshoturi()
            noi = sorted(s for s in dupa if s not in vazute)
            pas(f"snapshot nou legat de surse dupa runda {i}",
                len(noi) == 1,
                f"noi={noi} grup={len(dupa)}")
            if len(noi) != 1:
                raise RuntimeError(f"asteptat 1 snapshot nou la runda "
                                   f"{i}, gasite {len(noi)}")
            nou = noi[0]
            vazute.add(nou)
            snap_runde.append(nou)
            if not finala:
                # Snapshotul rundei inca exista (retentie maxima):
                # capturam blob-urile fisierului unic si dovedim ca
                # sunt citibile ACUM — prezenta inainte de prune.
                blobs = bloburi_fisier(nou, fis_unic)
                prezente = [ruleaza([restic_r, "-r", repo_r, "cat",
                                     "blob", b]).returncode == 0
                            for b in blobs]
                unic[fis_unic] = {
                    "snapshot": nou,
                    "sha256_fisier": sha_fisier(
                        os.path.join(surse_r, fis_unic)),
                    "bloburi": blobs}
                pas(f"bloburile unice ale rundei {i} prezente "
                    f"inainte de prune",
                    bool(blobs) and all(prezente),
                    f"{len(blobs)} bloburi, prezente="
                    f"{sum(prezente)}")
                if not blobs or not all(prezente):
                    raise RuntimeError(f"blob unic runda {i} "
                                       "indemonstrabil")

        # --- dovada eliminarii efective -------------------------------
        pastrat = snap_runde[-1]
        de_eliminat = snap_runde[:-1]
        dupa_forget = snapshoturi()
        raport["pastrat"] = pastrat
        raport["inainte"] = {"snapshoturi": de_eliminat,
                             "grup": inainte_forget}
        raport["dupa"] = {"snapshoturi": sorted(dupa_forget)}
        raport["eliminate"] = unic

        pas("grupul avea toate snapshoturile inainte de politica finala",
            len(de_eliminat) >= 1
            and all(s in inainte_forget for s in de_eliminat),
            f"grup={len(inainte_forget)} vizate={len(de_eliminat)}")

        lipsesc = [s for s in de_eliminat if s not in dupa_forget]
        pas("snapshoturile eliminate lipsesc din listarea de dupa",
            len(lipsesc) == len(de_eliminat)
            and pastrat in dupa_forget,
            f"eliminate={len(lipsesc)}/{len(de_eliminat)} "
            f"pastrat={pastrat}")

        for fis, inf in unic.items():
            gone = all(ruleaza([restic_r, "-r", repo_r, "cat", "blob",
                                b]).returncode != 0
                       for b in inf["bloburi"])
            inf["eliminat_prin_prune"] = gone
            pas(f"datele nepartajate ale {fis} eliminate prin prune",
                gone,
                f"snapshot={inf['snapshot']} "
                f"bloburi={inf['bloburi']}")

        contoare = raport.get("contoare_prune") or []
        pas("iesirea si contoarele prune inregistrate",
            bool(contoare),
            f"{len(contoare)} contoare: {str(contoare)[:160]}")

        c = ruleaza([restic_r, "-r", repo_r, "check"])
        pas("restic check dupa prune", c.returncode == 0,
            _curata(c.stderr or c.stdout)[:200])

        r = ruleaza([restic_r, "-r", repo_r, "restore", pastrat,
                     "--target", os.path.realpath(restore)])
        pas("restore al snapshotului pastrat", r.returncode == 0,
            _curata(r.stderr or r.stdout)[:200])
        if r.returncode != 0:
            raise RuntimeError("restore esuat")

        def harta_hash(radacina):
            m = {}
            for rd, _, fisiere in os.walk(radacina):
                for f in fisiere:
                    m[f] = sha_fisier(os.path.join(rd, f))
            return m

        h_surse = harta_hash(surse_r)
        h_rest = harta_hash(os.path.realpath(restore))
        pas("hashurile sursa/restaurare coincid separat",
            h_surse == h_rest,
            f"surse={len(h_surse)} restaurate={len(h_rest)}")
        pas("fisierele rundelor eliminate nu sunt in snapshotul "
            "pastrat", all(f not in h_rest for f in unic),
            f"verificate={list(unic)}")
    except Exception as e:
        pas("lant proba", False, _curata(e))
        raport["verdict"] = "FAIL"

    try:
        with open(os.path.join(os.path.realpath(out),
                               "PROBA-RETENTIE.json"), "w") as f:
            json.dump(raport, f, indent=1, ensure_ascii=False)
    except OSError as e:
        raport["verdict"] = "FAIL"
        raport["pasi"].append({"pas": "scriere raport", "ok": False,
                               "detalii": _curata(e)})
    print(json.dumps(raport, ensure_ascii=False))
    return 0 if raport["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
