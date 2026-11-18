"""Mandat de vol : ce que l'opérateur autorise l'agent à faire, signé en Ed25519."""

import base64
import dataclasses
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cached_property

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..canonical import canonical_json, sha256_hex
from ..geo import haversine_m, offset_latlon
from . import geometry

# Vocabulaire des actions = noms des outils vus par l'agent.
ACTIONS = frozenset({"takeoff", "goto", "hover", "land", "return_to_launch", "set_parameter"})
# Actions de RETOUR : un sous-mandat ne peut pas les retirer (voir derive()).
RETURN_ACTIONS = frozenset({"land", "return_to_launch"})


class MandateError(ValueError):
    """Mandat invalide, mal signé, ou sous-mandat qui élargirait les droits."""


def _parse_time(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise MandateError(f"date sans fuseau horaire : {s}")
    return dt.astimezone(timezone.utc)


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class LandingZone:
    lat: float
    lon: float
    rayon_m: float

    def contains(self, lat: float, lon: float) -> bool:
        return haversine_m(lat, lon, self.lat, self.lon) <= self.rayon_m


@dataclass(frozen=True)
class Mandate:
    id_mandat: str
    id_operateur: str
    emis_le: datetime
    expiration: datetime
    zone: tuple[tuple[float, float], ...]
    altitude_max_m: float
    duree_max_s: float
    actions_permises: frozenset[str]
    zones_atterrissage: tuple[LandingZone, ...] | None = None
    parent: str | None = None

    # ------------------------------------------------------------ (dé)sérialisation

    def to_dict(self) -> dict:
        return {
            "id_mandat": self.id_mandat,
            "id_operateur": self.id_operateur,
            "emis_le": _fmt_time(self.emis_le),
            "expiration": _fmt_time(self.expiration),
            "zone": [[lat, lon] for lat, lon in self.zone],
            "altitude_max_m": self.altitude_max_m,
            "duree_max_s": self.duree_max_s,
            "actions_permises": sorted(self.actions_permises),
            "zones_atterrissage": None if self.zones_atterrissage is None
            else [dataclasses.asdict(z) for z in self.zones_atterrissage],
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Mandate":
        """Construit ET valide un mandat. Toute anomalie lève MandateError."""
        expected = {"id_mandat", "id_operateur", "emis_le", "expiration", "zone", "altitude_max_m",
                    "duree_max_s", "actions_permises", "zones_atterrissage", "parent"}
        if set(d) != expected:
            raise MandateError(f"champs inattendus ou manquants : {sorted(set(d) ^ expected)}")
        try:
            zone = tuple((float(lat), float(lon)) for lat, lon in d["zone"])
            landing = None if d["zones_atterrissage"] is None else tuple(
                LandingZone(float(z["lat"]), float(z["lon"]), float(z["rayon_m"])) for z in d["zones_atterrissage"])
            m = cls(id_mandat=str(d["id_mandat"]), id_operateur=str(d["id_operateur"]),
                    emis_le=_parse_time(d["emis_le"]), expiration=_parse_time(d["expiration"]),
                    zone=zone, altitude_max_m=float(d["altitude_max_m"]), duree_max_s=float(d["duree_max_s"]),
                    actions_permises=frozenset(d["actions_permises"]), zones_atterrissage=landing,
                    parent=d["parent"])
        except (TypeError, ValueError, KeyError) as e:
            raise MandateError(f"mandat mal formé : {e}") from e
        m.validate()
        return m

    def validate(self) -> None:
        numbers = [self.altitude_max_m, self.duree_max_s, *(c for p in self.zone for c in p)]
        if self.zones_atterrissage:
            numbers += [c for z in self.zones_atterrissage for c in (z.lat, z.lon, z.rayon_m)]
        if not all(math.isfinite(x) for x in numbers):
            raise MandateError("valeur numérique non finie (NaN ou infini)")
        if self.altitude_max_m <= 0 or self.duree_max_s <= 0:
            raise MandateError("altitude_max_m et duree_max_s doivent être positives")
        if not self.actions_permises <= ACTIONS:
            raise MandateError(f"actions inconnues : {sorted(self.actions_permises - ACTIONS)}")
        if self.expiration <= self.emis_le:
            raise MandateError("expiration antérieure à l'émission")
        if not geometry.polygon_is_simple(self._local_zone):
            raise MandateError("la zone doit être un polygone simple (≥ 3 sommets, sans croisement)")
        for z in self.zones_atterrissage or ():
            if z.rayon_m <= 0 or not self.contains(z.lat, z.lon):
                raise MandateError("zone d'atterrissage invalide ou hors de la zone autorisée")

    def fingerprint(self) -> str:
        """Empreinte SHA-256 du mandat : identifie de façon unique son contenu exact."""
        return sha256_hex(self.to_dict())

    # ------------------------------------------------------------ géométrie

    @cached_property
    def _ref(self) -> tuple[float, float]:
        """Origine du repère local : la moyenne des sommets de la zone."""
        return (sum(p[0] for p in self.zone) / len(self.zone), sum(p[1] for p in self.zone) / len(self.zone))

    @cached_property
    def _local_zone(self) -> list[geometry.Point]:
        return [geometry.to_local(lat, lon, self._ref) for lat, lon in self.zone]

    def contains(self, lat: float, lon: float) -> bool:
        return geometry.point_in_polygon(geometry.to_local(lat, lon, self._ref), self._local_zone)

    def path_inside(self, start: tuple[float, float], end: tuple[float, float]) -> bool:
        return geometry.segment_in_polygon(geometry.to_local(*start, self._ref),
                                           geometry.to_local(*end, self._ref), self._local_zone)

    def landing_allowed(self, lat: float, lon: float) -> bool:
        if not self.contains(lat, lon):
            return False
        return self.zones_atterrissage is None or any(z.contains(lat, lon) for z in self.zones_atterrissage)

    # ------------------------------------------------------------ sous-mandat

    def widening_reasons(self, parent: "Mandate") -> list[str]:
        """Liste des points sur lesquels ce mandat serait PLUS large que `parent` (vide = OK)."""
        reasons = []
        if self.id_operateur != parent.id_operateur:
            reasons.append("opérateur différent")
        if self.parent != parent.fingerprint():
            reasons.append("le champ parent ne désigne pas le mandat parent")
        if self.altitude_max_m > parent.altitude_max_m:
            reasons.append(f"altitude {self.altitude_max_m:g} m > {parent.altitude_max_m:g} m")
        if self.duree_max_s > parent.duree_max_s:
            reasons.append(f"durée {self.duree_max_s:g} s > {parent.duree_max_s:g} s")
        if self.expiration > parent.expiration:
            reasons.append("expiration plus tardive")
        if not self.actions_permises <= parent.actions_permises:
            reasons.append(f"actions ajoutées : {sorted(self.actions_permises - parent.actions_permises)}")
        inner = [geometry.to_local(lat, lon, parent._ref) for lat, lon in self.zone]
        if not geometry.polygon_in_polygon(inner, parent._local_zone):
            reasons.append("zone non incluse dans la zone parente")
        if parent.zones_atterrissage is not None:
            if self.zones_atterrissage is None:
                reasons.append("atterrissage partout alors que le parent le limite")
            else:
                for z in self.zones_atterrissage:
                    if not any(haversine_m(z.lat, z.lon, p.lat, p.lon) + z.rayon_m <= p.rayon_m
                               for p in parent.zones_atterrissage):
                        reasons.append(f"zone d'atterrissage ({z.lat:.6f}, {z.lon:.6f}) hors des zones parentes")
        return reasons

    def derive(self, changes: dict) -> "Mandate":
        """Crée un sous-mandat en ne changeant que `changes`. Lève MandateError s'il élargit."""
        allowed = {"zone", "altitude_max_m", "duree_max_s", "actions_permises", "zones_atterrissage", "expiration"}
        unknown = set(changes) - allowed
        if unknown:
            raise MandateError(f"champs non modifiables : {sorted(unknown)}")
        d = self.to_dict()
        d.update(changes)
        d["id_mandat"] = f"{self.id_mandat}/sous-{uuid.uuid4().hex[:6]}"
        d["parent"] = self.fingerprint()
        child = Mandate.from_dict(d)
        reasons = child.widening_reasons(self)
        if reasons:
            raise MandateError("élargissement refusé : " + "; ".join(reasons))
        removed = (self.actions_permises & RETURN_ACTIONS) - child.actions_permises
        if removed:
            raise MandateError(f"une restriction ne peut pas retirer les actions de retour {sorted(removed)} : "
                               "le drone doit toujours pouvoir rentrer et se poser")
        same = {k: v for k, v in child.to_dict().items() if k not in ("id_mandat", "parent")}
        if same == {k: v for k, v in self.to_dict().items() if k not in ("id_mandat", "parent")}:
            raise MandateError("restriction sans effet : le mandat demandé est identique au mandat actif "
                               "(une restriction ne peut qu'enlever des droits, jamais en ajouter)")
        return child

    # ------------------------------------------------------------ texte pour l'agent

    def describe(self) -> str:
        zone = ", ".join(f"({lat:.7f}, {lon:.7f})" for lat, lon in self.zone)
        if self.zones_atterrissage is None:
            landing = "partout dans la zone autorisée"
        else:
            landing = "uniquement dans : " + ", ".join(
                f"un cercle de {z.rayon_m:g} m autour de ({z.lat:.7f}, {z.lon:.7f})" for z in self.zones_atterrissage)
        return (f"Mandat de vol de l'opérateur {self.id_operateur} (id {self.id_mandat}) :\n"
                f"- zone autorisée : polygone de sommets (latitude, longitude) {zone}\n"
                f"- altitude maximale : {self.altitude_max_m:g} m au-dessus du point de décollage\n"
                f"- durée maximale de la mission : {self.duree_max_s:g} s\n"
                f"- actions permises : {', '.join(sorted(self.actions_permises))}\n"
                f"- atterrissage autorisé {landing}\n"
                f"- expire le {_fmt_time(self.expiration)}\n"
                "Tu peux demander une restriction de ce mandat avec request_restriction, jamais un élargissement.")


# ---------------------------------------------------------------- signature

def sign_mandate(mandate: Mandate, private_key: Ed25519PrivateKey) -> dict:
    signature = private_key.sign(canonical_json(mandate.to_dict()))
    return {"mandat": mandate.to_dict(), "signature": {"alg": "Ed25519", "valeur": base64.b64encode(signature).decode()}}


def verify_signed_mandate(doc: dict, trusted_keys: dict[str, Ed25519PublicKey]) -> Mandate:
    """Vérifie la signature avec la clé de l'opérateur déclaré, puis valide le contenu."""
    try:
        payload, sig = doc["mandat"], doc["signature"]
        if sig.get("alg") != "Ed25519":
            raise MandateError(f"algorithme de signature non supporté : {sig.get('alg')!r}")
        signature = base64.b64decode(sig["valeur"], validate=True)
        operator = payload["id_operateur"]
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        raise MandateError(f"document de mandat mal formé : {e}") from e
    key = trusted_keys.get(operator)
    if key is None:
        raise MandateError(f"opérateur inconnu : {operator!r}")
    try:
        key.verify(signature, canonical_json(payload))
    except InvalidSignature:
        raise MandateError("signature invalide : mandat modifié ou non signé par cet opérateur") from None
    return Mandate.from_dict(payload)


def default_mandate(home: tuple[float, float], operator_id: str, now: datetime | None = None,
                    validity: timedelta = timedelta(hours=2)) -> Mandate:
    """Mandat de la mission de démonstration, construit autour du home."""
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    corners = [(-50, -50), (-50, 80), (80, 80), (80, -50)]  # (nord, est) en mètres
    zone = tuple(tuple(round(c, 7) for c in offset_latlon(*home, n, e)) for n, e in corners)
    return Mandate(
        id_mandat=f"m-{uuid.uuid4().hex[:8]}", id_operateur=operator_id, emis_le=now, expiration=now + validity,
        zone=zone, altitude_max_m=50.0, duree_max_s=900.0,
        actions_permises=frozenset({"takeoff", "goto", "hover", "land", "return_to_launch"}),
        zones_atterrissage=(LandingZone(round(home[0], 7), round(home[1], 7), 5.0),),
    )
