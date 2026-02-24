#!/usr/bin/env python3
"""Diagnostico: muestra la estructura XML real de un .prproj."""

import gzip
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def read_prproj(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def dump_tree(elem, depth=0, max_depth=4):
    """Muestra arbol XML hasta max_depth niveles."""
    if depth > max_depth:
        return
    indent = "  " * depth
    attrs = " ".join(f'{k}="{v}"' for k, v in elem.attrib.items())
    text = ""
    if elem.text and elem.text.strip():
        t = elem.text.strip()
        if len(t) > 80:
            t = t[:80] + "..."
        text = f" => {t}"
    print(f"{indent}<{elem.tag} {attrs}>{text}")
    for child in elem:
        dump_tree(child, depth + 1, max_depth)


def main():
    if len(sys.argv) < 2:
        print("Uso: python diagnostico.py <archivo.prproj>")
        sys.exit(1)

    prproj = Path(sys.argv[1])
    xml_bytes = read_prproj(prproj)
    root = ET.fromstring(xml_bytes)

    # --- 1. Tags de primer nivel: que tipos de objetos hay ---
    print("=" * 70)
    print("1. TIPOS DE OBJETOS (hijos directos de PremiereData)")
    print("=" * 70)
    tag_counts = {}
    for child in root:
        tag_counts[child.tag] = tag_counts.get(child.tag, 0) + 1
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        print(f"  {tag}: {count}")

    # --- 2. Primera secuencia: estructura completa ---
    print()
    print("=" * 70)
    print("2. ESTRUCTURA DE LA PRIMERA SECUENCIA (4 niveles)")
    print("=" * 70)
    sequences = [el for el in root if el.tag == "Sequence"]
    if not sequences:
        print("  No se encontraron <Sequence>!")
        # Buscar en todo el arbol
        print("  Buscando 'Sequence' en todo el XML...")
        for elem in root.iter():
            if "sequence" in elem.tag.lower() or "Sequence" in elem.tag:
                print(f"  Encontrado: <{elem.tag}> con attrs: {elem.attrib}")
    else:
        # Buscar una secuencia con nombre real (no "Secuencia anidada")
        target = sequences[0]
        for s in sequences:
            name_el = s.find("Name")
            if name_el is not None and name_el.text:
                name = name_el.text.strip()
                if "anidada" not in name.lower() and "nested" not in name.lower():
                    target = s
                    break

        name_el = target.find("Name")
        name = name_el.text.strip() if name_el is not None and name_el.text else "?"
        print(f"  Secuencia elegida: '{name}'")
        print()
        dump_tree(target, max_depth=5)

    # --- 3. Un track de video si existe ---
    print()
    print("=" * 70)
    print("3. PRIMER VideoClipTrack / VideoTrack (3 niveles)")
    print("=" * 70)
    for tag in ["VideoClipTrack", "VideoTrack", "Track"]:
        found = [el for el in root if el.tag == tag]
        if found:
            print(f"  Encontrados {len(found)} <{tag}>")
            dump_tree(found[0], max_depth=3)
            break
    else:
        print("  No encontrado. Tags con 'Track':")
        for el in root:
            if "track" in el.tag.lower():
                print(f"    <{el.tag}> {el.attrib}")

    # --- 4. Un clip / media source ---
    print()
    print("=" * 70)
    print("4. PRIMER ELEMENTO CON ActualMediaFilePath")
    print("=" * 70)
    found_media = False
    for elem in root.iter():
        if elem.tag == "ActualMediaFilePath" and elem.text and elem.text.strip():
            print(f"  Valor: {elem.text.strip()[:100]}")
            # Mostrar el padre y abuelo
            # Necesitamos buscar el parent
            break
    else:
        print("  No encontrado <ActualMediaFilePath>")
        print("  Buscando tags con 'Media' o 'File' o 'Path'...")
        media_tags = set()
        for elem in root.iter():
            if any(k in elem.tag for k in ["Media", "File", "Path"]):
                if elem.text and elem.text.strip() and len(elem.text.strip()) > 5:
                    media_tags.add(elem.tag)
        for t in sorted(media_tags):
            print(f"    <{t}>")

    # Encontrar el elemento Media que contiene ActualMediaFilePath
    print()
    print("  Primer <Media> o similar con ruta:")
    for el in root:
        if "Media" in el.tag and el.tag != "MediaFilePath":
            has_path = False
            for child in el.iter():
                if child.tag in ("ActualMediaFilePath", "FilePath", "FileKey"):
                    if child.text and child.text.strip():
                        has_path = True
                        break
            if has_path:
                dump_tree(el, max_depth=3)
                break

    # --- 5. Buscar como estan conectados clips y secuencias ---
    print()
    print("=" * 70)
    print("5. ELEMENTOS QUE REFERENCIAN LA PRIMERA SECUENCIA")
    print("=" * 70)
    if sequences:
        seq = sequences[0]
        seq_oid = seq.get("ObjectID", "")
        seq_ouid = seq.get("ObjectUID", "")
        print(f"  Secuencia ObjectID={seq_oid} ObjectUID={seq_ouid}")
        ref_count = 0
        for elem in root.iter():
            ref = elem.get("ObjectRef", "")
            uref = elem.get("ObjectURef", "")
            if (seq_oid and ref == seq_oid) or (seq_ouid and uref == seq_ouid):
                # Encontrar el padre top-level
                print(f"  Referenciado por: <{elem.tag}> attrs={dict(elem.attrib)}")
                ref_count += 1
                if ref_count >= 5:
                    print("  ... (mostrando max 5)")
                    break


if __name__ == "__main__":
    main()
