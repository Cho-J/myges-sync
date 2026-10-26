"""
source.py — Partie spécifique : récupération du planning depuis l'API REST.

C'est le seul fichier à remplacer pour brancher une autre source. Le reste du
synchroniseur (agenda_sync.py) n'attend qu'un objet qui expose :

    source.fetch_events() -> dict[str, dict]

Le dictionnaire renvoyé associe un UID stable à un événement normalisé, avec
les clés suivantes :

    uid, type, name, teacher, modality, comment, group, campus,
    rooms (list[str]), all_rooms (list[str]),
    start_ms, end_ms (int, millisecondes UTC), geo (tuple[float, float] | None)

L'implémentation ci-dessous cible une API de planning scolaire :
authentification OAuth implicite (jeton dans le fragment de redirection), puis
lecture de l'agenda sur une plage de dates.
"""

import logging
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger("agenda-sync")


def clean(value) -> str:
    """Normalise un champ texte : None → '', espaces parasites supprimés."""
    if value is None:
        return ""
    return str(value).strip()


class ScheduleSource:
    """Interface minimale d'une source de planning."""

    def fetch_events(self) -> dict:
        raise NotImplementedError


class RestApiSource(ScheduleSource):
    """Source : API REST de planning avec authentification OAuth implicite."""

    def __init__(self, auth_url: str, agenda_url: str, user: str, password: str,
                 range_start: datetime, range_end: datetime,
                 uid_domain: str, user_agent: str):
        self.auth_url = auth_url
        self.agenda_url = agenda_url
        self.user = user
        self.password = password
        self.range_start = range_start
        self.range_end = range_end
        self.uid_domain = uid_domain
        self.user_agent = user_agent

    # ───────────────────────── Accès à l'API ─────────────────────────

    def get_token(self) -> str:
        """Auth Basic → 302 → jeton dans le fragment de l'en-tête Location."""
        resp = requests.get(self.auth_url, auth=(self.user, self.password),
                            headers={"User-Agent": self.user_agent},
                            allow_redirects=False, timeout=30)
        if resp.status_code != 302:
            raise RuntimeError(
                f"Authentification refusée par la source (HTTP {resp.status_code})")

        location = resp.headers.get("Location", "")
        fragment = urlparse(location).fragment
        token = parse_qs(fragment).get("access_token", [None])[0]
        if not token:
            raise RuntimeError("Jeton absent de la redirection OAuth")
        return token

    def fetch_agenda(self, token: str) -> list:
        """Récupère les événements sur la plage configurée. Filtre les entrées non-objet."""
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": self.user_agent,
            "accept-encoding": "gzip",
        }
        params = {
            "start": int(self.range_start.timestamp() * 1000),
            "end": int(self.range_end.timestamp() * 1000),
        }
        resp = requests.get(self.agenda_url, headers=headers, params=params, timeout=60)
        resp.raise_for_status()

        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("Réponse de la source illisible (JSON invalide)") from exc

        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            raise RuntimeError("Réponse de la source malformée : clé 'result' absente ou invalide")

        # Le tableau peut contenir des chaînes parasites → on ne garde que les objets
        return [e for e in data["result"] if isinstance(e, dict)]

    # ───────────────────────── Normalisation ─────────────────────────

    def build_desired_events(self, raw_events: list) -> dict:
        """
        Transforme la réponse de l'API en dictionnaire {uid: event_dict}.
        Un événement couvrant plusieurs campus produit un VEVENT par campus.
        """
        desired = {}

        for raw in raw_events:
            rid = raw.get("reservation_id")
            start_ms = raw.get("start_date")
            end_ms = raw.get("end_date")
            if rid is None or start_ms is None or end_ms is None:
                log.warning("Événement ignoré (champs essentiels manquants) : %r",
                            {k: raw.get(k) for k in ("reservation_id", "name", "type")})
                continue

            etype = clean(raw.get("type")) or "Cours"
            name = clean(raw.get("name")) or "Sans intitulé"
            teacher = clean(raw.get("teacher"))
            modality = clean(raw.get("modality"))
            comment = clean(raw.get("comment"))

            discipline = raw.get("discipline") or {}
            group = clean(discipline.get("student_group_name"))

            rooms = [r for r in (raw.get("rooms") or []) if isinstance(r, dict)]
            all_room_names = sorted({clean(r.get("name")) for r in rooms
                                     if clean(r.get("name"))})

            # Regroupement des salles par campus (cas multi-campus prévu)
            by_campus = {}
            for room in rooms:
                campus = clean(room.get("campus"))
                by_campus.setdefault(campus or "SANSCAMPUS", []).append(room)
            if not by_campus:
                by_campus = {None: []}

            for campus, campus_rooms in by_campus.items():
                uid = (f"Class_{rid}_{campus}@{self.uid_domain}" if campus
                       else f"Class_{rid}@{self.uid_domain}")

                room_names = sorted({clean(r.get("name")) for r in campus_rooms
                                     if clean(r.get("name"))})

                # Coordonnées GPS : première salle du campus qui en possède
                geo = None
                for room in campus_rooms:
                    lat, lon = room.get("latitude"), room.get("longitude")
                    if lat and lon:
                        try:
                            geo = (float(lat), float(lon))
                        except (TypeError, ValueError):
                            geo = None
                        break

                desired[uid] = {
                    "uid": uid,
                    "rid": rid,
                    "type": etype,
                    "name": name,
                    "teacher": teacher,
                    "modality": modality,
                    "comment": comment,
                    "group": group,
                    "campus": campus if campus and campus != "SANSCAMPUS" else "",
                    "rooms": room_names,          # salles de CE campus
                    "all_rooms": all_room_names,  # toutes salles (utile examens)
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "geo": geo,
                }

        return desired

    # ───────────────────────── Point d'entrée ─────────────────────────

    def fetch_events(self) -> dict:
        token = self.get_token()
        log.info("Authentification auprès de la source OK")

        raw_events = self.fetch_agenda(token)
        log.info("Source : %d événement(s) reçu(s)", len(raw_events))

        # Garde-fou n°1 : réponse vide → on ne touche à rien
        if not raw_events:
            raise RuntimeError(
                "La source a renvoyé 0 événement — abandon par sécurité "
                "(garde-fou anti-effacement du calendrier)")

        desired = self.build_desired_events(raw_events)
        log.info("Après normalisation : %d VEVENT attendus", len(desired))
        return desired
