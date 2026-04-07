#!/usr/bin/env python3
"""Tests para FileIndex: resolucion de archivos offline con deteccion de ambiguedad."""

import logging
import os
import tempfile
import unicodedata
from pathlib import Path

# Importar desde el modulo principal
from empaquetar_premiere import FileIndex, AE_PROJECT_EXTENSIONS, _AE_SKIP_DIRS

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
        print(f"         esperado: {expected}")
        print(f"         obtenido: {got}")


def test_single_candidate():
    """Un solo archivo con ese nombre -> se resuelve directamente."""
    print("\n=== Test: candidato unico ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Media").mkdir()
        (root / "Media" / "video.mov").write_text("x")

        idx = FileIndex(root)

        # Ruta offline de Dropbox que no existe
        src = Path("C:/Users/editor/Dropbox/Proyecto/Media/video.mov")
        result = idx.resolve(src, log)
        check("resuelve al unico candidato", result, root / "Media" / "video.mov")


def test_no_candidates():
    """Archivo no existe en el proyecto -> None."""
    print("\n=== Test: sin candidatos ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Media").mkdir()
        (root / "Media" / "otro.mov").write_text("x")

        idx = FileIndex(root)
        src = Path("D:/algo/noexiste.mov")
        result = idx.resolve(src, log)
        check("retorna None", result, None)


def test_ambiguity_same_score():
    """Dos archivos con mismo nombre y mismo score -> ambiguo, no resolver."""
    print("\n=== Test: ambiguedad (mismo score) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "CarpetaA").mkdir()
        (root / "CarpetaB").mkdir()
        (root / "CarpetaA" / "clip.mp4").write_text("a")
        (root / "CarpetaB" / "clip.mp4").write_text("b")

        idx = FileIndex(root)

        # Ruta original no tiene carpetas que coincidan con ninguno
        src = Path("X:/Dropbox/OtraCosa/clip.mp4")
        result = idx.resolve(src, log)
        check("ambiguo retorna None", result, None)


def test_ambiguity_resolved_by_score():
    """Dos archivos con mismo nombre pero uno tiene mejor score -> se resuelve."""
    print("\n=== Test: ambiguedad resuelta por score ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Footage" / "Camara1").mkdir(parents=True)
        (root / "Archive").mkdir()
        (root / "Footage" / "Camara1" / "clip.mp4").write_text("a")
        (root / "Archive" / "clip.mp4").write_text("b")

        idx = FileIndex(root)

        # La ruta original tiene "Camara1" que coincide con un candidato
        src = Path("D:/Editor/Proyecto/Footage/Camara1/clip.mp4")
        result = idx.resolve(src, log)
        check(
            "resuelve al de mejor score",
            result,
            root / "Footage" / "Camara1" / "clip.mp4",
        )


def test_short_path():
    """Ruta con < 3 componentes (antes fallaba, ahora funciona)."""
    print("\n=== Test: ruta corta (L3) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "video.mov").write_text("x")

        idx = FileIndex(root)

        # Ruta cortisima: solo drive + archivo
        src = Path("D:/video.mov")
        result = idx.resolve(src, log)
        check("ruta corta se resuelve", result, root / "video.mov")


def test_restructured_folders():
    """Archivo existe pero en otra estructura de carpetas (L2)."""
    print("\n=== Test: carpetas reorganizadas (L2) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # El archivo esta en NuevaEstructura/ pero el original decia ViejaEstructura/
        (root / "NuevaEstructura").mkdir()
        (root / "NuevaEstructura" / "entrevista.wav").write_text("x")

        idx = FileIndex(root)

        src = Path("V:/Proyecto/ViejaEstructura/SubCarpeta/entrevista.wav")
        result = idx.resolve(src, log)
        check(
            "encuentra aunque cambio la estructura",
            result,
            root / "NuevaEstructura" / "entrevista.wav",
        )


def test_case_insensitive():
    """Busqueda case-insensitive (Windows)."""
    print("\n=== Test: case insensitive ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Media").mkdir()
        target = root / "Media" / "Interview.MOV"
        target.write_text("x")

        idx = FileIndex(root)

        src = Path("D:/proyecto/media/interview.mov")
        result = idx.resolve(src, log)
        # En Windows el nombre real puede ser Interview.MOV
        check("case insensitive match", result is not None, True)
        if result:
            check("apunta al archivo correcto", result.name, "Interview.MOV")


def test_unicode_nfc():
    """Normalización Unicode NFC (Mac usa NFD para acentos)."""
    print("\n=== Test: unicode NFC ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Crear archivo con nombre en NFC
        nfc_name = unicodedata.normalize("NFC", "música.wav")
        (root / nfc_name).write_text("x")

        idx = FileIndex(root)

        # Buscar con NFD (como lo almacena Mac)
        nfd_name = unicodedata.normalize("NFD", "música.wav")
        src = Path(f"D:/Proyecto/{nfd_name}")
        result = idx.resolve(src, log)
        check("NFC/NFD match", result is not None, True)


def test_ae_projects_collected():
    """FileIndex recolecta .aep/.aepx como subproducto."""
    print("\n=== Test: recoleccion de AE projects ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "AE").mkdir()
        (root / "AE" / "comp.aep").write_text("x")
        (root / "AE" / "otro.aepx").write_text("x")
        (root / "Media" ).mkdir()
        (root / "Media" / "video.mov").write_text("x")

        idx = FileIndex(root)
        check("encuentra 2 AE projects", len(idx.ae_projects), 2)
        ae_names = {Path(p).name for p in idx.ae_projects}
        check("incluye comp.aep", "comp.aep" in ae_names, True)
        check("incluye otro.aepx", "otro.aepx" in ae_names, True)


def test_skip_dirs():
    """FileIndex respeta skip_dirs y exclude_folder."""
    print("\n=== Test: skip dirs ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Media").mkdir()
        (root / "Media" / "video.mov").write_text("x")
        # Archivo en carpeta de auto-save (debe ignorarse)
        (root / "Adobe After Effects Auto-Save").mkdir()
        (root / "Adobe After Effects Auto-Save" / "backup.aep").write_text("x")
        # Archivo en carpeta excluida
        (root / "Output").mkdir()
        (root / "Output" / "video.mov").write_text("x")

        idx = FileIndex(root, _AE_SKIP_DIRS, exclude_folder="Output")

        # El backup.aep no debe estar en ae_projects
        ae_names = {Path(p).name for p in idx.ae_projects}
        check("auto-save excluido de AE", "backup.aep" not in ae_names, True)

        # video.mov en Output no debe estar en el indice
        key = "video.mov"
        candidates = idx._by_name.get(key, [])
        check(
            "exclude_folder no indexado",
            all("Output" not in str(c) for c in candidates),
            True,
        )
        check("solo 1 video.mov (Media)", len(candidates), 1)


def test_suffix_score():
    """Verificar _suffix_score directamente."""
    print("\n=== Test: _suffix_score ===")

    score = FileIndex._suffix_score(
        Path("D:/Dropbox/Proyecto/Media/Camara1/clip.mp4"),
        Path("E:/NAS/Proyecto/Media/Camara1/clip.mp4"),
    )
    check("3 carpetas coinciden (Camara1, Media, Proyecto)", score, 3)

    score = FileIndex._suffix_score(
        Path("D:/Dropbox/Otro/clip.mp4"),
        Path("E:/NAS/Proyecto/Media/clip.mp4"),
    )
    check("0 carpetas coinciden", score, 0)

    score = FileIndex._suffix_score(
        Path("D:/Media/clip.mp4"),
        Path("E:/NAS/Proyecto/Media/clip.mp4"),
    )
    check("1 carpeta coincide (Media)", score, 1)


def test_three_candidates_best_wins():
    """Tres candidatos, uno con mejor score gana."""
    print("\n=== Test: 3 candidatos, mejor score gana ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "A").mkdir()
        (root / "B" / "Footage").mkdir(parents=True)
        (root / "C" / "Footage" / "Day1").mkdir(parents=True)
        (root / "A" / "shot.mp4").write_text("1")
        (root / "B" / "Footage" / "shot.mp4").write_text("2")
        (root / "C" / "Footage" / "Day1" / "shot.mp4").write_text("3")

        idx = FileIndex(root)
        src = Path("X:/Editor/Project/Footage/Day1/shot.mp4")
        result = idx.resolve(src, log)
        check(
            "elige el de mas coincidencias (Day1/Footage)",
            result,
            root / "C" / "Footage" / "Day1" / "shot.mp4",
        )


def test_three_candidates_tie():
    """Tres candidatos, dos empatan en score -> ambiguo."""
    print("\n=== Test: 3 candidatos, empate -> ambiguo ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "A" / "Media").mkdir(parents=True)
        (root / "B" / "Media").mkdir(parents=True)
        (root / "C").mkdir()
        (root / "A" / "Media" / "clip.mp4").write_text("1")
        (root / "B" / "Media" / "clip.mp4").write_text("2")
        (root / "C" / "clip.mp4").write_text("3")

        idx = FileIndex(root)
        # "Media" coincide con A/Media y B/Media (empate score=1), C tiene score=0
        src = Path("X:/Dropbox/Media/clip.mp4")
        result = idx.resolve(src, log)
        check("empate en top 2 -> ambiguo", result, None)


def test_empty_project():
    """Proyecto vacio -> nada que resolver."""
    print("\n=== Test: proyecto vacio ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        idx = FileIndex(root)
        check("indice vacio", len(idx._by_name), 0)
        check("sin AE projects", len(idx.ae_projects), 0)
        result = idx.resolve(Path("D:/algo.mov"), log)
        check("resolve retorna None", result, None)


# ── Ejecutar todos los tests ──

if __name__ == "__main__":
    test_single_candidate()
    test_no_candidates()
    test_ambiguity_same_score()
    test_ambiguity_resolved_by_score()
    test_short_path()
    test_restructured_folders()
    test_case_insensitive()
    test_unicode_nfc()
    test_ae_projects_collected()
    test_skip_dirs()
    test_suffix_score()
    test_three_candidates_best_wins()
    test_three_candidates_tie()
    test_empty_project()

    print(f"\n{'=' * 50}")
    print(f"  TOTAL: {PASSED + FAILED} | PASSED: {PASSED} | FAILED: {FAILED}")
    print(f"{'=' * 50}")

    if FAILED:
        exit(1)
