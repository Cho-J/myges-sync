#!/usr/bin/env python3
"""
agenda_sync.py — Synchronisation d'un planning (API REST) vers un calendrier CalDAV.

Fonctionnement :
  1. Récupération des événements auprès de la source (source.py)
  2. Lecture de l'état actuel du calendrier CalDAV
  3. Diff par UID déterministe : création / mise à jour (via hash de contenu) / suppression
  4. Notification ntfy en cas d'échec, et lorsque le planning évolue

Usage :
  ./agenda_sync.py            # synchronisation réelle
  ./agenda_sync.py --dry-run  # affiche ce qui serait fait, n'écrit rien

Configuration et identifiants : fichier externe au format KEY='valeur'
(chemin dans la variable d'environnement AGENDA_SYNC_CONFIG), voir .env.example.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import caldav
import requests
from icalendar import Alarm
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar import vGeo

from source import RestApiSource

# ─────────────────────────── Configuration ────────────────────────────

CONFIG_FILE = os.environ.get("AGENDA_SYNC_CONFIG", "/opt/agenda-sync/agenda-sync-creds")

# Clés obligatoires du fichier de configuration (secrets compris)
REQUIRED_KEYS = [
    "SOURCE_AUTH_URL", "SOURCE_AGENDA_URL", "SOURCE_USER", "SOURCE_PASSWORD",
    "CALDAV_URL", "CALDAV_USER", "CALDAV_PASSWORD", "CALENDAR_NAME",
    "NTFY_URL", "NTFY_TOKEN",
    "RANGE_START", "RANGE_END",
]

# Valeurs par défaut des clés facultatives
DEFAULTS = {
    "NTFY_TOPIC_FAILURE": "sync-errors",   # échecs du script → problème d'infrastructure
    "NTFY_TOPIC_CHANGES": "sync-changes",  # évolutions du planning → information
    "LOG_FILE": "/opt/agenda-sync/logs/agenda-sync.log",
    "UID_DOMAIN": "agenda-sync",
    "SOURCE_USER_AGENT": "agenda-sync",
}

# Rappels avant événement, selon le type (clé = préfixe du champ `type`, insensible à la casse)
ALARM_DEFAULT = timedelta(minutes=30)          # Cours, Autre, etc.
ALARM_BY_TYPE = {
    "examen": timedelta(hours=24),
    "soutenance": timedelta(hours=24),
}

# Garde-fou anti-effacement massif : au-delà de ce ratio de suppressions
# (si le calendrier contient au moins MIN_EVENTS_FOR_GUARD événements), on abandonne.
MAX_DELETE_RATIO = 0.5
MIN_EVENTS_FOR_GUARD = 10

HASH_PROPERTY = "X-SYNC-HASH"

log = logging.getLogger("agenda-sync")


# ─────────────────────────── Utilitaires ──────────────────────────────

def load_config(path: str) -> dict:
    """Parse un fichier de configuration au format shell KEY='valeur'."""
    config = dict(DEFAULTS)
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip("'\"")

    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing:
        raise RuntimeError(f"Clés manquantes dans {path} : {', '.join(missing)}")
    return config


def parse_date(value: str) -> datetime:
    """AAAA-MM-JJ → datetime UTC à minuit."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def notify_failure(config: dict, message: str) -> None:
    """Envoie une notification d'échec sur ntfy. Ne lève jamais d'exception."""
    try:
        requests.post(
            f"{config['NTFY_URL']}/{config['NTFY_TOPIC_FAILURE']}",
            data=message.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config['NTFY_TOKEN']}",
                "Title": "Echec de la synchronisation du planning",
                "Priority": "high",
                "Tags": "warning,calendar",
            },
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — la notif ne doit pas masquer l'erreur d'origine
        log.error("Impossible d'envoyer la notification ntfy : %s", exc)


def notify_changes(config: dict, created: int, updated: int, deleted: int) -> None:
    """Notifie les changements réels. Silencieux si rien n'a bougé."""
    if not (created or updated or deleted):
        return

    parts = []
    if created:
        parts.append(f"{created} nouveau(x) cours")
    if updated:
        parts.append(f"{updated} modifié(s)")
    if deleted:
        parts.append(f"{deleted} annulé(s)")

    try:
        requests.post(
            f"{config['NTFY_URL']}/{config['NTFY_TOPIC_CHANGES']}",
            data=" · ".join(parts).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config['NTFY_TOKEN']}",
                "Title": "Planning mis à jour",
                "Priority": "4",
                "Tags": "calendar",
            },
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Notification ntfy en échec : %s", exc)


# ─────────────────── Construction des événements iCal ─────────────────

def event_kind(etype: str) -> str:
    """Classe le type d'événement : 'examen', 'soutenance' ou 'cours' (défaut)."""
    low = etype.lower()
    if low.startswith("examen"):
        return "examen"
    if low.startswith("soutenance"):
        return "soutenance"
    return "cours"


def alarm_delay(etype: str) -> timedelta:
    return ALARM_BY_TYPE.get(event_kind(etype), ALARM_DEFAULT)


def event_summary(ev: dict) -> str:
    """Titre lisible sur mobile. Préfixe pour examens/soutenances."""
    kind = event_kind(ev["type"])
    prefix = {"examen": "[EXAM] ", "soutenance": "[SOUT] "}.get(kind, "")

    rooms = ev["rooms"]
    if not rooms:
        suffix = ""
    elif len(rooms) == 1:
        suffix = f" — {rooms[0]}"
    else:
        # Plusieurs salles (typique des examens) : première + compteur
        suffix = f" — {rooms[0]} (+{len(rooms) - 1})"

    return f"{prefix}{ev['name']}{suffix}"


def event_description(ev: dict) -> str:
    lines = [f"Intitulé : {ev['name']}", f"Type : {ev['type']}"]
    if ev["modality"]:
        lines.append(f"Modalité : {ev['modality']}")
    if ev["teacher"]:
        lines.append(f"Enseignant : {ev['teacher']}")
    if ev["group"]:
        lines.append(f"Classe : {ev['group']}")
    if ev["comment"]:
        lines.append(f"Commentaire : {ev['comment']}")

    kind = event_kind(ev["type"])
    if kind in ("examen", "soutenance") and ev["all_rooms"]:
        lines.append(f"Salles mobilisées : {', '.join(ev['all_rooms'])}")
        lines.append("⚠ Toutes les salles de l'épreuve sont listées — "
                     "le dispatch nominatif est envoyé séparément par l'école.")
    elif ev["rooms"]:
        lines.append(f"Salle(s) : {', '.join(ev['rooms'])}")

    return "\n".join(lines)


def event_location(ev: dict) -> str:
    parts = []
    if ev["rooms"]:
        parts.append(", ".join(ev["rooms"]))
    if ev["campus"]:
        parts.append(ev["campus"])
    return " — ".join(parts)


def content_hash(ev: dict) -> str:
    """
    Empreinte du contenu métier. Si elle change, l'événement est mis à jour.
    Le délai d'alarme est inclus : modifier la config propage la mise à jour.
    """
    payload = {
        "name": ev["name"], "type": ev["type"], "teacher": ev["teacher"],
        "modality": ev["modality"], "comment": ev["comment"], "group": ev["group"],
        "campus": ev["campus"], "rooms": ev["rooms"], "all_rooms": ev["all_rooms"],
        "start": ev["start_ms"], "end": ev["end_ms"], "geo": ev["geo"],
        "alarm_s": int(alarm_delay(ev["type"]).total_seconds()),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def build_ics(ev: dict) -> str:
    """Construit un VCALENDAR/VEVENT complet pour un événement."""
    cal = ICalendar()
    cal.add("prodid", "-//agenda-sync//example.lan//FR")
    cal.add("version", "2.0")

    vev = IEvent()
    vev.add("uid", ev["uid"])
    vev.add("dtstamp", datetime.now(timezone.utc))
    # Timestamps de la source = UTC pur → on émet en UTC (suffixe Z),
    # le client CalDAV affiche dans son fuseau. Aucun décalage ajouté.
    vev.add("dtstart", datetime.fromtimestamp(ev["start_ms"] / 1000, tz=timezone.utc))
    vev.add("dtend", datetime.fromtimestamp(ev["end_ms"] / 1000, tz=timezone.utc))
    vev.add("summary", event_summary(ev))
    vev.add("description", event_description(ev))

    location = event_location(ev)
    if location:
        vev.add("location", location)
    if ev["geo"]:
        vev.add("geo", vGeo(ev["geo"]))
    vev.add("categories", [ev["type"]])
    vev[HASH_PROPERTY] = content_hash(ev)

    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", event_summary(ev))
    alarm.add("trigger", -alarm_delay(ev["type"]))
    vev.add_component(alarm)

    cal.add_component(vev)
    return cal.to_ical().decode("utf-8")


# ─────────────────────────── Côté CalDAV ──────────────────────────────

def get_calendar(config: dict):
    client = caldav.DAVClient(
        url=config["CALDAV_URL"],
        username=config["CALDAV_USER"],
        password=config["CALDAV_PASSWORD"],
    )
    principal = client.principal()
    name = config["CALENDAR_NAME"]
    try:
        return principal.calendar(name=name)
    except caldav.lib.error.NotFoundError as exc:
        raise RuntimeError(f"Calendrier '{name}' introuvable sur le serveur CalDAV") from exc


def load_remote_events(calendar) -> dict:
    """Retourne {uid: (objet_caldav, hash)} pour tous les VEVENT du calendrier."""
    remote = {}
    for obj in calendar.events():
        try:
            parsed = ICalendar.from_ical(obj.data)
        except Exception as exc:  # noqa: BLE001
            log.warning("Événement distant illisible, ignoré : %s", exc)
            continue

        for comp in parsed.walk("VEVENT"):
            uid = str(comp.get("UID", ""))
            if not uid:
                continue
            ev_hash = str(comp.get(HASH_PROPERTY, ""))
            if uid in remote:
                log.warning("UID en double côté CalDAV : %s", uid)
                continue
            remote[uid] = (obj, ev_hash)
    return remote


# ─────────────────────────── Synchronisation ──────────────────────────

def build_source(config: dict) -> RestApiSource:
    """Seul point de couplage avec la source : à adapter si source.py est remplacé."""
    return RestApiSource(
        auth_url=config["SOURCE_AUTH_URL"],
        agenda_url=config["SOURCE_AGENDA_URL"],
        user=config["SOURCE_USER"],
        password=config["SOURCE_PASSWORD"],
        range_start=parse_date(config["RANGE_START"]),
        range_end=parse_date(config["RANGE_END"]),
        uid_domain=config["UID_DOMAIN"],
        user_agent=config["SOURCE_USER_AGENT"],
    )


def sync(dry_run: bool, config: dict) -> None:
    log.info("=== Démarrage (plage %s → %s)%s ===",
             config["RANGE_START"], config["RANGE_END"],
             " [DRY-RUN]" if dry_run else "")

    # Garde-fou n°1 (réponse vide) appliqué dans la source
    desired = build_source(config).fetch_events()

    calendar = get_calendar(config)
    remote = load_remote_events(calendar)
    log.info("CalDAV : %d événement(s) présents dans '%s'",
             len(remote), config["CALENDAR_NAME"])

    to_create = [uid for uid in desired if uid not in remote]
    to_update = [uid for uid in desired
                 if uid in remote and remote[uid][1] != content_hash(desired[uid])]
    to_delete = [uid for uid in remote if uid not in desired]

    # Garde-fou n°2 : suppression massive anormale → abandon
    if (len(remote) >= MIN_EVENTS_FOR_GUARD
            and len(to_delete) / len(remote) > MAX_DELETE_RATIO):
        raise RuntimeError(
            f"Suppression anormale demandée ({len(to_delete)}/{len(remote)} "
            f"événements, seuil {MAX_DELETE_RATIO:.0%}) — abandon par sécurité")

    log.info("Diff : %d création(s), %d mise(s) à jour, %d suppression(s)",
             len(to_create), len(to_update), len(to_delete))

    for uid in to_create:
        ev = desired[uid]
        log.info("CRÉATION   %s | %s", uid, event_summary(ev))
        if not dry_run:
            calendar.save_event(build_ics(ev))

    for uid in to_update:
        ev = desired[uid]
        log.info("MISE À JOUR %s | %s", uid, event_summary(ev))
        if not dry_run:
            obj = remote[uid][0]
            obj.data = build_ics(ev)
            obj.save()

    for uid in to_delete:
        log.info("SUPPRESSION %s", uid)
        if not dry_run:
            remote[uid][0].delete()

    if not dry_run:
        notify_changes(config, len(to_create), len(to_update), len(to_delete))

    log.info("=== Terminé sans erreur%s ===", " [DRY-RUN]" if dry_run else "")


# ─────────────────────────── Point d'entrée ───────────────────────────

def setup_logging(dry_run: bool, log_file: str) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    if not dry_run:
        try:
            file_handler = RotatingFileHandler(
                log_file, maxBytes=500_000, backupCount=3, encoding="utf-8")
            file_handler.setFormatter(fmt)
            log.addHandler(file_handler)
        except OSError as exc:
            log.warning("Journal fichier indisponible (%s), stdout uniquement", exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronisation planning (API REST) → CalDAV")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche les actions sans rien écrire dans le calendrier")
    args = parser.parse_args()

    config = None
    try:
        config = load_config(CONFIG_FILE)
        setup_logging(args.dry_run, config["LOG_FILE"])
        sync(args.dry_run, config)
        return 0
    except Exception as exc:  # noqa: BLE001
        if not log.handlers:
            setup_logging(True, "")
        log.exception("Échec de la synchronisation : %s", exc)
        if config and config.get("NTFY_TOKEN"):
            notify_failure(config, f"Synchronisation planning → CalDAV en échec :\n{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
