#!/usr/bin/env python3
"""
test_suite.py - Suite completa de tests para premiere-packager.

Cubre:
  - Utilidades de rutas (translate, normalize, is_absolute, etc.)
  - PrprojGraph (parseo XML, navegacion del grafo, extraccion de medios)
  - Ranking de secuencias
  - FileIndex (resolucion offline con todos los fallbacks)
  - Escaneo de After Effects
  - Flujo completo de empaquetado (package_project)
"""

import gzip
import logging
import os
import shutil
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

from empaquetar_premiere import (
    AE_PROJECT_EXTENSIONS,
    FileIndex,
    PrprojGraph,
    _AE_SKIP_DIRS,
    _clean_folder_name,
    _expand_ae_dependencies,
    _fmt_size,
    _is_plausible_path,
    _scan_aep_for_footage,
    is_absolute_path,
    is_auto_nested_name,
    list_sequences,
    media_dest_path,
    normalize_media_path,
    package_project,
    parse_path_mappings,
    read_prproj,
    score_sequence,
    select_sequence_auto,
    select_sequence_by_pattern,
    translate_path,
    unique_folder_name,
    write_prproj,
)

logging.basicConfig(level=logging.DEBUG, format="%(message)s")
log = logging.getLogger("test")

PASSED = 0
FAILED = 0


def check(name: str, got, expected):
    global PASSED, FAILED
    ok = got == expected
    symbol = "PASS" if ok else "FAIL"
    print(f"  [{symbol}] {name}")
    if ok:
        PASSED += 1
    else:
        FAILED += 1
        print(f"         esperado: {expected!r}")
        print(f"         obtenido: {got!r}")


def check_true(name: str, condition: bool):
    check(name, condition, True)


def check_none(name: str, val):
    check(name, val is None, True)


def check_not_none(name: str, val):
    check(name, val is not None, True)


# =====================================================================
# Helpers para construir XML de .prproj sinteticos
# =====================================================================

_NEXT_ID = [1]


def _id():
    """Genera ObjectID incremental."""
    val = str(_NEXT_ID[0])
    _NEXT_ID[0] += 1
    return val


def _uid():
    """Genera ObjectUID unico."""
    val = f"uid-{_NEXT_ID[0]}"
    _NEXT_ID[0] += 1
    return val


def _make_media(path: str, synthetic: bool = False, proxy: bool = False) -> tuple[str, str]:
    """Crea un elemento Media con ActualMediaFilePath. Retorna (id, xml_str)."""
    mid = _id()
    state = "00000000-0000-0000-0000-000000000000" if synthetic else "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    proxy_el = f"<IsProxy>true</IsProxy>" if proxy else ""
    xml = f'''<Media ObjectID="{mid}">
        <ActualMediaFilePath>{path}</ActualMediaFilePath>
        <ContentAndMetadataState>{state}</ContentAndMetadataState>
        {proxy_el}
    </Media>'''
    return mid, xml


def _make_media_source(media_id: str, tag: str = "VideoMediaSource") -> tuple[str, str]:
    sid = _id()
    xml = f'''<{tag} ObjectID="{sid}">
        <MediaSource><Media ObjectRef="{media_id}"/></MediaSource>
    </{tag}>'''
    return sid, xml


def _make_sequence_source(seq_uid: str, tag: str = "VideoSequenceSource") -> tuple[str, str]:
    sid = _id()
    xml = f'''<{tag} ObjectID="{sid}">
        <SequenceSource><Sequence ObjectURef="{seq_uid}"/></SequenceSource>
    </{tag}>'''
    return sid, xml


def _make_clip(source_id: str, tag: str = "VideoClip") -> tuple[str, str]:
    cid = _id()
    xml = f'''<{tag} ObjectID="{cid}">
        <Source ObjectRef="{source_id}"/>
    </{tag}>'''
    return cid, xml


def _make_subclip(clip_id: str) -> tuple[str, str]:
    scid = _id()
    xml = f'''<SubClip ObjectID="{scid}">
        <Clip><Clip ObjectRef="{clip_id}"/></Clip>
    </SubClip>'''
    return scid, xml


def _make_track_item(subclip_id: str, tag: str = "VideoClipTrackItem") -> tuple[str, str]:
    tid = _id()
    xml = f'''<{tag} ObjectID="{tid}">
        <ClipTrackItem><SubClip ObjectRef="{subclip_id}"/></ClipTrackItem>
    </{tag}>'''
    return tid, xml


def _make_track(item_ids: list[str], tag: str = "VideoClipTrack") -> tuple[str, str]:
    trid = _id()
    items = "\n".join(f'<TrackItem ObjectRef="{i}"/>' for i in item_ids)
    xml = f'''<{tag} ObjectID="{trid}">
        <ClipTrack><ClipItems><TrackItems>
            {items}
        </TrackItems></ClipItems></ClipTrack>
    </{tag}>'''
    return trid, xml


def _make_track_group(track_ids: list[str], tag: str = "VideoTrackGroup") -> tuple[str, str]:
    gid = _id()
    tracks = "\n".join(f'<Track ObjectRef="{i}"/>' for i in track_ids)
    xml = f'''<{tag} ObjectID="{gid}">
        <TrackGroup><Tracks>
            {tracks}
        </Tracks></TrackGroup>
    </{tag}>'''
    return gid, xml


def _make_sequence(name: str, track_group_ids: list[tuple[str, str]],
                   uid: str | None = None) -> tuple[str, str, str]:
    """Construye Sequence. track_group_ids = [(media_type_uuid, group_id), ...].
    Retorna (object_id, object_uid, xml_str)."""
    sid = _id()
    suid = uid or _uid()
    tg_xml = ""
    for mt_uuid, gid in track_group_ids:
        tg_xml += f'''<TrackGroup>
            <First>{mt_uuid}</First>
            <Second ObjectRef="{gid}"/>
        </TrackGroup>\n'''
    xml = f'''<Sequence ObjectID="{sid}" ObjectUID="{suid}">
        <Name>{name}</Name>
        <TrackGroups>{tg_xml}</TrackGroups>
    </Sequence>'''
    return sid, suid, xml


def build_prproj_xml(elements: list[str], extra: str = "") -> str:
    """Arma un XML de .prproj completo."""
    body = "\n".join(elements)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<PremiereData Version="1">
    <Project ObjectID="0"><Name>TestProject</Name></Project>
    {extra}
    {body}
</PremiereData>'''


def build_simple_prproj(media_paths: list[str], seq_name: str = "Main Edit",
                        nested_paths: list[str] | None = None,
                        synthetic_paths: list[str] | None = None,
                        proxy_paths: list[str] | None = None) -> str:
    """Construye un .prproj con una secuencia principal que referencia los medios dados.

    Opcionalmente agrega una secuencia anidada con sus propios medios,
    medios sinteticos (barras/tono) y proxies que deben ser filtrados.
    """
    _NEXT_ID[0] = 100  # reset para consistencia
    elements = []
    track_item_ids = []

    # Medios normales
    for mp in media_paths:
        mid, mx = _make_media(mp)
        elements.append(mx)
        sid, sx = _make_media_source(mid)
        elements.append(sx)
        cid, cx = _make_clip(sid)
        elements.append(cx)
        scid, scx = _make_subclip(cid)
        elements.append(scx)
        tiid, tix = _make_track_item(scid)
        elements.append(tix)
        track_item_ids.append(tiid)

    # Medios sinteticos (deben ser filtrados)
    for sp in (synthetic_paths or []):
        mid, mx = _make_media(sp, synthetic=True)
        elements.append(mx)
        sid, sx = _make_media_source(mid)
        elements.append(sx)
        cid, cx = _make_clip(sid)
        elements.append(cx)
        scid, scx = _make_subclip(cid)
        elements.append(scx)
        tiid, tix = _make_track_item(scid)
        elements.append(tix)
        track_item_ids.append(tiid)

    # Proxies (deben ser filtrados)
    for pp in (proxy_paths or []):
        mid, mx = _make_media(pp, proxy=True)
        elements.append(mx)
        sid, sx = _make_media_source(mid)
        elements.append(sx)
        cid, cx = _make_clip(sid)
        elements.append(cx)
        scid, scx = _make_subclip(cid)
        elements.append(scx)
        tiid, tix = _make_track_item(scid)
        elements.append(tix)
        track_item_ids.append(tiid)

    # Secuencia anidada (opcional)
    nested_uid = None
    if nested_paths:
        nested_item_ids = []
        for np in nested_paths:
            mid, mx = _make_media(np)
            elements.append(mx)
            sid, sx = _make_media_source(mid)
            elements.append(sx)
            cid, cx = _make_clip(sid)
            elements.append(cx)
            scid, scx = _make_subclip(cid)
            elements.append(scx)
            tiid, tix = _make_track_item(scid)
            elements.append(tix)
            nested_item_ids.append(tiid)

        ntrid, ntrx = _make_track(nested_item_ids)
        elements.append(ntrx)
        ngid, ngx = _make_track_group([ntrid])
        elements.append(ngx)
        _nsid, nested_uid, nsx = _make_sequence(
            "Nested Seq", [("video-mt", ngid)])
        elements.append(nsx)

        # Agregar referencia a la secuencia anidada en la principal
        ssid, ssx = _make_sequence_source(nested_uid)
        elements.append(ssx)
        cid, cx = _make_clip(ssid)
        elements.append(cx)
        scid, scx = _make_subclip(cid)
        elements.append(scx)
        tiid, tix = _make_track_item(scid)
        elements.append(tix)
        track_item_ids.append(tiid)

    # Track y TrackGroup principales
    trid, trx = _make_track(track_item_ids)
    elements.append(trx)
    gid, gx = _make_track_group([trid])
    elements.append(gx)

    # Secuencia principal
    _msid, _msuid, msx = _make_sequence(seq_name, [("video-mt", gid)])
    elements.append(msx)

    return build_prproj_xml(elements)


def write_test_prproj(tmp: Path, xml_str: str, name: str = "test.prproj") -> Path:
    """Escribe un .prproj comprimido con gzip en tmp/."""
    prproj_path = tmp / name
    xml_bytes = xml_str.encode("utf-8")
    with gzip.open(prproj_path, "wb") as f:
        f.write(xml_bytes)
    return prproj_path


# =====================================================================
# 1. UTILIDADES DE RUTAS
# =====================================================================

def test_is_absolute_path():
    print("\n=== Test: is_absolute_path ===")
    check("Windows drive", is_absolute_path("C:\\Users\\file.mov"), True)
    check("Windows forward slash", is_absolute_path("D:/Media/clip.mp4"), True)
    check("UNC path", is_absolute_path("\\\\server\\share\\file.mov"), True)
    check("UNC forward", is_absolute_path("//server/share/file.mov"), True)
    check("Mac path", is_absolute_path("/Volumes/DISCO/file.mov"), True)
    check("Unix path", is_absolute_path("/home/user/file.mov"), True)
    check("relative path", is_absolute_path("Media/clip.mp4"), False)
    check("empty string", is_absolute_path(""), False)
    check("short string", is_absolute_path("ab"), False)
    check("dot relative", is_absolute_path("./file.mov"), False)
    check("relative dot-dot", is_absolute_path("../file.mov"), False)


def test_normalize_media_path():
    print("\n=== Test: normalize_media_path ===")
    # NFC normalization
    nfd = unicodedata.normalize("NFD", "/Volumes/música/canción.wav")
    result = normalize_media_path(nfd)
    check_true("NFC normalization aplicada",
               unicodedata.is_normalized("NFC", result))

    # Mac path normalization
    result = normalize_media_path("/Volumes/DISCO/./SubDir/../file.mov")
    check("Mac dot-dot normalizado", result, "/Volumes/DISCO/file.mov")

    # Windows path: normaliza barras (no resuelve .. sin resolve())
    result = normalize_media_path("D:/Media//clip.mov")
    check_true("Windows doble slash normalizado", "//" not in result)


def test_parse_path_mappings():
    print("\n=== Test: parse_path_mappings ===")
    # Caso normal
    mappings = parse_path_mappings(["/Volumes/A=V:", "/Volumes/B=W:"])
    check("dos mapeos", len(mappings), 2)
    # Ordenados por longitud descendente
    check("mas largo primero", mappings[0][0], "/Volumes/A")

    # Sin mapeos
    mappings = parse_path_mappings(None)
    check("None -> vacio", mappings, [])
    mappings = parse_path_mappings([])
    check("lista vacia -> vacio", mappings, [])

    # Error de formato
    try:
        parse_path_mappings(["sin-igual"])
        check("formato invalido lanza error", False, True)
    except ValueError:
        check("formato invalido lanza error", True, True)

    # Trailing slashes
    mappings = parse_path_mappings(["/Volumes/DISCO/=V:\\"])
    check("trailing slash removido mac", mappings[0][0], "/Volumes/DISCO")
    check("trailing slash removido win", mappings[0][1], "V:")


def test_translate_path():
    print("\n=== Test: translate_path ===")
    mappings = parse_path_mappings([
        "/Volumes/SEGUIMIENTOS=V:",
        "/Volumes/NAS-Dropbox/DATA/SEGUIMIENTOS=V:",
    ])

    result = translate_path("/Volumes/SEGUIMIENTOS/Proyecto/media.mov", mappings)
    check_true("traduce Mac a Win", result.startswith("V:"))
    check_true("mantiene resto de la ruta", "Proyecto" in result)

    # Sin match
    result = translate_path("/Volumes/OTRO/file.mov", mappings)
    check("sin match devuelve original", result, "/Volumes/OTRO/file.mov")

    # Sin mapeos
    result = translate_path("D:/file.mov", [])
    check("sin mapeos devuelve original", result, "D:/file.mov")

    # Match mas especifico primero
    result = translate_path(
        "/Volumes/NAS-Dropbox/DATA/SEGUIMIENTOS/clip.mov", mappings)
    check_true("match especifico", result.startswith("V:"))


def test_media_dest_path():
    print("\n=== Test: media_dest_path ===")
    media_folder = Path("E:/Backup/Proyecto/Otros")

    # Ruta Windows
    result = media_dest_path("D:\\Media\\Clips\\video.mov", media_folder)
    check_true("drive D en la ruta", "D" in str(result))
    check("nombre archivo preservado", result.name, "video.mov")

    # Ruta Mac
    result = media_dest_path("/Volumes/DISCO/Media/audio.wav", media_folder)
    check("nombre archivo preservado mac", result.name, "audio.wav")


def test_clean_folder_name():
    print("\n=== Test: _clean_folder_name ===")
    check("prefijo con punto", _clean_folder_name("1. Material"), "Material")
    check("prefijo con guion", _clean_folder_name("2- Projects"), "Projects")
    check("prefijo con espacio", _clean_folder_name("3 Renders"), "Renders")
    check("sin prefijo", _clean_folder_name("Media"), "Media")
    check("doble digito", _clean_folder_name("10. Exports"), "Exports")


def test_unique_folder_name():
    print("\n=== Test: unique_folder_name ===")
    used = set()
    name1 = unique_folder_name("Proyecto", used)
    check("primero sin sufijo", name1, "Proyecto")
    name2 = unique_folder_name("Proyecto", used)
    check("segundo con _2", name2, "Proyecto_2")
    name3 = unique_folder_name("Proyecto", used)
    check("tercero con _3", name3, "Proyecto_3")
    name4 = unique_folder_name("Otro", used)
    check("nombre diferente sin sufijo", name4, "Otro")


def test_is_auto_nested_name():
    print("\n=== Test: is_auto_nested_name ===")
    check("secuencia anidada", is_auto_nested_name("Secuencia anidada 01"), True)
    check("nested sequence", is_auto_nested_name("Nested Sequence 03"), True)
    check("nombre normal", is_auto_nested_name("Main Edit"), False)
    check("contiene pero no empieza", is_auto_nested_name("My Nested Sequence"), False)


def test_fmt_size():
    print("\n=== Test: _fmt_size ===")
    check("bytes", _fmt_size(500), "500 B")
    check("KB", _fmt_size(5120), "5 KB")
    check("MB", _fmt_size(5 * 1024 * 1024), "5 MB")
    check("GB", _fmt_size(int(1.5 * 1024 ** 3)), "1.5 GB")


# =====================================================================
# 2. PRPROJ GRAPH - parseo y navegacion
# =====================================================================

def test_prproj_graph_basic():
    print("\n=== Test: PrprojGraph basico ===")
    xml_str = build_simple_prproj(
        ["D:/Media/video.mov", "D:/Media/audio.wav"],
        seq_name="Final Edit",
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)

    seqs = graph.find_sequences()
    check("encuentra 1 secuencia", len(seqs), 1)
    check("nombre correcto", graph.sequence_name(seqs[0]), "Final Edit")


def test_prproj_graph_media_collection():
    print("\n=== Test: PrprojGraph recoleccion de medios ===")
    xml_str = build_simple_prproj(
        ["D:/Media/video.mov", "D:/Media/audio.wav"],
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()

    media = graph.collect_media_for_sequence(seqs[0])
    check("recolecta 2 medios", len(media), 2)
    check_true("incluye video.mov", "D:/Media/video.mov" in media)
    check_true("incluye audio.wav", "D:/Media/audio.wav" in media)


def test_prproj_graph_nested_sequence():
    print("\n=== Test: PrprojGraph secuencias anidadas ===")
    xml_str = build_simple_prproj(
        ["D:/Media/main.mov"],
        nested_paths=["D:/Media/nested_clip.mp4", "D:/Media/nested_audio.wav"],
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()

    # Deberia haber 2 secuencias: main y nested
    check("encuentra 2 secuencias", len(seqs), 2)

    # Buscar la secuencia principal (Main Edit)
    main_seq = None
    for s in seqs:
        if graph.sequence_name(s) == "Main Edit":
            main_seq = s
            break
    check_not_none("encuentra Main Edit", main_seq)

    if main_seq:
        media = graph.collect_media_for_sequence(main_seq)
        check("recolecta 3 medios (main + nested)", len(media), 3)
        check_true("incluye main.mov", "D:/Media/main.mov" in media)
        check_true("incluye nested_clip.mp4", "D:/Media/nested_clip.mp4" in media)
        check_true("incluye nested_audio.wav", "D:/Media/nested_audio.wav" in media)


def test_prproj_graph_filters_synthetic():
    print("\n=== Test: PrprojGraph filtra medios sinteticos ===")
    xml_str = build_simple_prproj(
        ["D:/Media/real.mov"],
        synthetic_paths=["D:/Synthetic/bars.mov", "D:/Synthetic/tone.wav"],
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()
    media = graph.collect_media_for_sequence(seqs[0])
    check("solo 1 medio real", len(media), 1)
    check_true("barras filtradas", "bars.mov" not in str(media))
    check_true("tono filtrado", "tone.wav" not in str(media))


def test_prproj_graph_filters_proxy():
    print("\n=== Test: PrprojGraph filtra proxies ===")
    xml_str = build_simple_prproj(
        ["D:/Media/real.mov"],
        proxy_paths=["D:/Proxies/real_proxy.mov"],
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()
    media = graph.collect_media_for_sequence(seqs[0])
    check("solo 1 medio real", len(media), 1)
    check_true("proxy filtrado", "proxy" not in str(media).lower())


def test_prproj_graph_nesting_graph():
    print("\n=== Test: PrprojGraph grafo de anidamiento ===")
    xml_str = build_simple_prproj(
        ["D:/Media/main.mov"],
        nested_paths=["D:/Media/nested.mp4"],
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()
    nesting = graph.build_nesting_graph(seqs)

    # La secuencia principal tiene hijos, la anidada no
    main_uid = None
    nested_uid = None
    for s in seqs:
        uid = graph.sequence_uid(s)
        if graph.sequence_name(s) == "Main Edit":
            main_uid = uid
        else:
            nested_uid = uid

    if main_uid and nested_uid:
        check_true("main tiene hijo nested",
                    nested_uid in nesting.get(main_uid, set()))
        check("nested no tiene hijos",
              len(nesting.get(nested_uid, set())), 0)


def test_prproj_graph_sequence_info():
    print("\n=== Test: PrprojGraph sequence_info ===")
    xml_str = build_simple_prproj(
        ["D:/a.mov", "D:/b.mov", "D:/c.mov"],
        seq_name="Final Master",
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()
    info = graph.sequence_info(seqs[0])

    check("nombre", info["name"], "Final Master")
    check("3 clips", info["clip_count"], 3)
    check_true("tiene video tracks", info["video_tracks"] >= 1)


def test_prproj_graph_empty():
    print("\n=== Test: PrprojGraph sin secuencias ===")
    xml_str = build_prproj_xml([])
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    seqs = graph.find_sequences()
    check("0 secuencias", len(seqs), 0)


def test_prproj_graph_trim():
    print("\n=== Test: PrprojGraph trim_to_sequence ===")
    xml_str = build_simple_prproj(
        ["D:/Media/used.mov"],
        seq_name="Keep",
    )
    # Agregar otra secuencia que no esta referenciada
    _NEXT_ID[0] = 900
    extra_mid, extra_mx = _make_media("D:/Media/unused.mov")
    extra_sid, extra_sx = _make_media_source(extra_mid)
    extra_cid, extra_cx = _make_clip(extra_sid)
    extra_scid, extra_scx = _make_subclip(extra_cid)
    extra_tiid, extra_tix = _make_track_item(extra_scid)
    extra_trid, extra_trx = _make_track([extra_tiid])
    extra_gid, extra_gx = _make_track_group([extra_trid])
    _esid, _esuid, extra_seqx = _make_sequence("Unused Seq", [("v", extra_gid)])

    # Insertar manualmente en el XML
    root = ET.fromstring(xml_str)
    for elem_str in [extra_mx, extra_sx, extra_cx, extra_scx, extra_tix,
                     extra_trx, extra_gx, extra_seqx]:
        elem = ET.fromstring(elem_str)
        root.append(elem)

    graph = PrprojGraph(root)
    seqs = graph.find_sequences()
    keep_seq = None
    for s in seqs:
        if graph.sequence_name(s) == "Keep":
            keep_seq = s
            break

    count_before = len(list(root))
    removed = graph.trim_to_sequence(keep_seq, log)
    count_after = len(list(root))

    check_true("se eliminaron elementos", removed > 0)
    check_true("XML mas chico despues del trim", count_after < count_before)


def test_prproj_graph_find_all_media_paths():
    print("\n=== Test: PrprojGraph find_all_media_path_elements ===")
    xml_str = build_simple_prproj(
        ["D:/Media/a.mov", "D:/Media/b.wav"],
    )
    root = ET.fromstring(xml_str)
    graph = PrprojGraph(root)
    elems = graph.find_all_media_path_elements()
    paths = [p for _, p in elems]
    check_true("encuentra a.mov", "D:/Media/a.mov" in paths)
    check_true("encuentra b.wav", "D:/Media/b.wav" in paths)


# =====================================================================
# 3. RANKING DE SECUENCIAS
# =====================================================================

def test_score_sequence_root_with_children():
    print("\n=== Test: score_sequence - raiz con hijos ===")
    infos = [
        {"uid": "main", "name": "Final Edit", "clip_count": 50,
         "video_tracks": 3, "audio_tracks": 4, "nested_count": 2},
        {"uid": "nest1", "name": "Secuencia anidada 01", "clip_count": 5,
         "video_tracks": 1, "audio_tracks": 1, "nested_count": 0},
    ]
    nesting = {"main": {"nest1"}, "nest1": set()}

    score_main = score_sequence(infos[0], nesting, infos)
    score_nest = score_sequence(infos[1], nesting, infos)

    check_true("raiz > hoja", score_main > score_nest)


def test_score_sequence_promote_demote():
    print("\n=== Test: score_sequence - promote/demote patterns ===")
    base = {"uid": "a", "clip_count": 10, "video_tracks": 2,
            "audio_tracks": 2, "nested_count": 0}
    nesting = {"a": set(), "b": set()}
    infos_ctx = [base]

    # Nombre que promueve
    promoted = {**base, "uid": "a", "name": "Final Master"}
    score_p = score_sequence(promoted, nesting, infos_ctx)

    # Nombre que demota
    demoted = {**base, "uid": "b", "name": "Instagram Reel"}
    score_d = score_sequence(demoted, {"a": set(), "b": set()}, infos_ctx)

    check_true("promoted > demoted", score_p > score_d)


def test_score_sequence_clip_density():
    print("\n=== Test: score_sequence - densidad de clips ===")
    many = {"uid": "a", "name": "Edit", "clip_count": 100,
            "video_tracks": 2, "audio_tracks": 2, "nested_count": 0}
    few = {"uid": "b", "name": "Edit", "clip_count": 5,
           "video_tracks": 2, "audio_tracks": 2, "nested_count": 0}
    infos = [many, few]
    nesting = {"a": set(), "b": set()}

    score_many = score_sequence(many, nesting, infos)
    score_few = score_sequence(few, nesting, infos)
    check_true("mas clips > menos clips", score_many > score_few)


# =====================================================================
# 4. FILE INDEX - resolucion offline exhaustiva
# =====================================================================

def test_file_index_extra_search_roots():
    """FileIndex.add_roots agrega archivos de directorios externos."""
    print("\n=== Test: FileIndex extra search roots ===")
    with tempfile.TemporaryDirectory() as tmp1, \
         tempfile.TemporaryDirectory() as tmp2:
        root = Path(tmp1)
        extra = Path(tmp2)

        # Archivo solo en root extra
        (extra / "Footage").mkdir()
        (extra / "Footage" / "external.mov").write_text("x")

        idx = FileIndex(root)
        check_none("antes de add_roots no encuentra",
                   idx.resolve(Path("D:/external.mov"), log))

        idx.add_roots([extra], log)
        result = idx.resolve(Path("D:/external.mov"), log)
        check_not_none("despues de add_roots encuentra", result)
        if result:
            check("apunta al archivo correcto", result.name, "external.mov")


def test_file_index_extra_root_does_not_override():
    """Archivos del root principal no se duplican al agregar extras."""
    print("\n=== Test: FileIndex extra root no duplica ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Media").mkdir()
        (root / "Media" / "clip.mov").write_text("x")

        idx = FileIndex(root)
        # add_roots con el mismo root no deberia duplicar
        idx.add_roots([root], log)

        key = "clip.mov"
        candidates = idx._by_name.get(key, [])
        check("no duplica archivos", len(candidates), 1)


def test_file_index_extra_root_nonexistent():
    """add_roots con directorio inexistente no rompe."""
    print("\n=== Test: FileIndex extra root inexistente ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        idx = FileIndex(root)
        # No debe lanzar excepcion
        idx.add_roots([Path("Z:/NoExiste/Nada")], log)
        check("sobrevive a root inexistente", True, True)


def test_file_index_suffix_score_deep():
    """Score con muchos niveles de coincidencia."""
    print("\n=== Test: FileIndex suffix_score profundo ===")
    score = FileIndex._suffix_score(
        Path("D:/Dropbox/Cliente/Boda/Footage/Camara1/Day1/clip.mp4"),
        Path("V:/Cliente/Boda/Footage/Camara1/Day1/clip.mp4"),
    )
    check("5 carpetas coinciden", score, 5)


def test_file_index_resolve_prefers_deeper_match():
    """Con 3+ candidatos, resuelve al que tiene mas carpetas en comun."""
    print("\n=== Test: FileIndex resuelve al match mas profundo ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Candidato 1: solo nombre coincide
        (root / "Random").mkdir()
        (root / "Random" / "video.mov").write_text("1")
        # Candidato 2: 1 carpeta coincide (Media)
        (root / "Media").mkdir()
        (root / "Media" / "video.mov").write_text("2")
        # Candidato 3: 2 carpetas coinciden (Footage/Day1)
        (root / "Footage" / "Day1").mkdir(parents=True)
        (root / "Footage" / "Day1" / "video.mov").write_text("3")

        idx = FileIndex(root)
        src = Path("D:/Project/Footage/Day1/video.mov")
        result = idx.resolve(src, log)
        check_not_none("resuelve", result)
        if result:
            check_true("elige Footage/Day1",
                        "Footage" in str(result) and "Day1" in str(result))


def test_file_index_with_path_translation():
    """Flujo completo: traducir ruta Mac, luego resolver offline."""
    print("\n=== Test: FileIndex con traduccion de ruta ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Footage" / "Camara1").mkdir(parents=True)
        (root / "Footage" / "Camara1" / "clip.mov").write_text("x")

        idx = FileIndex(root)

        # Simular lo que hace package_project:
        # 1. Traducir ruta Mac -> Windows
        mac_path = "/Volumes/SEGUIMIENTOS/Proyecto/Footage/Camara1/clip.mov"
        mappings = parse_path_mappings(["/Volumes/SEGUIMIENTOS=V:"])
        translated = translate_path(normalize_media_path(mac_path), mappings)
        src = Path(translated)

        # 2. src no existe en disco, resolver via FileIndex
        result = idx.resolve(src, log)
        check_not_none("resuelve despues de traduccion", result)
        if result:
            check("archivo correcto", result.name, "clip.mov")


def test_file_index_unicode_nfd_from_mac():
    """Archivos con acentos creados en Mac (NFD) se resuelven correctamente."""
    print("\n=== Test: FileIndex unicode NFD desde Mac ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Archivo con NFC en disco (Windows)
        nfc_name = unicodedata.normalize("NFC", "presentacion.mov")
        (root / nfc_name).write_text("x")

        idx = FileIndex(root)

        # Ruta del .prproj guardada en NFD (como la guarda macOS)
        nfd_name = unicodedata.normalize("NFD", "presentacion.mov")
        src = Path(f"D:/Proyecto/{nfd_name}")
        result = idx.resolve(src, log)
        check_not_none("resuelve NFD -> NFC", result)


def test_file_index_mixed_extra_roots_disambiguation():
    """Candidatos de raiz principal y extra root se desambiguan por score."""
    print("\n=== Test: FileIndex disambiguation con extra roots ===")
    with tempfile.TemporaryDirectory() as tmp1, \
         tempfile.TemporaryDirectory() as tmp2:
        project = Path(tmp1)
        extra = Path(tmp2)

        # En proyecto: archivo sin contexto de carpeta
        (project / "clip.mov").write_text("x")

        # En extra: archivo con carpeta que coincide con la ruta original
        (extra / "Footage" / "Camara1").mkdir(parents=True)
        (extra / "Footage" / "Camara1" / "clip.mov").write_text("y")

        idx = FileIndex(project)
        idx.add_roots([extra], log)

        src = Path("D:/Dropbox/Proyecto/Footage/Camara1/clip.mov")
        result = idx.resolve(src, log)
        check_not_none("resuelve al mejor score", result)
        if result:
            check_true("elige el de extra root con mejor match",
                        "Camara1" in str(result))


def test_file_index_parent_root_no_duplicate():
    """add_roots con V:\\ cuando self._root es V:\\AEDAS\\Proyecto no re-indexa
    el subarbol del proyecto, pero SI indexa el resto del drive."""
    print("\n=== Test: FileIndex parent root no duplica proyecto ===")
    with tempfile.TemporaryDirectory() as tmp:
        drive = Path(tmp)
        # Simular: V:\AEDAS\Proyecto (ya indexado como root)
        project = drive / "AEDAS" / "Proyecto"
        (project / "Media").mkdir(parents=True)
        (project / "Media" / "clip.mov").write_text("proj")

        # Simular: V:\OtroCliente\Footage (fuera del proyecto)
        (drive / "OtroCliente" / "Footage").mkdir(parents=True)
        (drive / "OtroCliente" / "Footage" / "external.mov").write_text("ext")

        # FileIndex con root = proyecto
        idx = FileIndex(project)
        check("clip.mov indexado", len(idx._by_name.get("clip.mov", [])), 1)
        check("external.mov NO indexado aun",
              len(idx._by_name.get("external.mov", [])), 0)

        # add_roots con el "drive" completo (contiene self._root)
        idx.add_roots([drive], log)

        # external.mov ahora SI esta
        check("external.mov indexado", len(idx._by_name.get("external.mov", [])), 1)
        # clip.mov NO debe estar duplicado (el subarbol proyecto se excluyo)
        check("clip.mov no duplicado", len(idx._by_name.get("clip.mov", [])), 1)


def test_file_index_exclude_folder():
    """exclude_folder filtra archivos del destino de empaquetado."""
    print("\n=== Test: FileIndex exclude_folder ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Archivo en carpeta de destino (debe excluirse)
        (root / "Empaquetado" / "Media").mkdir(parents=True)
        (root / "Empaquetado" / "Media" / "clip.mov").write_text("copia")
        # Archivo en carpeta de origen
        (root / "Original" / "Media").mkdir(parents=True)
        (root / "Original" / "Media" / "clip.mov").write_text("original")

        idx = FileIndex(root, exclude_folder="Empaquetado")
        src = Path("D:/Dropbox/Media/clip.mov")
        result = idx.resolve(src, log)
        check_not_none("resuelve", result)
        if result:
            check_true("no resuelve al empaquetado",
                        "Empaquetado" not in str(result))
            check_true("resuelve al original",
                        "Original" in str(result))


# =====================================================================
# 5. AFTER EFFECTS
# =====================================================================

def test_is_plausible_path():
    print("\n=== Test: _is_plausible_path ===")
    check("ruta normal", _is_plausible_path("D:/Media/Footage/clip.mov"), True)
    check("ruta profunda ok", _is_plausible_path(
        "D:/A/B/C/D/E/F/G/H/I/J/clip.mov"), True)
    # Mas de 15 niveles -> falso positivo
    deep = "/".join(["a"] * 20) + "/clip.mov"
    check("ruta demasiado profunda", _is_plausible_path("D:/" + deep), False)
    # Nombre muy corto
    check("stem muy corto", _is_plausible_path("D:/a.mov"), False)
    # Caracteres binarios
    check("ruido binario", _is_plausible_path(
        "D:/Media/\x80\x81\x82/clip.mov"), False)


def test_scan_aep_for_footage():
    print("\n=== Test: _scan_aep_for_footage ===")
    with tempfile.TemporaryDirectory() as tmp:
        aep = Path(tmp) / "test.aepx"
        # Simular contenido de .aepx con rutas embebidas
        content = b"""<?xml version="1.0"?>
<AfterEffectsProject>
    <Footage>
        <item path="D:/Media/Footage/scene01.mov" />
        <item path="D:/Media/Audio/narration.wav" />
        <item path="/Volumes/SSD/Textures/background.png" />
    </Footage>
</AfterEffectsProject>
D:/Media/Footage/scene01.mov
D:/Media/Audio/narration.wav
/Volumes/SSD/Textures/background.png
"""
        aep.write_bytes(content)
        paths = _scan_aep_for_footage(aep, log)
        check_true("encuentra scene01.mov",
                    any("scene01.mov" in p for p in paths))
        check_true("encuentra narration.wav",
                    any("narration.wav" in p for p in paths))
        check_true("encuentra background.png",
                    any("background.png" in p for p in paths))


def test_scan_aep_gzipped():
    """Escaneo de .aepx comprimido con gzip."""
    print("\n=== Test: _scan_aep_for_footage gzipped ===")
    with tempfile.TemporaryDirectory() as tmp:
        aepx = Path(tmp) / "comp.aepx"
        content = b"Some binary data D:/Footage/shot.mov more data"
        with gzip.open(aepx, "wb") as f:
            f.write(content)
        paths = _scan_aep_for_footage(aepx, log)
        check_true("encuentra shot.mov en gzip",
                    any("shot.mov" in p for p in paths))


def test_scan_aep_empty():
    """Archivo AE vacio no rompe."""
    print("\n=== Test: _scan_aep_for_footage vacio ===")
    with tempfile.TemporaryDirectory() as tmp:
        aep = Path(tmp) / "empty.aep"
        aep.write_bytes(b"RIFX\x00\x00\x00\x00")
        paths = _scan_aep_for_footage(aep, log)
        check("sin rutas en archivo vacio", len(paths), 0)


def test_scan_aep_nonexistent():
    """Archivo AE que no existe no rompe."""
    print("\n=== Test: _scan_aep_for_footage no existe ===")
    paths = _scan_aep_for_footage(Path("Z:/no/existe.aep"), log)
    check("retorna set vacio", len(paths), 0)


def test_expand_ae_dependencies():
    """_expand_ae_dependencies escanea AE projects en la lista de medios."""
    print("\n=== Test: _expand_ae_dependencies ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Crear un .aep que referencia footage
        aep = root / "comp.aep"
        content = f"binary data {root / 'footage.mov'} more data"
        aep.write_bytes(content.encode("utf-8"))
        (root / "footage.mov").write_text("x")

        # target_paths incluye el .aep
        target = {str(aep)}
        deps = _expand_ae_dependencies(target, [], log)
        # El footage.mov deberia ser encontrado como dependencia
        check_true("encuentra dependencia de AE",
                    any("footage.mov" in d for d in deps))


# =====================================================================
# 6. READ/WRITE PRPROJ
# =====================================================================

def test_read_write_prproj():
    print("\n=== Test: read/write prproj ===")
    with tempfile.TemporaryDirectory() as tmp:
        xml_str = build_simple_prproj(["D:/test.mov"])
        path = write_test_prproj(Path(tmp), xml_str)

        # Leer
        xml_bytes = read_prproj(path)
        check_true("lee XML valido", b"PremiereData" in xml_bytes)

        # Escribir y releer
        out = Path(tmp) / "output.prproj"
        write_prproj(out, xml_bytes)
        xml_bytes2 = read_prproj(out)
        check("roundtrip preserva contenido", xml_bytes, xml_bytes2)


def test_read_prproj_uncompressed():
    """Lee .prproj sin comprimir (raro pero posible)."""
    print("\n=== Test: read_prproj sin comprimir ===")
    with tempfile.TemporaryDirectory() as tmp:
        xml_str = build_simple_prproj(["D:/test.mov"])
        path = Path(tmp) / "plain.prproj"
        path.write_bytes(xml_str.encode("utf-8"))
        xml_bytes = read_prproj(path)
        check_true("lee XML sin comprimir", b"PremiereData" in xml_bytes)


# =====================================================================
# 7. LIST_SEQUENCES
# =====================================================================

def test_list_sequences():
    print("\n=== Test: list_sequences ===")
    with tempfile.TemporaryDirectory() as tmp:
        xml_str = build_simple_prproj(
            ["D:/a.mov", "D:/b.mov", "D:/c.mov", "D:/d.mov"],
            seq_name="Final Edit",
        )
        path = write_test_prproj(Path(tmp), xml_str)
        ranked = list_sequences(path)
        check_true("al menos 1 secuencia", len(ranked) >= 1)
        check("primera es Final Edit", ranked[0][0]["name"], "Final Edit")


def test_list_sequences_empty():
    """Proyecto sin secuencias."""
    print("\n=== Test: list_sequences vacio ===")
    with tempfile.TemporaryDirectory() as tmp:
        xml_str = build_prproj_xml([])
        path = write_test_prproj(Path(tmp), xml_str)
        ranked = list_sequences(path)
        check("sin secuencias", len(ranked), 0)


# =====================================================================
# 8. SELECT SEQUENCE
# =====================================================================

def test_select_sequence_auto():
    print("\n=== Test: select_sequence_auto ===")
    ranked = [
        ({"name": "Final", "uid": "1"}, 0.9),
        ({"name": "Draft", "uid": "2"}, 0.5),
    ]
    result = select_sequence_auto(ranked, log)
    check("elige la primera", result["name"], "Final")


def test_select_sequence_auto_empty():
    print("\n=== Test: select_sequence_auto vacio ===")
    result = select_sequence_auto([], log)
    check_none("retorna None", result)


def test_select_sequence_by_pattern():
    print("\n=== Test: select_sequence_by_pattern ===")
    ranked = [
        ({"name": "Final Master", "uid": "1"}, 0.9),
        ({"name": "Instagram Reel", "uid": "2"}, 0.5),
        ({"name": "BTS Edit", "uid": "3"}, 0.3),
    ]
    result = select_sequence_by_pattern(ranked, "*master*", log)
    check("encuentra por patron", result["name"], "Final Master")

    result = select_sequence_by_pattern(ranked, "*reel*", log)
    check("encuentra reel", result["name"], "Instagram Reel")

    result = select_sequence_by_pattern(ranked, "*noexiste*", log)
    check_none("patron sin match", result)


# =====================================================================
# 9. PACKAGE_PROJECT - flujo completo
# =====================================================================

def test_package_project_dry_run():
    """Empaquetado completo en modo dry-run con archivos reales."""
    print("\n=== Test: package_project dry-run ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        # Crear archivos de media reales
        (project / "Media").mkdir()
        media1 = project / "Media" / "video.mov"
        media2 = project / "Media" / "audio.wav"
        media1.write_text("video content")
        media2.write_text("audio content")

        # Crear .prproj que referencia esos archivos
        xml_str = build_simple_prproj(
            [str(media1), str(media2)],
            seq_name="Final Edit",
        )
        prproj = write_test_prproj(project, xml_str, "test.prproj")

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="test",
            dry_run=True,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
            extra_search_roots=None,
        )

        check("2 archivos copiaria", stats["copied"], 2)
        check("0 missing", stats["missing"], 0)
        check("0 errores", len(stats["errors"]), 0)
        # En dry-run no se crean archivos
        check_true("no creo carpeta destino", not (dest / "test").exists())


def test_package_project_real_copy():
    """Empaquetado completo con copia real de archivos."""
    print("\n=== Test: package_project copia real ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media1 = project / "Media" / "video.mov"
        media1.write_text("video content")

        xml_str = build_simple_prproj(
            [str(media1)],
            seq_name="Edit Final",
        )
        prproj = write_test_prproj(project, xml_str, "real.prproj")

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="real",
            dry_run=False,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
            extra_search_roots=None,
        )

        check("1 archivo copiado", stats["copied"], 1)
        # Verificar que se creo el .prproj de salida
        output_prproj = dest / "real" / "real.prproj"
        check_true("prproj de salida existe", output_prproj.exists())

        # Verificar que el .prproj de salida es valido
        if output_prproj.exists():
            xml_bytes = read_prproj(output_prproj)
            check_true("prproj de salida es XML valido",
                        b"PremiereData" in xml_bytes)


def test_package_project_offline_resolution():
    """Archivos offline se resuelven via FileIndex."""
    print("\n=== Test: package_project resolucion offline ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        # El .prproj referencia un archivo en una ruta que NO existe
        fake_path = "D:\\Dropbox\\Cliente\\Footage\\clip.mov"

        # Pero el archivo SI existe en el arbol de dest (la raiz de busqueda)
        (dest / "Footage").mkdir()
        real_file = dest / "Footage" / "clip.mov"
        real_file.write_text("real media")

        xml_str = build_simple_prproj([fake_path], seq_name="Main")
        prproj = write_test_prproj(project, xml_str, "offline.prproj")

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="offline",
            dry_run=False,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
            extra_search_roots=None,
        )

        check("1 archivo copiado (resuelto)", stats["copied"], 1)
        check("0 missing", stats["missing"], 0)


def test_package_project_with_extra_search_roots():
    """Archivos offline se resuelven usando search roots adicionales."""
    print("\n=== Test: package_project con extra search roots ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()
        extra = root / "NAS"
        extra.mkdir()

        # Archivo solo existe en el NAS (extra search root)
        (extra / "Footage" / "Camara1").mkdir(parents=True)
        real_file = extra / "Footage" / "Camara1" / "scene.mov"
        real_file.write_text("nas media")

        # .prproj referencia ruta que no existe
        fake_path = "D:\\Editor\\Proyecto\\Footage\\Camara1\\scene.mov"
        xml_str = build_simple_prproj([fake_path], seq_name="Main")
        prproj = write_test_prproj(project, xml_str, "nas.prproj")

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="nas",
            dry_run=False,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
            extra_search_roots=[extra],
        )

        check("1 archivo copiado desde NAS", stats["copied"], 1)
        check("0 missing", stats["missing"], 0)


def test_package_project_mac_path_translation():
    """Rutas Mac se traducen y luego se resuelven offline."""
    print("\n=== Test: package_project traduccion Mac ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        # Crear el archivo en una ruta Windows
        (dest / "Media").mkdir()
        real_file = dest / "Media" / "clip.mov"
        real_file.write_text("media")

        # .prproj usa ruta Mac
        mac_path = "/Volumes/SEGUIMIENTOS/Proyecto/Media/clip.mov"
        xml_str = build_simple_prproj([mac_path], seq_name="Main")
        prproj = write_test_prproj(project, xml_str, "mac.prproj")

        mappings = parse_path_mappings(["/Volumes/SEGUIMIENTOS=V:"])

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="mac",
            dry_run=False,
            mode="auto",
            sequence_pattern=None,
            path_mappings=mappings,
            log=log,
            extra_search_roots=None,
        )

        # La ruta traducida (V:\Proyecto\Media\clip.mov) no existe en disco,
        # pero FileIndex deberia resolver "clip.mov" al archivo en dest/Media/
        check("1 archivo copiado con traduccion", stats["copied"], 1)
        check("0 missing", stats["missing"], 0)


def test_package_project_mode_all():
    """Modo --all copia todos los medios sin filtrar por secuencia."""
    print("\n=== Test: package_project modo --all ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media1 = project / "Media" / "a.mov"
        media2 = project / "Media" / "b.wav"
        media1.write_text("a")
        media2.write_text("b")

        xml_str = build_simple_prproj(
            [str(media1), str(media2)],
            seq_name="Main",
        )
        prproj = write_test_prproj(project, xml_str, "all.prproj")

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="all",
            dry_run=True,
            mode="all",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
        )

        check("2 archivos en modo all", stats["copied"], 2)


def test_package_project_no_sequences_fallback():
    """Proyecto sin secuencias usa fallback: copiar todo."""
    print("\n=== Test: package_project sin secuencias (fallback) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media = project / "Media" / "clip.mov"
        media.write_text("x")

        # XML sin secuencias pero con un Media
        _NEXT_ID[0] = 500
        mid, mx = _make_media(str(media))
        xml_str = build_prproj_xml([mx])
        prproj = write_test_prproj(project, xml_str, "noseq.prproj")

        stats = package_project(
            prproj_path=prproj,
            dest_root=dest,
            folder_name="noseq",
            dry_run=True,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
        )

        check("fallback copia todos los medios", stats["copied"], 1)


def test_package_project_corrupt_prproj():
    """Archivo .prproj corrupto se maneja sin crash."""
    print("\n=== Test: package_project corrupto ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dest = root / "Backup"
        dest.mkdir()

        corrupt = root / "corrupt.prproj"
        corrupt.write_bytes(b"esto no es XML ni gzip")

        stats = package_project(
            prproj_path=corrupt,
            dest_root=dest,
            folder_name="corrupt",
            dry_run=False,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
        )

        check_true("reporta error", len(stats["errors"]) > 0)


def test_package_project_invalid_xml():
    """Archivo .prproj con gzip valido pero XML invalido."""
    print("\n=== Test: package_project XML invalido ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dest = root / "Backup"
        dest.mkdir()

        bad = root / "bad.prproj"
        with gzip.open(bad, "wb") as f:
            f.write(b"<notclosed>")

        stats = package_project(
            prproj_path=bad,
            dest_root=dest,
            folder_name="bad",
            dry_run=False,
            mode="auto",
            sequence_pattern=None,
            path_mappings=[],
            log=log,
        )

        check_true("reporta error XML", len(stats["errors"]) > 0)


def test_package_project_skip_existing():
    """Archivos ya copiados se omiten (skipped)."""
    print("\n=== Test: package_project omite existentes ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media = project / "Media" / "video.mov"
        media.write_text("original")

        xml_str = build_simple_prproj([str(media)], seq_name="Main")
        prproj = write_test_prproj(project, xml_str, "dup.prproj")

        # Primera copia
        stats1 = package_project(
            prproj_path=prproj, dest_root=dest, folder_name="dup",
            dry_run=False, mode="auto", sequence_pattern=None,
            path_mappings=[], log=log,
        )
        check("primera copia: 1 copiado", stats1["copied"], 1)

        # Segunda copia: debe omitir
        stats2 = package_project(
            prproj_path=prproj, dest_root=dest, folder_name="dup",
            dry_run=False, mode="auto", sequence_pattern=None,
            path_mappings=[], log=log,
        )
        check("segunda copia: 0 copiados", stats2["copied"], 0)
        check("segunda copia: 1 omitido", stats2["skipped"], 1)


def test_package_project_relative_paths_in_output():
    """El .prproj de salida usa rutas relativas."""
    print("\n=== Test: package_project rutas relativas ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media = project / "Media" / "clip.mov"
        media.write_text("x")

        xml_str = build_simple_prproj([str(media)], seq_name="Main")
        prproj = write_test_prproj(project, xml_str, "rel.prproj")

        package_project(
            prproj_path=prproj, dest_root=dest, folder_name="rel",
            dry_run=False, mode="auto", sequence_pattern=None,
            path_mappings=[], log=log,
        )

        output_prproj = dest / "rel" / "rel.prproj"
        if output_prproj.exists():
            xml_bytes = read_prproj(output_prproj)
            xml_text = xml_bytes.decode("utf-8")
            check_true("contiene ruta relativa ./",
                        "./" in xml_text)
            check_true("no contiene ruta absoluta original",
                        str(media).replace("\\", "/") not in xml_text)
        else:
            check("prproj de salida existe", False, True)


def test_package_project_trim_xml():
    """El .prproj de salida tiene menos objetos que el original (trim funciona)."""
    print("\n=== Test: package_project trim XML ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media = project / "Media" / "clip.mov"
        media.write_text("x")

        # Crear .prproj con secuencia principal + objetos extra no alcanzables
        xml_str = build_simple_prproj([str(media)], seq_name="Main")

        # Agregar objetos huerfanos que el trim deberia eliminar
        _NEXT_ID[0] = 800
        orphan_mid, orphan_mx = _make_media("D:/Orphan/unused.mov")
        orphan_sid, orphan_sx = _make_media_source(orphan_mid)
        orphan_cid, orphan_cx = _make_clip(orphan_sid)

        root_el = ET.fromstring(xml_str)
        for elem_str in [orphan_mx, orphan_sx, orphan_cx]:
            root_el.append(ET.fromstring(elem_str))
        count_before = len(list(root_el))

        # Escribir el XML inflado como .prproj
        modified_xml = ET.tostring(root_el, encoding="utf-8", xml_declaration=True)
        prproj = project / "trim.prproj"
        import gzip as _gz
        with _gz.open(prproj, "wb") as f:
            f.write(modified_xml)

        package_project(
            prproj_path=prproj, dest_root=dest, folder_name="trim",
            dry_run=False, mode="auto", sequence_pattern=None,
            path_mappings=[], log=log,
        )

        output_prproj = dest / "trim" / "trim.prproj"
        check_true("prproj de salida existe", output_prproj.exists())

        if output_prproj.exists():
            xml_bytes = read_prproj(output_prproj)
            out_root = ET.fromstring(xml_bytes)
            count_after = len(list(out_root))
            check_true(
                f"trim reduce objetos ({count_before} -> {count_after})",
                count_after < count_before,
            )


def test_package_project_trim_dry_run_reports():
    """En dry-run, el trim reporta cuantos objetos eliminaria."""
    print("\n=== Test: package_project trim dry-run ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        (project / "Media").mkdir()
        media = project / "Media" / "clip.mov"
        media.write_text("x")

        # Crear .prproj con objetos huerfanos
        xml_str = build_simple_prproj([str(media)], seq_name="Main")
        _NEXT_ID[0] = 850
        orphan_mid, orphan_mx = _make_media("D:/Orphan/extra.mov")

        root_el = ET.fromstring(xml_str)
        root_el.append(ET.fromstring(orphan_mx))
        modified_xml = ET.tostring(root_el, encoding="utf-8", xml_declaration=True)
        prproj = project / "drytrim.prproj"
        import gzip as _gz
        with _gz.open(prproj, "wb") as f:
            f.write(modified_xml)

        # Capturar log para verificar que reporta eliminacion
        import io
        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.INFO)
        test_log = logging.getLogger("trim_dry_test")
        test_log.addHandler(handler)
        test_log.setLevel(logging.INFO)

        package_project(
            prproj_path=prproj, dest_root=dest, folder_name="drytrim",
            dry_run=True, mode="auto", sequence_pattern=None,
            path_mappings=[], log=test_log,
        )

        output = handler.stream.getvalue()
        check_true("dry-run reporta eliminaria objetos",
                    "Eliminaria" in output and "objetos" in output)

        test_log.removeHandler(handler)


def test_package_project_empty_media():
    """Proyecto con secuencia sin medios (vacia) se maneja correctamente."""
    print("\n=== Test: package_project secuencia vacia ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = root / "Proyecto"
        project.mkdir()
        dest = root / "Backup"
        dest.mkdir()

        # Secuencia sin clips
        _NEXT_ID[0] = 700
        trid, trx = _make_track([])
        gid, gx = _make_track_group([trid])
        _sid, _suid, sx = _make_sequence("Empty", [("v", gid)])
        xml_str = build_prproj_xml([trx, gx, sx])
        prproj = write_test_prproj(project, xml_str, "empty.prproj")

        stats = package_project(
            prproj_path=prproj, dest_root=dest, folder_name="empty",
            dry_run=False, mode="auto", sequence_pattern=None,
            path_mappings=[], log=log,
        )

        check("0 copiados", stats["copied"], 0)
        check("0 missing", stats["missing"], 0)
        check("prproj se guarda igual",
              (dest / "empty" / "empty.prproj").exists(), True)


# =====================================================================
# MAIN
# =====================================================================

if __name__ == "__main__":
    # 1. Utilidades de rutas
    test_is_absolute_path()
    test_normalize_media_path()
    test_parse_path_mappings()
    test_translate_path()
    test_media_dest_path()
    test_clean_folder_name()
    test_unique_folder_name()
    test_is_auto_nested_name()
    test_fmt_size()

    # 2. PrprojGraph
    test_prproj_graph_basic()
    test_prproj_graph_media_collection()
    test_prproj_graph_nested_sequence()
    test_prproj_graph_filters_synthetic()
    test_prproj_graph_filters_proxy()
    test_prproj_graph_nesting_graph()
    test_prproj_graph_sequence_info()
    test_prproj_graph_empty()
    test_prproj_graph_trim()
    test_prproj_graph_find_all_media_paths()

    # 3. Ranking
    test_score_sequence_root_with_children()
    test_score_sequence_promote_demote()
    test_score_sequence_clip_density()

    # 4. FileIndex offline resolution
    test_file_index_extra_search_roots()
    test_file_index_extra_root_does_not_override()
    test_file_index_extra_root_nonexistent()
    test_file_index_suffix_score_deep()
    test_file_index_resolve_prefers_deeper_match()
    test_file_index_with_path_translation()
    test_file_index_unicode_nfd_from_mac()
    test_file_index_mixed_extra_roots_disambiguation()
    test_file_index_parent_root_no_duplicate()
    test_file_index_exclude_folder()

    # 5. After Effects
    test_is_plausible_path()
    test_scan_aep_for_footage()
    test_scan_aep_gzipped()
    test_scan_aep_empty()
    test_scan_aep_nonexistent()
    test_expand_ae_dependencies()

    # 6. Read/Write
    test_read_write_prproj()
    test_read_prproj_uncompressed()

    # 7. list_sequences
    test_list_sequences()
    test_list_sequences_empty()

    # 8. Seleccion de secuencia
    test_select_sequence_auto()
    test_select_sequence_auto_empty()
    test_select_sequence_by_pattern()

    # 9. Package project (flujo completo)
    test_package_project_dry_run()
    test_package_project_real_copy()
    test_package_project_offline_resolution()
    test_package_project_with_extra_search_roots()
    test_package_project_mac_path_translation()
    test_package_project_mode_all()
    test_package_project_no_sequences_fallback()
    test_package_project_corrupt_prproj()
    test_package_project_invalid_xml()
    test_package_project_skip_existing()
    test_package_project_relative_paths_in_output()
    test_package_project_trim_xml()
    test_package_project_trim_dry_run_reports()
    test_package_project_empty_media()

    print(f"\n{'=' * 60}")
    print(f"  TOTAL: {PASSED + FAILED} | PASSED: {PASSED} | FAILED: {FAILED}")
    print(f"{'=' * 60}")

    if FAILED:
        exit(1)
