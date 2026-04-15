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
import json
import logging
import os
import posixpath
import re
import shutil
import struct
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from collections import deque
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


_XML_DECL = b'<?xml version="1.0" encoding="UTF-8" ?>\n'

# Tags cuyo texto contiene XML escapado con saltos codificados como &#10;.
# Premiere rechaza el proyecto si estos aparecen como \n literal tras el
# round-trip. ET decodifica &#10; al parsear, asi que re-codificamos el
# texto de estos elementos al serializar.
_PRESERVE_NL_TAGS = (b"Project.Metadata.Schema",
                     b"ExportSettings.ExportedPreset.SaveAsFile")


def _reencode_nl_refs(xml_bytes: bytes) -> bytes:
    """Dentro del contenido textual de tags conocidos con XML escapado,
    reemplaza \\n literal por &#10; para preservar el formato Adobe."""
    import re as _re
    for tag in _PRESERVE_NL_TAGS:
        pattern = _re.compile(
            rb"(<" + _re.escape(tag) + rb"\b[^>]*>)(.*?)(</"
            + _re.escape(tag) + rb">)",
            _re.DOTALL,
        )
        def _sub(m: "_re.Match[bytes]") -> bytes:
            return m.group(1) + m.group(2).replace(b"\n", b"&#10;") + m.group(3)
        xml_bytes = pattern.sub(_sub, xml_bytes)
    return xml_bytes


def serialize_prproj(root: ET.Element) -> bytes:
    """Serializa el árbol XML con el formato exacto que Premiere Pro espera.

    Formato Adobe verificado contra .prproj originales:
    - Declaración con espacio antes de ?>
    - Etiquetas vacías auto-cerradas sin espacio: <Tag/>  (no <Tag />)
    - Dos saltos de línea tras </PremiereData>
    - &#10; preservado dentro de texto con XML escapado
    """
    body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    body = body.replace(b" />", b"/>")
    body = _reencode_nl_refs(body)
    return _XML_DECL + body + b"\n\n"


def write_prproj(path: Path, xml_bytes: bytes) -> None:
    """Comprime XML con header gzip compatible con Premiere Pro.

    Premiere valida el byte OS del header gzip y rechaza archivos que
    no usen el valor 19 (custom de Adobe). Python's gzip usa 255 (Windows)
    o 3 (Unix), lo cual causa "El proyecto parece dañado".
    """
    # Comprimir con deflate crudo (sin wrapper gzip)
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    compressed = compressor.compress(xml_bytes) + compressor.flush()

    # Header gzip con OS byte = 19 (valor de Adobe)
    header = struct.pack("<2sBBIBB",
        b"\x1f\x8b",   # Magic number
        8,              # Método: deflate
        0,              # Flags: sin campos extra
        0,              # MTime: cero
        0,              # Extra flags
        19,             # OS: Adobe Premiere (0x13)
    )

    # Trailer: CRC32 + tamaño original
    crc = zlib.crc32(xml_bytes) & 0xFFFFFFFF
    size = len(xml_bytes) & 0xFFFFFFFF
    trailer = struct.pack("<II", crc, size)

    path.write_bytes(header + compressed + trailer)


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
        Usado para reescribir rutas en el .prproj de salida.

        Solo devuelve etiquetas con ruta absoluta. Las RelativePath se manejan
        aparte (necesitan contexto del destino, no se pueden normalizar aqui).
        """
        results = []
        media_tags = {"ActualMediaFilePath", "MediaFilePath", "FilePath"}
        for elem in self.root.iter():
            if elem.tag in media_tags and elem.text:
                text = elem.text.strip()
                if is_absolute_path(text):
                    results.append((elem, text))
        return results

    def find_relative_path_elements(self) -> list[ET.Element]:
        """Devuelve todos los <RelativePath> del XML. Se reescriben tras
        mover los medios, calculando la relativa desde el .prproj."""
        return [el for el in self.root.iter()
                if el.tag == "RelativePath" and el.text]

    # --- Limpieza del XML: solo secuencia seleccionada ----------------------

    def _ensure_ref_graph(self) -> None:
        """Pre-construye el mapa de referencias salientes de cada elemento
        top-level.  Se ejecuta una sola vez (lazy) y se reutiliza en cada
        llamada a collect_reachable."""
        if hasattr(self, "_outgoing"):
            return
        # _outgoing[python_id(elem)] = (set_of_ObjectRef, set_of_ObjectURef)
        self._outgoing: dict[int, tuple[set[str], set[str]]] = {}
        for elem in self.root:
            refs: set[str] = set()
            urefs: set[str] = set()
            for desc in elem.iter():
                r = desc.get("ObjectRef")
                if r:
                    refs.add(r)
                ur = desc.get("ObjectURef")
                if ur:
                    urefs.add(ur)
            self._outgoing[id(elem)] = (refs, urefs)

    def collect_reachable(self, start_elements: list[ET.Element]) -> tuple[set[str], set[str]]:
        """BFS desde los elementos iniciales, siguiendo todas las referencias
        ObjectRef/ObjectURef transitivamente.

        Retorna (set_de_ObjectIDs_necesarios, set_de_ObjectUIDs_necesarios).
        """
        self._ensure_ref_graph()

        needed_ids: set[str] = set()
        needed_uids: set[str] = set()
        queue: deque[ET.Element] = deque(start_elements)
        visited: set[int] = set()

        while queue:
            elem = queue.popleft()
            py_id = id(elem)
            if py_id in visited:
                continue
            visited.add(py_id)

            oid = elem.get("ObjectID")
            if oid:
                needed_ids.add(oid)
            ouid = elem.get("ObjectUID")
            if ouid:
                needed_uids.add(ouid)

            refs, urefs = self._outgoing.get(py_id, (set(), set()))
            for ref in refs:
                if ref not in needed_ids:
                    needed_ids.add(ref)
                    target = self._by_id.get(ref)
                    if target is not None and id(target) not in visited:
                        queue.append(target)
            for uref in urefs:
                if uref not in needed_uids:
                    needed_uids.add(uref)
                    target = self._by_uid.get(uref)
                    if target is not None and id(target) not in visited:
                        queue.append(target)

        return needed_ids, needed_uids

    def trim_to_sequence(self, seqs: ET.Element | list[ET.Element],
                         log: logging.Logger) -> int:
        """Elimina del XML todos los objetos que no son alcanzables desde la(s)
        secuencia(s) seleccionada(s). Simula el comportamiento del Project Manager
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

        # BFS desde la(s) secuencia(s): encontrar todo lo alcanzable
        start = seqs if isinstance(seqs, list) else [seqs]
        needed_ids, needed_uids = self.collect_reachable(start)

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

        # Post-pruning: limpiar referencias colgantes en todos los elementos
        # preservados via KEEP_TAGS (bins sobre todo, pero cualquier contenedor
        # kept unconditionally puede tener el mismo problema). Premiere rechaza
        # el proyecto con "el proyecto parece danado" si queda cualquier Ref o
        # URef apuntando a un ID inexistente.
        def _collect_present() -> tuple[set[str], set[str]]:
            ids: set[str] = set()
            uids: set[str] = set()
            for el in self.root.iter():
                oid = el.get("ObjectID")
                if oid:
                    ids.add(oid)
                ouid = el.get("ObjectUID")
                if ouid:
                    uids.add(ouid)
            return ids, uids

        # Iteramos hasta estabilizar: eliminar un <Item> dangling puede
        # dejar a su padre vacio o romper otras suposiciones. Limite alto
        # por seguridad, normalmente converge en 1-2 pasadas.
        dangling_total = 0
        for _ in range(10):
            present_ids, present_uids = _collect_present()
            removed_this_pass = 0
            for container in self.root:
                if container.tag not in KEEP_TAGS:
                    continue
                for parent in list(container.iter()):
                    for child in list(parent):
                        ref = child.get("ObjectRef")
                        uref = child.get("ObjectURef")
                        if ref and ref not in present_ids:
                            parent.remove(child)
                            removed_this_pass += 1
                            continue
                        if uref and uref not in present_uids:
                            parent.remove(child)
                            removed_this_pass += 1
            dangling_total += removed_this_pass
            if removed_this_pass == 0:
                break

        # Validacion final: si algun Ref/URef sigue colgando en cualquier
        # parte del arbol, loggear warning con un ejemplo. No abortamos
        # (preferimos generar un prproj que casi seguro funciona a no
        # generar nada) pero avisamos para investigar.
        present_ids, present_uids = _collect_present()
        remaining = []
        for el in self.root.iter():
            ref = el.get("ObjectRef")
            if ref and ref not in present_ids:
                remaining.append((el.tag, "Ref", ref))
            uref = el.get("ObjectURef")
            if uref and uref not in present_uids:
                remaining.append((el.tag, "URef", uref))

        log.info("  XML limpiado: %d objetos eliminados, %d refs colgantes saneadas",
                 len(to_remove), dangling_total)
        if remaining:
            log.warning("  AVISO: quedan %d refs colgantes tras el saneado (Premiere puede rechazar el proyecto).",
                        len(remaining))
            sample_tags = sorted({t for t, _, _ in remaining})[:5]
            log.warning("    Tags afectados (ejemplo): %s", ", ".join(sample_tags))
            tag0, kind0, id0 = remaining[0]
            log.warning("    Primera: <%s %s=\"%s\">", tag0, kind0, id0)
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

# Patron para detectar rutas que son ruido binario: multiples caracteres no-ASCII
# seguidos o rutas con demasiados componentes (>15 niveles de profundidad).
_RE_BINARY_NOISE = re.compile(r'[\x80-\ufffc]{3,}')


def _is_plausible_path(p: str) -> bool:
    """Filtra falsos positivos del escaneo binario de AE.

    Descarta rutas que probablemente son basura binaria decodificada:
    - Contienen secuencias de caracteres no-ASCII (ruido binario)
    - Tienen mas de 15 niveles de profundidad
    - Contienen caracteres de control embebidos
    """
    if _RE_BINARY_NOISE.search(p):
        return False
    # Profundidad razonable (los proyectos reales rara vez pasan de 10)
    parts = p.replace("\\", "/").split("/")
    if len(parts) > 15:
        return False
    # El nombre del archivo debe tener al menos 2 caracteres antes de la extension
    name = Path(p).stem
    if len(name) < 2:
        return False
    return True


def _rewrite_aep_chunks(
    data: bytes, start: int, end: int,
    path_map: dict[str, str],
    stats: dict[str, int],
) -> bytes:
    """Procesa chunks RIFX entre start..end. Reescribe la clave 'fullpath' en
    cada chunk 'alas' cuyo valor este en path_map. Devuelve los bytes del
    rango procesado (las longitudes de LIST padres se recalculan al vuelo).
    """
    out = bytearray()
    pos = start
    while pos + 8 <= end:
        cid = data[pos:pos+4]
        clen = struct.unpack(">I", data[pos+4:pos+8])[0]
        payload_start = pos + 8
        payload_end = payload_start + clen
        if payload_end > end:
            # Chunk corrupto o fuera de rango: copiar bytes restantes tal cual
            out += data[pos:end]
            pos = end
            break

        if cid == b"LIST":
            form = data[payload_start:payload_start+4]
            new_children = _rewrite_aep_chunks(
                data, payload_start+4, payload_end, path_map, stats)
            new_clen = 4 + len(new_children)
            out += b"LIST" + struct.pack(">I", new_clen) + form + new_children
            if new_clen % 2:
                out += b"\x00"
        elif cid == b"alas":
            payload = data[payload_start:payload_end]
            new_payload = _maybe_rewrite_alas_payload(payload, path_map, stats)
            new_clen = len(new_payload)
            out += b"alas" + struct.pack(">I", new_clen) + new_payload
            if new_clen % 2:
                out += b"\x00"
        else:
            # Chunk opaco: copiar con su padding original
            raw_total = 8 + clen + (1 if clen % 2 else 0)
            out += data[pos:pos + raw_total]

        pos = payload_end
        if clen % 2:
            pos += 1

    return bytes(out)


def _maybe_rewrite_alas_payload(
    payload: bytes,
    path_map: dict[str, str],
    stats: dict[str, int],
) -> bytes:
    """Decodifica el JSON de un chunk 'alas' y reemplaza 'fullpath' si esta
    en path_map. Devuelve el payload original si no es JSON o no matchea."""
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload
    if not isinstance(obj, dict):
        return payload
    fp = obj.get("fullpath")
    if not isinstance(fp, str):
        return payload
    # Normalizar para matchear (rutas Mac pueden tener / u otras formas)
    new_fp = path_map.get(fp)
    if new_fp is None:
        new_fp = path_map.get(unicodedata.normalize("NFC", fp))
    if new_fp is None:
        return payload
    obj["fullpath"] = new_fp
    stats["rewritten"] = stats.get("rewritten", 0) + 1
    # ensure_ascii=False para preservar UTF-8 crudo (Adobe usa UTF-8 sin escapes)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def rewrite_aep_paths(
    aep_path: Path,
    path_map: dict[str, str],
    log: logging.Logger,
) -> dict[str, int]:
    """Reescribe rutas de footage dentro de un .aep (formato RIFX con chunks
    'alas' JSON). path_map mapea ruta_original_absoluta -> ruta_nueva_absoluta.

    Devuelve stats: {'rewritten': n, 'total_alas': n}. Si el .aep no es RIFX
    o no tiene chunks 'alas' reescribibles, no modifica el fichero.
    """
    stats = {"rewritten": 0, "total_alas": 0}
    try:
        data = aep_path.read_bytes()
    except OSError as exc:
        log.warning("    [AE-REWRITE] No se puede leer %s: %s", aep_path.name, exc)
        return stats

    if data[:4] != b"RIFX":
        log.warning("    [AE-REWRITE] %s no es RIFX, omitiendo", aep_path.name)
        return stats

    # Contar alas totales para el log (solo informativo)
    stats["total_alas"] = data.count(b"alas")  # aproximado; incluye coincidencias en payloads

    form = data[8:12]
    declared_body_size = struct.unpack(">I", data[4:8])[0]
    # El chunk root RIFX declara: 4 bytes form + contenido
    # body termina en 8 + declared_body_size
    body_end = min(8 + declared_body_size, len(data))
    new_body = _rewrite_aep_chunks(data, 12, body_end, path_map, stats)
    # Preservar bytes tras el cuerpo RIFX (trailing) si los hay
    trailing = data[body_end:]

    if stats["rewritten"] == 0:
        return stats  # no cambios: no reescribir el fichero

    new_size = 4 + len(new_body)  # form + body
    new_data = b"RIFX" + struct.pack(">I", new_size) + form + new_body + trailing
    try:
        aep_path.write_bytes(new_data)
    except OSError as exc:
        log.error("    [AE-REWRITE] Error escribiendo %s: %s", aep_path.name, exc)
        return stats

    return stats


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
                if (Path(p).suffix.lower() in _AE_FOOTAGE_EXTENSIONS
                        and _is_plausible_path(p)):
                    paths.add(p)
            except Exception:
                pass

    for m in _RE_AE_MAC_PATH.finditer(text):
        p = m.group(1)
        if len(p) <= 500 and p != exclude:
            try:
                if (Path(p).suffix.lower() in _AE_FOOTAGE_EXTENSIONS
                        and _is_plausible_path(p)):
                    paths.add(p)
            except Exception:
                pass

    return paths


class FileIndex:
    """Indice de archivos bajo un directorio, construido con un solo os.walk.

    Permite busqueda rapida por nombre de archivo con deteccion de ambiguedad
    y scoring por coincidencia de carpetas padre.  Tambien recolecta proyectos
    After Effects (.aep/.aepx) como subproducto del recorrido.
    """

    def __init__(
        self,
        project_root: Path,
        skip_dirs: frozenset[str] = frozenset(),
        exclude_folder: str = "",
    ) -> None:
        self._root = project_root
        self._by_name: dict[str, list[Path]] = {}
        self.ae_projects: set[str] = set()

        skip = skip_dirs
        if exclude_folder:
            skip = skip | {exclude_folder.lower()}

        self._skip = skip
        self._walk_root(project_root)

    def _walk_root(self, root: Path, collect_ae: bool = True) -> None:
        """Indexa todos los archivos bajo *root*."""
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d.lower() not in self._skip]
                for fname in filenames:
                    full = Path(dirpath) / fname
                    key = unicodedata.normalize("NFC", fname).lower()
                    self._by_name.setdefault(key, []).append(full)
                    if collect_ae and Path(fname).suffix.lower() in AE_PROJECT_EXTENSIONS:
                        self.ae_projects.add(str(full))
        except OSError:
            pass

    def _walk_root_excluding(self, root: Path, exclude: Path) -> None:
        """Indexa *root* pero salta el subarbol *exclude* (ya indexado)."""
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dp = Path(dirpath)
                # Si estamos dentro del subarbol excluido, podar
                try:
                    dp.relative_to(exclude)
                    dirnames.clear()
                    continue
                except ValueError:
                    pass
                # Podar el directorio excluido para no descender a el
                dirnames[:] = [
                    d for d in dirnames
                    if d.lower() not in self._skip
                    and not self._is_subpath(dp / d, exclude)
                ]
                for fname in filenames:
                    full = dp / fname
                    key = unicodedata.normalize("NFC", fname).lower()
                    self._by_name.setdefault(key, []).append(full)
        except OSError:
            pass

    @staticmethod
    def _is_subpath(candidate: Path, target: Path) -> bool:
        """True si target esta contenido en (o es igual a) candidate."""
        try:
            target.relative_to(candidate)
            return True
        except ValueError:
            return False

    def add_roots(self, roots: list[Path], log: logging.Logger) -> None:
        """Indexa directorios adicionales para resolucion de archivos offline.

        Solo anade al indice de nombres para busqueda; NO recolecta proyectos
        After Effects (esos solo se descubren desde la raiz del proyecto).
        """
        for root in roots:
            if not root.is_dir():
                log.warning("  Raiz de busqueda no encontrada: %s", root)
                continue
            # Evitar re-indexar si ya esta contenida en la raiz principal
            try:
                root.relative_to(self._root)
                continue  # ya indexado
            except ValueError:
                pass
            # Evitar caminar un root que CONTIENE la raiz principal.
            # Ej: root=V:\ y self._root=V:\AEDAS\Proyecto → caminar V:\
            # entero es redundante (ya tenemos V:\AEDAS\Proyecto indexado)
            # y extremadamente lento en un NAS.  Solo indexar lo que falta.
            try:
                self._root.relative_to(root)
                # root contiene self._root → caminar root EXCLUYENDO self._root
                log.info("  Raiz de busqueda %s contiene proyecto, escaneando sin duplicar...", root)
                self._walk_root_excluding(root, self._root)
                continue
            except ValueError:
                pass
            before = sum(len(v) for v in self._by_name.values())
            self._walk_root(root, collect_ae=False)
            after = sum(len(v) for v in self._by_name.values())
            added = after - before
            if added:
                log.info("  Raiz de busqueda adicional: %s (+%d archivos)",
                         root, added)

    @staticmethod
    def _suffix_score(original: Path, candidate: Path) -> int:
        """Cuenta componentes de carpeta padre que coinciden de derecha a izq."""
        orig_parts = [p.lower() for p in original.parent.parts]
        cand_parts = [p.lower() for p in candidate.parent.parts]
        score = 0
        for o, c in zip(reversed(orig_parts), reversed(cand_parts)):
            if o == c:
                score += 1
            else:
                break
        return score

    def resolve(self, src: Path, log: logging.Logger) -> Path | None:
        """Busca un archivo offline en el indice por nombre + scoring.

        Retorna None si no hay candidatos o si hay ambiguedad irresoluble.
        """
        key = unicodedata.normalize("NFC", src.name).lower()
        candidates = self._by_name.get(key, [])

        if not candidates:
            log.debug("    [NO ENCONTRADO] %s: no existe en arbol del proyecto", src.name)
            return None

        if len(candidates) == 1:
            return candidates[0]

        # Multiples candidatos: scoring por coincidencia de carpetas padre
        scored = [(self._suffix_score(src, c), c) for c in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)

        best_score, best = scored[0]
        second_score = scored[1][0]

        if best_score > second_score:
            return best

        # Empate -> ambiguo, no resolver
        log.warning(
            "    [AMBIGUO]  %s: %d coincidencias con mismo score, no resuelto",
            src.name, sum(1 for s, _ in scored if s == best_score),
        )
        log.debug(
            "               Candidatos: %s",
            [str(c) for _, c in scored],
        )
        return None


def _expand_ae_dependencies(
    target_paths: set[str],
    path_mappings: list[tuple[str, str]],
    log: logging.Logger,
    file_index: FileIndex | None = None,
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
            if not src.exists() and file_index is not None:
                resolved = file_index.resolve(src, log)
                if resolved is not None:
                    log.info("    [AE] %s resuelto -> %s", src.name, resolved)
                    src = resolved
            if not src.exists():
                log.warning(
                    "    [AE] %s (offline, dependencias no incluidas) - %s",
                    src.name, src,
                )
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
    extra_search_roots: list[Path] | None = None,
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
    selected_seqs: list[ET.Element] = []  # secuencia(s) elegida(s) (para limpiar XML)

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
            selected_list: list[dict] | None = None
            if sequence_callback is not None:
                # GUI callback: recibe ranked, devuelve lista de info dicts o None
                selected_list = sequence_callback(ranked)
            elif mode == "auto":
                s = select_sequence_auto(ranked, log)
                selected_list = [s] if s else None
            elif mode == "pattern":
                s = select_sequence_by_pattern(ranked, sequence_pattern, log)
                selected_list = [s] if s else None
            else:  # interactive
                s = select_sequence_interactive(ranked, log)
                selected_list = [s] if s else None

            if not selected_list:
                log.warning("  Sin secuencia seleccionada. Saltando proyecto.")
                return stats

            names = [s["name"] for s in selected_list]
            log.info("  Secuencia(s): %s", ", ".join(names))

            # Recolectar medios de todas las secuencias seleccionadas
            selected_seqs = [sel["element"] for sel in selected_list]
            target_paths: set[str] = set()
            for sel in selected_list:
                target_paths |= graph.collect_media_for_sequence(sel["element"])
            log.info("  Medios de %d secuencia(s): %d archivos",
                     len(selected_list), len(target_paths))

    # --- Construir indice de archivos (un solo os.walk) ---
    file_index = FileIndex(dest_root, _AE_SKIP_DIRS, exclude_folder=folder_name)

    # Agregar el directorio fuente del .prproj como raiz de busqueda.
    # En GUI dest_root ya ES la raiz del proyecto (no-op), pero en CLI
    # dest_root es el directorio de backup, asi que el arbol fuente no
    # estaria indexado sin esta linea.
    file_index.add_roots([prproj_path.parent], log)

    # --- Auto-descubrir directorios de archivos offline ---
    # En vez de caminar drives enteros (V:\, Z:\), derivar las carpetas
    # de busqueda a partir de las rutas de medios que no existen en disco.
    # Para cada ruta offline, buscar el ancestro mas cercano que exista
    # y agregarlo como raiz de busqueda (max 3 niveles arriba).
    auto_roots: set[str] = set()
    for orig in target_paths:
        normalized = normalize_media_path(orig)
        translated = translate_path(normalized, path_mappings)
        src = Path(translated)
        if src.exists():
            continue
        # Subir hasta encontrar un directorio que exista
        parent = src.parent
        for _ in range(3):
            if parent == parent.parent:
                break
            if parent.is_dir():
                auto_roots.add(str(parent))
                break
            parent = parent.parent

    if auto_roots:
        file_index.add_roots([Path(r) for r in sorted(auto_roots)], log)

    # Fallback: agregar la carpeta cliente (padre del project_root) para
    # encontrar medios que esten fuera del proyecto pero en la misma
    # jerarquia del cliente. Util cuando material estaba en disco externo.
    client_folder = dest_root.parent
    if client_folder.is_dir() and client_folder != dest_root:
        file_index.add_roots([client_folder], log)

    if extra_search_roots:
        file_index.add_roots(
            [Path(r) for r in extra_search_roots], log)

    # --- Proyectos After Effects referenciados por la secuencia ---
    ae_in_sequence = {
        p for p in target_paths
        if Path(p).suffix.lower() in AE_PROJECT_EXTENSIONS
    }
    if ae_in_sequence:
        log.info("  Proyectos After Effects en secuencia: %d", len(ae_in_sequence))

    # --- Expandir con dependencias de After Effects ---
    ae_extra = _expand_ae_dependencies(target_paths, path_mappings, log, file_index)
    if ae_extra:
        target_paths.update(ae_extra)

    if not target_paths:
        log.info("  Sin medios que copiar.")
        if not dry_run:
            project_folder.mkdir(parents=True, exist_ok=True)
            write_prproj(project_folder / prproj_path.name, serialize_prproj(root))
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

    # --- Resolver archivos offline buscando en la raiz del proyecto ---
    resolved_count = 0
    ambiguous_count = 0
    for orig in sorted(target_paths):
        src = src_map[orig]
        if src.exists():
            continue
        resolved = file_index.resolve(src, log)
        if resolved is None:
            # Contar ambiguos (resolve ya logueo [AMBIGUO] si aplica)
            key = unicodedata.normalize("NFC", src.name).lower()
            if len(file_index._by_name.get(key, [])) > 1:
                ambiguous_count += 1
            continue
        log.info("    [RESUELTO] %s -> %s", src.name, resolved)
        src_map[orig] = resolved
        resolved_count += 1
        # Recalcular destino segun nueva ubicacion
        try:
            rel = resolved.relative_to(project_root)
            clean_parts = [_clean_folder_name(p) for p in rel.parent.parts]
            clean_rel = Path(*clean_parts, rel.name) if clean_parts else rel
            path_map[orig] = project_folder / clean_rel
        except ValueError:
            path_map[orig] = media_dest_path(str(resolved), media_folder)

    if resolved_count or ambiguous_count:
        parts = []
        if resolved_count:
            parts.append(f"resueltos: {resolved_count}")
        if ambiguous_count:
            parts.append(f"ambiguos: {ambiguous_count}")
        log.info("  Archivos offline %s", " | ".join(parts))

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

    # --- Reescribir rutas dentro de los .aep copiados ---
    # Los .aep (RIFX) almacenan rutas de footage en chunks 'alas' con JSON.
    # Tras empaquetar, esas rutas siguen apuntando a la ubicacion original
    # (p.ej. /Volumes/...), asi que AE no encontrara el footage. Construimos
    # un mapa {ruta_original -> nueva_ruta_absoluta_en_el_paquete} y lo
    # aplicamos a cada .aep copiado.
    if not dry_run:
        aep_path_map: dict[str, str] = {}
        for orig in copied_origs:
            dst = path_map[orig]
            # AE almacena rutas Mac-style con forward slashes. Mantener ese
            # formato para maximizar la probabilidad de match si el .aep
            # venia de macOS. En Windows AE acepta ambas formas.
            aep_path_map[orig] = str(dst).replace("\\", "/")
            # Tambien registrar variantes que AE podria almacenar:
            # - la ruta normalizada NFC
            nfc = unicodedata.normalize("NFC", orig)
            if nfc != orig:
                aep_path_map[nfc] = aep_path_map[orig]

        aep_files_copied = [
            path_map[o] for o in copied_origs
            if Path(o).suffix.lower() in AE_PROJECT_EXTENSIONS
               and path_map[o].exists()
               and path_map[o].suffix.lower() == ".aep"
        ]
        if aep_files_copied:
            total_rewritten = 0
            for aep_dst in aep_files_copied:
                rs = rewrite_aep_paths(aep_dst, aep_path_map, log)
                total_rewritten += rs.get("rewritten", 0)
            if total_rewritten:
                log.info("  Rutas reescritas en %d .aep: %d footage links actualizados",
                         len(aep_files_copied), total_rewritten)
            else:
                log.info("  .aep procesados: %d (sin cambios, formato no reconocido o rutas ya correctas)",
                         len(aep_files_copied))

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

    # --- Limpiar XML: DESACTIVADO ---
    # La poda (trim_to_sequence) generaba referencias colgantes que Premiere
    # rechazaba con "el proyecto parece danado". Preservar el arbol completo
    # es mas pesado (+15MB tipico) pero fiable al 100%.
    if False and selected_seqs:
        if dry_run:
            # Calcular cuantos se eliminarian sin modificar el arbol
            needed_ids, needed_uids = graph.collect_reachable(selected_seqs)
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
            graph.trim_to_sequence(selected_seqs, log)

    # --- Reescribir rutas de medios copiados en el XML ---
    # Solo reescribir archivos que se copiaron (no offline).
    # Usar rutas relativas al .prproj para que el proyecto sea portable.
    all_media_elems = graph.find_all_media_path_elements()
    # orig_to_dst: map de ruta absoluta original -> destino en el paquete
    orig_to_dst: dict[str, Path] = {}
    for _elem, orig in all_media_elems:
        if orig in copied_origs:
            orig_to_dst[orig] = path_map[orig]

    # Paso 1: reescribir <RelativePath> ANTES de tocar los hermanos absolutos,
    # porque los emparejamos leyendo la ruta absoluta original del hermano.
    # Premiere usa RelativePath como ruta alternativa de resolucion; si queda
    # apuntando a la estructura original rompe el linking de proyectos AE
    # vinculados y de medios en general.
    prproj_dst = project_folder / prproj_path.name
    rewritten_relpaths = 0
    for parent in root.iter():
        rel_el = None
        abs_orig = None
        for child in parent:
            if child.tag == "RelativePath" and child.text:
                rel_el = child
            elif child.tag in ("FilePath", "ActualMediaFilePath", "MediaFilePath") \
                    and child.text and is_absolute_path(child.text.strip()):
                abs_orig = child.text.strip()
        if rel_el is None or abs_orig is None:
            continue
        dst = orig_to_dst.get(abs_orig)
        if dst is None:
            continue
        try:
            rel_from_prproj = os.path.relpath(dst, prproj_dst.parent)
            rel_el.text = rel_from_prproj.replace("\\", "/")
            rewritten_relpaths += 1
        except ValueError:
            pass

    if rewritten_relpaths:
        log.info("  Rutas relativas reescritas: %d", rewritten_relpaths)

    # Paso 2: reescribir rutas absolutas (FilePath, ActualMediaFilePath, etc.)
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
        write_prproj(project_folder / prproj_path.name, serialize_prproj(root))
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
    parser.add_argument(
        "--search",
        action="append",
        metavar="RUTA",
        help=(
            "Directorio adicional donde buscar archivos offline.\n"
            "Se puede repetir: --search V:\\AEDAS --search D:\\Dropbox"
        ),
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

    search_roots = [Path(s) for s in (args.search or [])]
    if search_roots:
        log.info("  Busqueda adicional:")
        for sr in search_roots:
            log.info("    %s", sr)
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
            extra_search_roots=search_roots,
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
