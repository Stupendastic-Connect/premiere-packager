#!/usr/bin/env python3
"""
empaquetar_premiere.py - Empaquetador de proyectos Adobe Premiere Pro en lote.

Automatiza el proceso de "Project Manager > Collect Files" sin abrir Premiere.
Parsea el grafo de objetos del .prproj para copiar solo los medios de la
secuencia seleccionada (incluidas secuencias anidadas).

Modos de seleccion de secuencia:
  - Interactivo (defecto): muestra tabla rankeada, el usuario confirma
  - --auto: usa la mejor candidata sin preguntar
  - --sequence "patron": selecciona por nombre (soporta * como comodin)
  - --all: empaqueta todos los medios sin filtrar por secuencia

Uso:
    python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --dry-run
    python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --auto
    python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --sequence "*boda*"
    python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --all

Traduccion de rutas Mac a Windows (proyectos creados en Mac, medios en drives Windows):
    python empaquetar_premiere.py "V:/Proyectos" "E:/Backup" --map "/Volumes/SEGUIMIENTOS=V:" --map "/Volumes/Dropbox=D:/Dropbox"
"""

import argparse
import fnmatch
import gzip
import logging
import os
import posixpath
import re
import shutil
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

TICKS_PER_SECOND = 254_016_000_000

# Secuencias auto-generadas por Premiere que se ocultan de la lista
SKIP_PATTERNS = [
    "secuencia anidada",
    "nested sequence",
]

# Secuencias con este numero de clips o menos se consideran nests internos
MIN_CLIPS_THRESHOLD = 2

# Nombres que bajan la puntuacion (deliverables secundarios / redes)
DEMOTE_PATTERNS = [
    "reel", "redes", "instagram", "ig", "tiktok", "shorts", "stories",
    "story", "trailer", "teaser", "promo", "bts", "behind",
    "selects", "seleccion", "bruto", "brutos", "raw", "rushes",
    "test", "prueba", "borrador", "draft", "temp", "old", "copia",
    "backup", "pre-edit", "preedit",
]

# Nombres que suben la puntuacion
PROMOTE_PATTERNS = [
    "final", "master", "main", "principal", "entrega", "delivery",
    "export", "online", "conform", "edit", "cut",
]

# ContentAndMetadataState todo ceros = medio sintetico (barras, tono, etc.)
SYNTHETIC_MEDIA_STATE = "00000000-0000-0000-0000-000000000000"

# Extensiones de proyecto After Effects
AE_PROJECT_EXTENSIONS = frozenset({".aep", ".aepx"})


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("empaquetar")


# ---------------------------------------------------------------------------
# Traduccion de rutas Mac -> Windows
# ---------------------------------------------------------------------------

def parse_path_mappings(map_args: list[str] | None) -> list[tuple[str, str]]:
    """Parsea los argumentos --map en pares (mac_prefix, windows_prefix).
    Formato: "/Volumes/SEGUIMIENTOS=V:" """
    if not map_args:
        return []
    mappings = []
    for entry in map_args:
        if "=" not in entry:
            raise ValueError(f"Formato de --map invalido (falta '='): {entry}")
        mac, win = entry.split("=", 1)
        # Normalizar: sin trailing slash
        mac = mac.rstrip("/").rstrip("\\")
        win = win.rstrip("/").rstrip("\\")
        mappings.append((mac, win))
    # Ordenar por longitud descendente (match mas especifico primero)
    mappings.sort(key=lambda x: len(x[0]), reverse=True)
    return mappings


def translate_path(mac_path: str, mappings: list[tuple[str, str]]) -> str:
    """Traduce una ruta Mac a Windows usando los mapeos.
    Si no hay match, devuelve la ruta original."""
    if not mappings:
        return mac_path
    for mac_prefix, win_prefix in mappings:
        if mac_path.startswith(mac_prefix + "/") or mac_path == mac_prefix:
            remainder = mac_path[len(mac_prefix):]
            # Convertir slashes y limpiar /./  /../
            translated = win_prefix + remainder.replace("/", "\\")
            # Normalizar . y .. en la ruta
            return str(Path(translated))
    return mac_path


def normalize_media_path(path_str: str) -> str:
    """Normaliza rutas con /./  /../ y barras inconsistentes.
    Tambien aplica NFC Unicode (Mac usa NFD, Windows usa NFC)."""
    # Mac almacena acentos como NFD (o + combining acute) -> NFC (ó)
    path_str = unicodedata.normalize("NFC", path_str)
    # Detectar si es ruta Mac/Unix
    if path_str.startswith("/"):
        normalized = posixpath.normpath(path_str)
        return normalized
    # Ruta Windows
    return str(Path(path_str))


# ---------------------------------------------------------------------------
# Lectura / escritura de .prproj
# ---------------------------------------------------------------------------

def read_prproj(path: Path) -> bytes:
    """Lee un .prproj y devuelve el XML sin comprimir."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def write_prproj(path: Path, xml_bytes: bytes) -> None:
    """Comprime XML con gzip y lo guarda como .prproj."""
    with gzip.open(path, "wb") as f:
        f.write(xml_bytes)


# ---------------------------------------------------------------------------
# Motor de grafo de objetos del .prproj
# ---------------------------------------------------------------------------

class PrprojGraph:
    """Parsea el XML plano de un .prproj y permite navegar el grafo de objetos
    mediante ObjectID/ObjectRef y ObjectUID/ObjectURef.

    Estructura real del XML (todos los objetos son hijos directos de
    <PremiereData>, enlazados por referencias):

      Sequence
        → TrackGroups/TrackGroup/Second (ObjectRef) → VideoTrackGroup|AudioTrackGroup
          → TrackGroup/Tracks/Track (ObjectRef) → VideoClipTrack|AudioClipTrack
            → ClipTrack/ClipItems/TrackItems/TrackItem (ObjectRef) → VideoClipTrackItem
              → ClipTrackItem/SubClip (ObjectRef) → SubClip
                → Clip/Clip (ObjectRef) → VideoClip|AudioClip
                  → Source (ObjectRef) →
                      VideoMediaSource → MediaSource/Media (ObjectRef) → Media
                                           → ActualMediaFilePath (ruta del archivo)
                      VideoSequenceSource → SequenceSource/Sequence (ObjectURef)
                                             → Sequence (secuencia anidada, recursion)
    """

    def __init__(self, root: ET.Element):
        self.root = root
        self._by_id: dict[str, ET.Element] = {}
        self._by_uid: dict[str, ET.Element] = {}
        for elem in root:
            oid = elem.get("ObjectID")
            if oid:
                self._by_id[oid] = elem
            ouid = elem.get("ObjectUID")
            if ouid:
                self._by_uid[ouid] = elem

    def deref(self, node: ET.Element) -> ET.Element | None:
        """Resuelve una referencia ObjectRef u ObjectURef."""
        if node is None:
            return None
        ref = node.get("ObjectRef")
        if ref:
            return self._by_id.get(ref)
        uref = node.get("ObjectURef")
        if uref:
            return self._by_uid.get(uref)
        return None

    # --- Secuencias --------------------------------------------------------

    def find_sequences(self) -> list[ET.Element]:
        """Encuentra todos los elementos Sequence del proyecto."""
        return [el for el in self.root if el.tag == "Sequence"]

    def sequence_name(self, seq: ET.Element) -> str:
        name_el = seq.find("Name")
        return name_el.text.strip() if name_el is not None and name_el.text else "(sin nombre)"

    def sequence_uid(self, seq: ET.Element) -> str:
        return seq.get("ObjectUID", seq.get("ObjectID", "?"))

    # --- Tracks de una secuencia -------------------------------------------

    def _get_all_track_groups(self, seq: ET.Element) -> list[tuple[str, ET.Element]]:
        """Obtiene todos los track groups de una secuencia.
        Retorna lista de (tag_del_grupo_resuelto, elemento_resuelto).

        TrackGroups en el XML usa <First> con un UUID de media type (no un
        indice entero), y <Second ObjectRef="X"> apuntando al track group.
        """
        results = []
        track_groups = seq.find("TrackGroups")
        if track_groups is None:
            return results
        for tg in track_groups.findall("TrackGroup"):
            second = tg.find("Second")
            if second is not None:
                resolved = self.deref(second)
                if resolved is not None:
                    results.append((resolved.tag, resolved))
        return results

    def _get_tracks(self, track_group: ET.Element) -> list[ET.Element]:
        """Obtiene la lista de tracks resueltos de un TrackGroup."""
        tracks = []
        # Buscar Tracks dentro de TrackGroup hijo o directamente
        for tracks_el in track_group.iter("Tracks"):
            for track_ref in tracks_el.findall("Track"):
                resolved = self.deref(track_ref)
                if resolved is not None:
                    tracks.append(resolved)
            if tracks:
                break
        return tracks

    def _get_clip_items(self, track: ET.Element) -> list[ET.Element]:
        """Obtiene los ClipTrackItems resueltos de un track.

        Estructura: Track > ClipTrack > ClipItems > TrackItems > TrackItem(ObjectRef)
        Cada TrackItem referencia un VideoClipTrackItem o AudioClipTrackItem.
        """
        items = []
        for track_items_el in track.iter("TrackItems"):
            for item_ref in track_items_el.findall("TrackItem"):
                resolved = self.deref(item_ref)
                if resolved is not None:
                    items.append(resolved)
        return items

    # --- Extraccion de medios desde una secuencia --------------------------

    def collect_media_for_sequence(self, seq: ET.Element) -> set[str]:
        """Recorre el grafo desde una secuencia y devuelve todas las rutas
        de medios referenciados (incluidas secuencias anidadas, recursivo)."""
        media_paths: set[str] = set()
        visited_seqs: set[str] = set()
        self._collect_recursive(seq, media_paths, visited_seqs)
        return media_paths

    def _collect_recursive(
        self, seq: ET.Element, media_paths: set[str], visited_seqs: set[str],
    ) -> None:
        seq_uid = self.sequence_uid(seq)
        if seq_uid in visited_seqs:
            return
        visited_seqs.add(seq_uid)

        for _tag, track_group in self._get_all_track_groups(seq):
            for track in self._get_tracks(track_group):
                for clip_item in self._get_clip_items(track):
                    self._extract_from_clip_item(clip_item, media_paths, visited_seqs)

    def _extract_from_clip_item(
        self, clip_item: ET.Element, media_paths: set[str], visited_seqs: set[str],
    ) -> None:
        """Extrae media path o recurre si es secuencia anidada."""
        source_node = self._find_source(clip_item)
        if source_node is None:
            return

        # Caso 1: secuencia anidada
        nested_seq = self._find_nested_sequence(source_node)
        if nested_seq is not None:
            self._collect_recursive(nested_seq, media_paths, visited_seqs)
            return

        # Caso 2: medio normal
        media_path = self._find_media_path(source_node)
        if media_path:
            media_paths.add(media_path)

    def _find_source(self, clip_item: ET.Element) -> ET.Element | None:
        """Navega: ClipTrackItem → SubClip → Clip → VideoClip/AudioClip → Source
        y resuelve cada referencia. Retorna el Source resuelto (VideoMediaSource,
        AudioMediaSource, VideoSequenceSource, o AudioSequenceSource)."""
        # Paso 1: encontrar y resolver SubClip
        subclip = None
        for path in ["ClipTrackItem/SubClip", "SubClip"]:
            ref = clip_item.find(path)
            if ref is not None:
                subclip = self.deref(ref)
                if subclip is None:
                    subclip = ref
                break
        if subclip is None:
            return None

        # Paso 2: encontrar y resolver el Clip (VideoClip/AudioClip)
        # Premiere usa SubClip > Clip > Clip(ObjectRef) o SubClip > Clip(ObjectRef)
        clip = None
        for path in ["Clip/Clip", "Clip"]:
            ref = subclip.find(path)
            if ref is not None:
                resolved = self.deref(ref)
                if resolved is not None:
                    clip = resolved
                    break
        if clip is None:
            return None

        # Paso 3: encontrar y resolver Source
        for path in ["Source", "Clip/Source"]:
            ref = clip.find(path)
            if ref is not None:
                resolved = self.deref(ref)
                if resolved is not None:
                    return resolved
        return None

    def _find_nested_sequence(self, source_node: ET.Element) -> ET.Element | None:
        """Si el source es VideoSequenceSource/AudioSequenceSource, devuelve
        el Sequence anidado."""
        # Solo buscar en nodos que sean *SequenceSource
        if "SequenceSource" not in source_node.tag:
            return None
        for path in ["SequenceSource/Sequence", "Sequence"]:
            ref = source_node.find(path)
            if ref is not None:
                resolved = self.deref(ref)
                if resolved is not None and resolved.tag == "Sequence":
                    return resolved
        return None

    def _find_media_path(self, source_node: ET.Element) -> str | None:
        """Extrae la ruta del archivo desde un VideoMediaSource/AudioMediaSource.
        Navega: MediaSource > Media(ObjectRef) > ActualMediaFilePath."""
        # Buscar referencia a Media
        media_node = None
        for path in ["MediaSource/Media", "Media"]:
            ref = source_node.find(path)
            if ref is not None:
                resolved = self.deref(ref)
                if resolved is not None:
                    media_node = resolved
                    break

        if media_node is None:
            return None

        # Filtrar medios sinteticos (barras, tono, color matte)
        state = media_node.find("ContentAndMetadataState")
        if state is not None and state.text and state.text.strip() == SYNTHETIC_MEDIA_STATE:
            return None

        is_proxy = media_node.find("IsProxy")
        if is_proxy is not None and is_proxy.text and is_proxy.text.strip().lower() == "true":
            return None

        # Extraer ruta (FileKey es UUID en versiones recientes, no usar como path)
        for tag in ("ActualMediaFilePath", "FilePath"):
            el = media_node.find(tag)
            if el is not None and el.text and is_absolute_path(el.text.strip()):
                return el.text.strip()

        return None

    # --- Metadatos de secuencia para ranking --------------------------------

    def sequence_info(self, seq: ET.Element) -> dict:
        """Extrae metadatos utiles de una secuencia para el ranking."""
        name = self.sequence_name(seq)
        uid = self.sequence_uid(seq)

        video_tracks = 0
        audio_tracks = 0
        clip_count = 0

        for tag, track_group in self._get_all_track_groups(seq):
            tracks = self._get_tracks(track_group)
            if "Video" in tag:
                video_tracks += len(tracks)
            elif "Audio" in tag:
                audio_tracks += len(tracks)
            # Contar clips en todos los tracks (video, audio, data)
            for track in tracks:
                clip_count += len(self._get_clip_items(track))

        # Detectar secuencias anidadas
        nested_uids: set[str] = set()
        for _tag, track_group in self._get_all_track_groups(seq):
            for track in self._get_tracks(track_group):
                for clip_item in self._get_clip_items(track):
                    source = self._find_source(clip_item)
                    if source is not None:
                        ns = self._find_nested_sequence(source)
                        if ns is not None:
                            nested_uids.add(self.sequence_uid(ns))

        return {
            "element": seq,
            "name": name,
            "uid": uid,
            "video_tracks": video_tracks,
            "audio_tracks": audio_tracks,
            "clip_count": clip_count,
            "nested_count": len(nested_uids),
        }

    # --- Grafo de anidamiento -----------------------------------------------

    def build_nesting_graph(self, sequences: list[ET.Element]) -> dict[str, set[str]]:
        """Construye {parent_uid: {child_uid, ...}} para detectar raices."""
        seq_uids = {self.sequence_uid(s) for s in sequences}
        graph: dict[str, set[str]] = {uid: set() for uid in seq_uids}

        for seq in sequences:
            parent_uid = self.sequence_uid(seq)
            for _tag, track_group in self._get_all_track_groups(seq):
                for track in self._get_tracks(track_group):
                    for clip_item in self._get_clip_items(track):
                        source = self._find_source(clip_item)
                        if source is None:
                            continue
                        ns = self._find_nested_sequence(source)
                        if ns is not None:
                            child_uid = self.sequence_uid(ns)
                            if child_uid in seq_uids:
                                graph[parent_uid].add(child_uid)

        return graph

    # --- Encontrar todos los elementos Media del XML (para reescribir) ------

    def find_all_media_path_elements(self) -> list[tuple[ET.Element, str]]:
        """Busca todos los elementos con rutas de medios en el XML completo.
        Usado para reescribir rutas en el .prproj de salida."""
        results = []
        media_tags = {"ActualMediaFilePath", "MediaFilePath", "FilePath"}
        for elem in self.root.iter():
            if elem.tag in media_tags and elem.text:
                text = elem.text.strip()
                if is_absolute_path(text):
                    results.append((elem, text))
        return results

    # --- Limpieza del XML: solo secuencia seleccionada ----------------------

    def collect_reachable(self, start_elements: list[ET.Element]) -> tuple[set[str], set[str]]:
        """BFS desde los elementos iniciales, siguiendo todas las referencias
        ObjectRef/ObjectURef transitivamente.

        Retorna (set_de_ObjectIDs_necesarios, set_de_ObjectUIDs_necesarios).
        """
        needed_ids: set[str] = set()
        needed_uids: set[str] = set()
        queue = list(start_elements)
        visited: set[int] = set()  # python id() de cada elemento

        while queue:
            elem = queue.pop(0)
            py_id = id(elem)
            if py_id in visited:
                continue
            visited.add(py_id)

            # Registrar los IDs propios de este elemento
            oid = elem.get("ObjectID")
            if oid:
                needed_ids.add(oid)
            ouid = elem.get("ObjectUID")
            if ouid:
                needed_uids.add(ouid)

            # Escanear todos los descendientes buscando referencias
            for desc in elem.iter():
                ref = desc.get("ObjectRef")
                if ref and ref not in needed_ids:
                    needed_ids.add(ref)
                    target = self._by_id.get(ref)
                    if target is not None and id(target) not in visited:
                        queue.append(target)

                uref = desc.get("ObjectURef")
                if uref and uref not in needed_uids:
                    needed_uids.add(uref)
                    target = self._by_uid.get(uref)
                    if target is not None and id(target) not in visited:
                        queue.append(target)

        return needed_ids, needed_uids

    def trim_to_sequence(self, seq: ET.Element, log: logging.Logger) -> int:
        """Elimina del XML todos los objetos que no son alcanzables desde la
        secuencia seleccionada. Simula el comportamiento del Project Manager
        de Premiere ('Collect Files' para una secuencia).

        Mantiene la infraestructura del proyecto (settings, bins) para que
        Premiere pueda abrir el archivo sin errores.

        Retorna el numero de elementos eliminados.
        """
        # Tags de infraestructura que siempre se mantienen
        KEEP_TAGS = {
            "Project", "ProjectSettings", "ScratchDiskSettings",
            "IngestSettings", "WorkspaceSettings", "DummyCaptureSettings",
            "DefaultSequenceSettings", "RootProjectItem", "BinProjectItem",
            "VideoSettings", "AudioSettings",
            "VideoCompileSettings", "AudioCompileSettings", "CompileSettings",
            "WorkspaceSettings",
        }

        # BFS desde la secuencia: encontrar todo lo alcanzable
        needed_ids, needed_uids = self.collect_reachable([seq])

        # Eliminar elementos top-level no alcanzables
        to_remove = []
        for elem in list(self.root):
            # Infraestructura: siempre se mantiene
            if elem.tag in KEEP_TAGS:
                continue

            oid = elem.get("ObjectID")
            ouid = elem.get("ObjectUID")

            # Si alguno de sus IDs esta en el set de necesarios, mantener
            if oid and oid in needed_ids:
                continue
            if ouid and ouid in needed_uids:
                continue

            # Elementos sin IDs son estructurales, mantener
            if not oid and not ouid:
                continue

            to_remove.append(elem)

        for elem in to_remove:
            self.root.remove(elem)

        log.info("  XML limpiado: %d objetos eliminados", len(to_remove))
        return len(to_remove)


# ---------------------------------------------------------------------------
# Sistema de ranking de secuencias
# ---------------------------------------------------------------------------

def score_sequence(info: dict, nesting_graph: dict, all_infos: list[dict]) -> float:
    """Puntua una secuencia para determinar cual es la principal.

    Factores (pesos):
      - Topologia de anidamiento: 0.40
      - Densidad de clips: 0.25
      - Complejidad (tracks): 0.20
      - Nombre: 0.15
    """
    uid = info["uid"]
    score = 0.0

    # --- Topologia (0.40) ---
    # Es raiz (no esta anidada en ninguna otra)?
    is_child = any(uid in children for children in nesting_graph.values())
    has_children = len(nesting_graph.get(uid, set())) > 0

    if not is_child and has_children:
        score += 0.40  # Raiz con hijos: maximo
    elif not is_child and not has_children:
        score += 0.20  # Raiz sin hijos: podria ser la principal o un deliverable
    elif is_child and has_children:
        score += 0.10  # Nodo intermedio
    else:
        score += 0.0   # Hoja anidada: probablemente no es la principal

    # --- Densidad de clips (0.25) ---
    max_clips = max((i["clip_count"] for i in all_infos), default=1) or 1
    score += 0.25 * (info["clip_count"] / max_clips)

    # --- Complejidad: tracks (0.20) ---
    total_tracks = info["video_tracks"] + info["audio_tracks"]
    max_tracks = max((i["video_tracks"] + i["audio_tracks"] for i in all_infos), default=1) or 1
    score += 0.20 * (total_tracks / max_tracks)

    # --- Nombre (0.15) ---
    name_lower = info["name"].lower()
    name_score = 0.5  # neutro por defecto

    for pattern in PROMOTE_PATTERNS:
        if pattern in name_lower:
            name_score = 1.0
            break

    for pattern in DEMOTE_PATTERNS:
        if pattern in name_lower:
            name_score = 0.0
            break

    score += 0.15 * name_score

    return round(score, 3)


# ---------------------------------------------------------------------------
# Seleccion interactiva de secuencia
# ---------------------------------------------------------------------------

def format_duration_ticks(ticks: int) -> str:
    """Convierte ticks de Premiere a formato MM:SS."""
    if ticks <= 0:
        return "--:--"
    seconds = ticks / TICKS_PER_SECOND
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def select_sequence_interactive(
    ranked: list[tuple[dict, float]],
    log: logging.Logger,
) -> dict | None:
    """Muestra tabla de secuencias y pide al usuario que elija."""
    if not ranked:
        log.warning("  No se encontraron secuencias.")
        return None

    # Cabecera
    log.info("")
    log.info("  Secuencias encontradas:")
    log.info("  %s", "-" * 62)
    log.info("   #   %-30s  %6s  %5s  %s", "Nombre", "Clips", "Pistas", "Anidadas")
    log.info("  %s", "-" * 62)

    for i, (info, sc) in enumerate(ranked, 1):
        total_tracks = info["video_tracks"] + info["audio_tracks"]
        marker = " <--" if i == 1 else ""
        log.info(
            "  %2d   %-30s  %6d  %5d  %4d%s",
            i,
            info["name"][:30],
            info["clip_count"],
            total_tracks,
            info["nested_count"],
            marker,
        )

    log.info("  %s", "-" * 62)

    # Pedir seleccion
    while True:
        try:
            raw = input(f"  Selecciona secuencia [1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            log.info("\n  Cancelado.")
            return None

        if raw == "":
            return ranked[0][0]

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(ranked):
                return ranked[idx - 1][0]

        log.info("  Introduce un numero entre 1 y %d", len(ranked))


def select_sequence_by_pattern(
    ranked: list[tuple[dict, float]],
    pattern: str,
    log: logging.Logger,
) -> dict | None:
    """Selecciona la primera secuencia cuyo nombre coincide con el patron."""
    for info, _ in ranked:
        if fnmatch.fnmatch(info["name"].lower(), pattern.lower()):
            log.info("  Secuencia seleccionada por patron '%s': %s", pattern, info["name"])
            return info
    log.warning("  Ningun nombre coincide con el patron '%s'", pattern)
    # Mostrar nombres disponibles
    for info, _ in ranked:
        log.info("    - %s", info["name"])
    return None


def select_sequence_auto(
    ranked: list[tuple[dict, float]],
    log: logging.Logger,
) -> dict | None:
    """Selecciona automaticamente la secuencia con mayor puntuacion."""
    if not ranked:
        log.warning("  No se encontraron secuencias.")
        return None
    info, sc = ranked[0]
    log.info("  Auto-seleccionada: %s (score: %.3f)", info["name"], sc)
    return info


# ---------------------------------------------------------------------------
# Utilidades de rutas
# ---------------------------------------------------------------------------

def is_auto_nested_name(name: str) -> bool:
    """Detecta nombres auto-generados por Premiere como 'Secuencia anidada 01'."""
    lower = name.lower().strip()
    for pattern in SKIP_PATTERNS:
        if lower.startswith(pattern):
            return True
    return False


def is_absolute_path(text: str) -> bool:
    if not text or len(text) < 3:
        return False
    if text[1] == ":" and text[2] in "/\\":
        return True
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if text[0] == "/" and len(text) > 1 and text[1] != "/":
        return True
    return False


_NUMBERED_PREFIX = re.compile(r"^\d+[\.\-\s]+\s*")


def _clean_folder_name(name: str) -> str:
    """Quita prefijos numerados: '1. Material' -> 'Material', '2. Projects' -> 'Projects'."""
    return _NUMBERED_PREFIX.sub("", name) or name


def _fmt_size(nbytes: int) -> str:
    """Formatea bytes a unidad legible: 1.5 GB, 320 MB, 4.2 KB."""
    if nbytes >= 1 << 30:
        return f"{nbytes / (1 << 30):.1f} GB"
    if nbytes >= 1 << 20:
        return f"{nbytes / (1 << 20):.0f} MB"
    if nbytes >= 1 << 10:
        return f"{nbytes / (1 << 10):.0f} KB"
    return f"{nbytes} B"


def media_dest_path(original: str, media_folder: Path) -> Path:
    """Ruta destino de un medio dentro de Otros/."""
    p = Path(original)
    drive = p.drive.replace(":", "")
    if drive:
        try:
            rel = p.relative_to(p.anchor)
        except ValueError:
            rel = Path(p.name)
        return media_folder / drive / rel
    else:
        try:
            rel = p.relative_to("/")
        except ValueError:
            rel = Path(p.name)
        return media_folder / rel


def unique_folder_name(base_name: str, used: set[str]) -> str:
    if base_name not in used:
        used.add(base_name)
        return base_name
    n = 2
    while f"{base_name}_{n}" in used:
        n += 1
    name = f"{base_name}_{n}"
    used.add(name)
    return name


# ---------------------------------------------------------------------------
# After Effects: escaneo de dependencias de footage
# ---------------------------------------------------------------------------

# Extensiones de footage que After Effects suele referenciar
_AE_FOOTAGE_EXTENSIONS = frozenset({
    # Video
    ".mov", ".mp4", ".avi", ".mxf", ".m4v", ".mkv", ".wmv", ".mpg", ".mpeg",
    ".m2t", ".m2ts", ".mts", ".r3d", ".braw", ".ari",
    # Imagen / secuencia de imagenes
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".psd", ".ai", ".exr",
    ".dpx", ".tga", ".bmp", ".gif", ".webp", ".svg", ".hdr",
    # Audio
    ".wav", ".mp3", ".aif", ".aiff", ".m4a", ".aac", ".flac", ".ogg",
    # Proyectos Adobe / 3D
    ".aep", ".aepx", ".mogrt", ".c4d", ".obj", ".fbx",
    # Fuentes
    ".otf", ".ttf",
})

# Regex para rutas absolutas en texto decodificado de binarios AE.
# Al decodificar bytes con errors="replace", los bytes no-UTF-8 se convierten
# en U+FFFD, que actuan como delimitadores naturales de las cadenas embebidas.
_RE_AE_WIN_PATH = re.compile(
    r'([A-Za-z]:[/\\][^\x00-\x1f\ufffd]+\.\w{2,10})(?=[^\w.]|$)')
_RE_AE_MAC_PATH = re.compile(
    r'(?<!:)(/(?:Volumes|Users)/[^\x00-\x1f\ufffd]+\.\w{2,10})(?=[^\w.]|$)')


def _scan_aep_for_footage(aep_path: Path, log: logging.Logger) -> set[str]:
    """Escanea un proyecto After Effects (.aep/.aepx) buscando rutas de footage.

    Para .aep (binario RIFX): decodifica como UTF-8 y busca patrones de rutas.
    Para .aepx (XML): misma estrategia (las rutas estan en texto plano).
    """
    try:
        data = aep_path.read_bytes()
    except OSError as exc:
        log.warning("    [AE] Error leyendo %s: %s", aep_path.name, exc)
        return set()

    if len(data) > 200_000_000:
        log.warning("    [AE] %s muy grande (%s), omitiendo escaneo",
                     aep_path.name, _fmt_size(len(data)))
        return set()

    # Descomprimir si es gzip (.aepx puede estar comprimido)
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            pass

    paths: set[str] = set()
    exclude = str(aep_path)

    # Decodificar como UTF-8; los bytes binarios se convierten en \ufffd
    text = data.decode("utf-8", errors="replace")

    for m in _RE_AE_WIN_PATH.finditer(text):
        p = m.group(1)
        if len(p) <= 500 and p != exclude:
            try:
                if Path(p).suffix.lower() in _AE_FOOTAGE_EXTENSIONS:
                    paths.add(p)
            except Exception:
                pass

    for m in _RE_AE_MAC_PATH.finditer(text):
        p = m.group(1)
        if len(p) <= 500 and p != exclude:
            try:
                if Path(p).suffix.lower() in _AE_FOOTAGE_EXTENSIONS:
                    paths.add(p)
            except Exception:
                pass

    return paths


def _expand_ae_dependencies(
    target_paths: set[str],
    path_mappings: list[tuple[str, str]],
    log: logging.Logger,
) -> set[str]:
    """Detecta archivos .aep/.aepx en los medios recolectados y escanea
    sus dependencias de footage.  Soporta proyectos AE anidados."""
    ae_deps: set[str] = set()
    scanned: set[str] = set()

    # Identificar .aep/.aepx en los medios recolectados
    to_scan: set[tuple[str, str]] = set()
    for orig in target_paths:
        translated = translate_path(normalize_media_path(orig), path_mappings)
        if Path(translated).suffix.lower() in AE_PROJECT_EXTENSIONS:
            to_scan.add((orig, translated))

    while to_scan:
        current_batch = to_scan
        to_scan = set()

        for _orig, translated in current_batch:
            if translated in scanned:
                continue
            scanned.add(translated)

            src = Path(translated)
            if not src.exists():
                log.info("    [AE] %s (offline, dependencias no resueltas)", src.name)
                continue

            log.info("    [AE] Escaneando footage: %s", src.name)
            deps = _scan_aep_for_footage(src, log)
            new_deps = deps - target_paths - ae_deps

            if new_deps:
                ae_deps.update(new_deps)
                log.info("    [AE]   -> %d archivos encontrados", len(new_deps))

                # Buscar .aep anidados para escaneo recursivo
                for dep in new_deps:
                    dep_translated = translate_path(
                        normalize_media_path(dep), path_mappings)
                    if Path(dep_translated).suffix.lower() in AE_PROJECT_EXTENSIONS:
                        if dep_translated not in scanned:
                            to_scan.add((dep, dep_translated))

    if ae_deps:
        log.info("  Dependencias After Effects: +%d archivos", len(ae_deps))

    return ae_deps


# Carpetas de archivo que se ignoran al buscar .aep en el arbol del proyecto
_AE_SKIP_DIRS = frozenset({
    "antic", "old", "backup", "archive", "antiguo", "bak", "prev",
    "node_modules", ".git", "__pycache__",
    "adobe premiere pro auto-save",
    "almacenamiento automático de adobe after effects",
    "adobe after effects auto-save",
})


def _find_ae_projects_in_tree(
    project_root: Path,
    log: logging.Logger,
    exclude_folder: str = "",
) -> set[str]:
    """Busca archivos .aep/.aepx en el arbol del proyecto.

    Esto captura proyectos After Effects que no estan referenciados
    directamente en la secuencia de Premiere (ej. cuando se usa el render
    del AE en vez de Dynamic Link).
    """
    skip = _AE_SKIP_DIRS
    if exclude_folder:
        skip = skip | {exclude_folder.lower()}

    ae_files: set[str] = set()
    try:
        for dirpath, dirnames, filenames in os.walk(project_root):
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in skip
            ]
            for fname in filenames:
                if Path(fname).suffix.lower() in AE_PROJECT_EXTENSIONS:
                    ae_files.add(str(Path(dirpath) / fname))
    except OSError:
        pass

    if ae_files:
        log.info("  Proyectos After Effects en carpeta: %d", len(ae_files))

    return ae_files


# ---------------------------------------------------------------------------
# Empaquetado de un proyecto
# ---------------------------------------------------------------------------

def list_sequences(prproj_path: Path) -> list[tuple[dict, float]]:
    """Return ranked list of (info, score) for sequences in a .prproj file.

    Useful for GUI sequence pickers.  Each *info* dict has keys:
    name, uid, clip_count, video_tracks, audio_tracks, nested_count, element.
    """
    xml_bytes = read_prproj(prproj_path)
    root = ET.fromstring(xml_bytes)
    graph = PrprojGraph(root)
    sequences = graph.find_sequences()
    if not sequences:
        return []

    infos = [graph.sequence_info(s) for s in sequences]
    nesting = graph.build_nesting_graph(sequences)
    scored = [(info, score_sequence(info, nesting, infos)) for info in infos]
    ranked = sorted(scored, key=lambda x: x[1], reverse=True)

    filtered = [
        (info, sc) for info, sc in ranked
        if not is_auto_nested_name(info["name"])
        and info["clip_count"] > MIN_CLIPS_THRESHOLD
    ]
    return filtered if filtered else ranked


def package_project(
    prproj_path: Path,
    dest_root: Path,
    folder_name: str,
    dry_run: bool,
    mode: str,
    sequence_pattern: str | None,
    path_mappings: list[tuple[str, str]],
    log: logging.Logger,
    sequence_callback=None,
) -> dict:
    """Empaqueta un proyecto .prproj individual."""
    stats = {"copied": 0, "missing": 0, "skipped": 0, "errors": []}

    project_folder = dest_root / folder_name
    media_folder = project_folder / "Otros"

    log.info("  Origen:  %s", prproj_path)
    log.info("  Destino: %s", project_folder)

    # --- Leer y parsear ---
    try:
        xml_bytes = read_prproj(prproj_path)
    except Exception as exc:
        log.error("  ERROR leyendo archivo: %s", exc)
        stats["errors"].append(str(prproj_path))
        return stats

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.error("  ERROR parseando XML: %s", exc)
        stats["errors"].append(str(prproj_path))
        return stats

    graph = PrprojGraph(root)

    # --- Determinar medios a copiar ---
    selected_seq = None  # secuencia elegida (para limpiar el XML)

    if mode == "all":
        # Modo legacy: copiar todos los medios del proyecto
        all_media_elems = graph.find_all_media_path_elements()
        target_paths = set()
        for _, path_str in all_media_elems:
            target_paths.add(path_str)
        log.info("  Modo --all: %d medios totales en el proyecto", len(target_paths))
    else:
        # Seleccionar secuencia
        sequences = graph.find_sequences()
        if not sequences:
            log.warning("  No se encontraron secuencias en el proyecto.")
            # Fallback: copiar todo
            all_media_elems = graph.find_all_media_path_elements()
            target_paths = {p for _, p in all_media_elems}
            log.info("  Copiando todos los medios (%d archivos)", len(target_paths))
        else:
            # Calcular info y ranking
            infos = [graph.sequence_info(s) for s in sequences]
            nesting = graph.build_nesting_graph(sequences)
            scored = [(info, score_sequence(info, nesting, infos)) for info in infos]
            ranked = sorted(scored, key=lambda x: x[1], reverse=True)

            # Filtrar secuencias internas de Premiere:
            # - Nombre auto-generado ("Secuencia anidada 01")
            # - Nests internos (<=2 clips, tipico de Premiere al anidar)
            filtered = [
                (info, sc) for info, sc in ranked
                if not is_auto_nested_name(info["name"])
                and info["clip_count"] > MIN_CLIPS_THRESHOLD
            ]
            if filtered:
                ranked = filtered

            # Seleccionar segun modo
            selected = None
            if sequence_callback is not None:
                # GUI callback: recibe ranked, devuelve info dict o None
                selected = sequence_callback(ranked)
            elif mode == "auto":
                selected = select_sequence_auto(ranked, log)
            elif mode == "pattern":
                selected = select_sequence_by_pattern(ranked, sequence_pattern, log)
            else:  # interactive
                selected = select_sequence_interactive(ranked, log)

            if selected is None:
                log.warning("  Sin secuencia seleccionada. Saltando proyecto.")
                return stats

            log.info("  Secuencia: %s", selected["name"])
            selected_seq = selected["element"]

            # Recolectar medios de la secuencia (+ anidadas)
            target_paths = graph.collect_media_for_sequence(selected_seq)
            log.info("  Medios de esta secuencia: %d archivos", len(target_paths))

    # --- Incluir proyectos After Effects del arbol del proyecto ---
    ae_projects = _find_ae_projects_in_tree(dest_root, log, exclude_folder=folder_name)
    if ae_projects:
        target_paths.update(ae_projects)

    # --- Expandir con dependencias de After Effects ---
    ae_extra = _expand_ae_dependencies(target_paths, path_mappings, log)
    if ae_extra:
        target_paths.update(ae_extra)

    if not target_paths:
        log.info("  Sin medios que copiar.")
        if not dry_run:
            project_folder.mkdir(parents=True, exist_ok=True)
            modified = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            write_prproj(project_folder / prproj_path.name, modified)
        return stats

    # --- Traducir rutas Mac -> Windows y construir mapa ---
    # path_map: ruta_original_xml -> ruta_destino
    # src_map:  ruta_original_xml -> ruta_real_en_disco (traducida)
    #
    # Estrategia: si el archivo esta dentro de la carpeta del proyecto,
    # mantener su ruta relativa (misma estructura de carpetas original).
    # Si es externo (otro disco, otra carpeta), poner en Otros/.
    path_map: dict[str, Path] = {}
    src_map: dict[str, Path] = {}
    # dest_root es la raiz real del proyecto (ej: Proyecto/)
    # no prproj_path.parent (que puede ser Proyecto/2. Proyectos/)
    project_root = dest_root
    internal_count = 0
    external_count = 0

    for orig in sorted(target_paths):
        normalized = normalize_media_path(orig)
        translated = translate_path(normalized, path_mappings)
        src_path = Path(translated)
        src_map[orig] = src_path

        try:
            rel = src_path.relative_to(project_root)
            # Limpiar prefijos numerados: "1. Material/..." -> "Material/..."
            clean_parts = [_clean_folder_name(p) for p in rel.parent.parts]
            clean_rel = Path(*clean_parts, rel.name) if clean_parts else rel
            path_map[orig] = project_folder / clean_rel
            internal_count += 1
        except ValueError:
            # Archivo externo al proyecto -> Otros/ con estructura por drive
            path_map[orig] = media_dest_path(translated, media_folder)
            external_count += 1

    if external_count:
        log.info("  Medios del proyecto: %d | Externos (Otros/): %d",
                 internal_count, external_count)

    if path_mappings:
        translated_count = sum(1 for o in target_paths if str(src_map[o]) != o)
        if translated_count:
            log.info("  Rutas traducidas Mac->Win: %d/%d", translated_count, len(target_paths))

    copied_origs: set[str] = set()  # Medios que se copiaron/copiarian
    for orig in sorted(target_paths):
        src = src_map[orig]
        dst = path_map[orig]

        if not src.exists():
            log.warning("    [OFFLINE]  %s", src)
            stats["missing"] += 1
            continue

        if src.is_dir():
            continue

        copied_origs.add(orig)
        if dry_run:
            log.info("    [COPIARIA] %s", src.name)
            stats["copied"] += 1
        else:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    shutil.copy2(src, dst)
                    log.info("    [COPIADO]  %s", src.name)
                    stats["copied"] += 1
                else:
                    stats["skipped"] += 1
            except OSError as exc:
                log.error("    [ERROR]    %s: %s", src.name, exc)
                stats["errors"].append(str(src))

    # --- Mostrar estructura de carpetas del empaquetado ---
    # Recopilar rutas destino con conteo y tamaño por carpeta
    dir_counts: dict[str, int] = {}
    dir_sizes: dict[str, int] = {}
    root_files = 0
    root_size = 0
    total_size = 0
    for orig in sorted(target_paths):
        src = src_map[orig]
        if not src.exists() or src.is_dir():
            continue
        try:
            fsize = src.stat().st_size
        except OSError:
            fsize = 0
        total_size += fsize
        dst = path_map[orig]
        try:
            rel = dst.relative_to(project_folder)
        except ValueError:
            continue
        if rel.parent == Path("."):
            root_files += 1
            root_size += fsize
        else:
            parts = rel.parent.parts
            for depth in range(len(parts)):
                key = str(Path(*parts[: depth + 1]))
                # Conteo solo en la carpeta hoja (donde esta el archivo)
                if depth == len(parts) - 1:
                    dir_counts[key] = dir_counts.get(key, 0) + 1
                else:
                    dir_counts.setdefault(key, 0)
                # Tamaño se acumula en todas las carpetas ancestro
                dir_sizes[key] = dir_sizes.get(key, 0) + fsize

    if dir_counts or root_files:
        log.info("  Estructura Empaquetado (%s):", _fmt_size(total_size))
        log.info("    %s/", folder_name)
        if root_files:
            log.info("    |-- %s + %d archivos (%s)",
                     prproj_path.name, root_files, _fmt_size(root_size))
        else:
            log.info("    |-- %s", prproj_path.name)
        shown: set[str] = set()
        for folder in sorted(dir_counts):
            parts = Path(folder).parts
            for depth in range(len(parts)):
                partial = str(Path(*parts[: depth + 1]))
                if partial in shown:
                    continue
                shown.add(partial)
                indent = "    " + "|   " * depth
                count = dir_counts.get(partial, 0)
                size = dir_sizes.get(partial, 0)
                size_str = _fmt_size(size) if size else ""
                if count:
                    log.info("%s|-- %s/ (%d archivos, %s)",
                             indent, parts[depth], count, size_str)
                else:
                    if size_str:
                        log.info("%s|-- %s/ (%s)", indent, parts[depth], size_str)
                    else:
                        log.info("%s|-- %s/", indent, parts[depth])

    # --- Limpiar XML: solo la secuencia seleccionada y sus dependencias ---
    if selected_seq is not None:
        if dry_run:
            # Calcular cuantos se eliminarian sin modificar el arbol
            needed_ids, needed_uids = graph.collect_reachable([selected_seq])
            keep_tags = {
                "Project", "ProjectSettings", "ScratchDiskSettings",
                "IngestSettings", "WorkspaceSettings", "DummyCaptureSettings",
                "DefaultSequenceSettings", "RootProjectItem", "BinProjectItem",
                "VideoSettings", "AudioSettings",
                "VideoCompileSettings", "AudioCompileSettings", "CompileSettings",
            }
            removable = sum(
                1 for elem in root
                if elem.tag not in keep_tags
                and (elem.get("ObjectID") or elem.get("ObjectUID"))
                and not (elem.get("ObjectID") and elem.get("ObjectID") in needed_ids)
                and not (elem.get("ObjectUID") and elem.get("ObjectUID") in needed_uids)
            )
            log.info("  [DRY-RUN] Eliminaria %d objetos del XML", removable)
        else:
            graph.trim_to_sequence(selected_seq, log)

    # --- Reescribir rutas de medios copiados en el XML ---
    # Solo reescribir archivos que se copiaron (no offline).
    # Usar rutas relativas al .prproj para que el proyecto sea portable.
    all_media_elems = graph.find_all_media_path_elements()
    for elem, orig in all_media_elems:
        if orig in copied_origs:
            dst = path_map[orig]
            try:
                rel = dst.relative_to(project_folder)
                elem.text = "./" + str(rel).replace("\\", "/")
            except ValueError:
                elem.text = str(dst).replace("\\", "/")

    # --- Guardar ---
    if dry_run:
        log.info("  [GUARDARIA]  %s", project_folder / prproj_path.name)
    else:
        project_folder.mkdir(parents=True, exist_ok=True)
        modified = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        write_prproj(project_folder / prproj_path.name, modified)
        log.info("  Proyecto empaquetado guardado.")

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Empaquetador de proyectos Adobe Premiere Pro en lote.",
        epilog=(
            "Ejemplos:\n"
            '  python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --dry-run\n'
            '  python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --auto\n'
            '  python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --sequence "*boda*"\n'
            '  python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --all\n'
            '  python empaquetar_premiere.py "V:/Proyectos" "E:/Backup" --map "/Volumes/SEG=V:" --dry-run\n'
            '  python empaquetar_premiere.py "proyecto.prproj" "E:/Backup" --auto'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("origen", type=Path, help="Carpeta con .prproj o archivo .prproj individual")
    parser.add_argument("destino", type=Path, help="Carpeta destino para el backup")

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--auto",
        action="store_true",
        help="Selecciona automaticamente la secuencia principal",
    )
    group.add_argument(
        "--sequence",
        type=str,
        metavar="PATRON",
        help="Selecciona secuencia por nombre (soporta comodines: *boda*)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Empaqueta todos los medios sin filtrar por secuencia",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra lo que haria sin copiar nada",
    )
    parser.add_argument(
        "--map",
        action="append",
        metavar="MAC=WIN",
        help=(
            "Traduccion de rutas Mac a Windows. Formato: /Volumes/DISCO=V:\n"
            "Se puede repetir: --map /Volumes/A=V: --map /Volumes/B=W:"
        ),
    )
    parser.add_argument(
        "--include-autosave",
        action="store_true",
        help="Incluir carpetas 'Adobe Premiere Pro Auto-Save' (omitidas por defecto)",
    )

    args = parser.parse_args()
    log = setup_logging()

    # Parsear mapeos de rutas
    path_mappings = parse_path_mappings(args.map)

    # Aceptar un archivo .prproj individual o una carpeta
    if args.origen.is_file() and args.origen.suffix.lower() == ".prproj":
        projects = [args.origen]
    elif args.origen.is_dir():
        projects = sorted(args.origen.rglob("*.prproj"))
        # Filtrar Auto-Save por defecto
        if not args.include_autosave:
            before = len(projects)
            projects = [
                p for p in projects
                if "Adobe Premiere Pro Auto-Save" not in str(p)
            ]
            filtered = before - len(projects)
            if filtered:
                log.info("  (Omitidos %d proyectos de Auto-Save)", filtered)
    else:
        log.error("Origen no encontrado: %s", args.origen)
        sys.exit(1)

    if not projects:
        log.warning("No se encontraron .prproj en %s", args.origen)
        sys.exit(0)

    # Determinar modo
    if args.all:
        mode = "all"
    elif args.auto:
        mode = "auto"
    elif args.sequence:
        mode = "pattern"
    else:
        mode = "interactive"

    # Cabecera
    log.info("=" * 60)
    log.info("EMPAQUETADOR DE PROYECTOS PREMIERE PRO")
    log.info("=" * 60)
    if args.dry_run:
        log.info("  MODO DRY-RUN: no se copiara nada")
    mode_labels = {
        "interactive": "Interactivo (elegir secuencia)",
        "auto": "Automatico (mejor candidata)",
        "pattern": f"Por patron: {args.sequence}",
        "all": "Todos los medios",
    }
    log.info("  Seleccion:  %s", mode_labels[mode])
    log.info("  Origen:     %s", args.origen)
    log.info("  Destino:    %s", args.destino)
    log.info("  Proyectos:  %d", len(projects))
    if path_mappings:
        log.info("  Mapeos Mac->Win:")
        for mac, win in path_mappings:
            log.info("    %s  ->  %s", mac, win)
    log.info("")

    # Procesar
    totals = {"copied": 0, "missing": 0, "skipped": 0, "errors": [], "projects": 0}
    used_names: set[str] = set()

    for i, prproj in enumerate(projects, 1):
        folder_name = unique_folder_name(prproj.stem, used_names)
        log.info("[%d/%d] %s", i, len(projects), prproj.name)

        stats = package_project(
            prproj, args.destino, folder_name,
            args.dry_run, mode, args.sequence,
            path_mappings, log,
        )

        totals["copied"] += stats["copied"]
        totals["missing"] += stats["missing"]
        totals["skipped"] += stats["skipped"]
        totals["errors"].extend(stats["errors"])
        totals["projects"] += 1
        log.info("")

    # Resumen
    log.info("=" * 60)
    log.info("RESUMEN")
    log.info("=" * 60)
    log.info("  Proyectos procesados:      %d", totals["projects"])
    log.info("  Archivos copiados:         %d", totals["copied"])
    log.info("  Ya existentes (omitidos):  %d", totals["skipped"])
    log.info("  Medios no encontrados:     %d", totals["missing"])
    log.info("  Errores:                   %d", len(totals["errors"]))
    if totals["errors"]:
        log.info("  Detalle de errores:")
        for err in totals["errors"]:
            log.info("    - %s", err)
    if args.dry_run:
        log.info("")
        log.info("  (dry-run: no se realizaron cambios en disco)")


if __name__ == "__main__":
    main()
