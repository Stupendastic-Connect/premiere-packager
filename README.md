# premiere-packager

Empaqueta proyectos de Adobe Premiere Pro sin abrir Premiere. Equivalente al "Project Manager > Collect Files" pero desde la línea de comandos o una interfaz gráfica liviana.

Parsea el grafo XML del `.prproj`, identifica qué medios usa una secuencia específica y los copia a una carpeta destino. Incluye una copia limpia del proyecto con solo los objetos alcanzables.

---

## Características

- Selección de secuencia: interactiva, por patrón, automática o "todas"
- Traversal recursivo de secuencias anidadas
- Resolución de archivos offline: búsqueda por sufijo, insensible a mayúsculas, normalización Unicode NFC/NFD
- Traducción de rutas Mac→Windows (`/Volumes/DISCO` → `V:`)
- Raíces de búsqueda adicionales para archivos en Dropbox, NAS u otras ubicaciones
- Escaneo de dependencias de After Effects (`.aep` / `.aepx`, binario y XML)
- Filtros: descarta medios sintéticos (barras, tonos) y proxies
- Trimming del XML: elimina objetos inalcanzables del `.prproj` de salida
- Rutas relativas en el proyecto empaquetado
- Modo dry-run para previsualizar sin copiar nada
- Ranking de secuencias por topología (40%), densidad de clips (25%), complejidad de pistas (20%) y patrones de nombre (15%)

---

## Instalación

Requiere Python 3.10 o superior. Sin dependencias externas (solo stdlib).

```bash
git clone https://github.com/stupendastic/premiere-packager
cd premiere-packager
```

Listo. No hay nada que instalar.

---

## Uso por CLI

```bash
python empaquetar_premiere.py <origen> <destino> [opciones]
```

`<origen>` puede ser una carpeta con uno o más `.prproj` o directamente un archivo `.prproj`.

### Ejemplos

```bash
# Modo interactivo: muestra secuencias disponibles y pregunta cuál empaquetar
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup"

# Dry-run: muestra qué se copiaría sin copiar nada
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --dry-run

# Selección automática de la secuencia principal
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --auto

# Selección por patrón de nombre (acepta substrings, case-insensitive)
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --sequence "final"

# Empaquetar todas las secuencias
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --all

# Traducción de rutas Mac→Windows (se pueden repetir varios --map)
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" \
    --map "/Volumes/DISCO=V:" \
    --map "/Volumes/SSD=D:"

# Raíces adicionales para resolver archivos offline
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --search "V:/AEDAS"

# Incluir carpeta Adobe Premiere Pro Auto-Save
python empaquetar_premiere.py "D:/Proyectos" "E:/Backup" --include-autosave
```

### Referencia de opciones

| Opción | Descripción |
|--------|-------------|
| `--dry-run` | Muestra qué se haría sin modificar nada |
| `--auto` | Selecciona automáticamente la secuencia principal |
| `--sequence "texto"` | Selecciona secuencias cuyo nombre contenga el texto |
| `--all` | Empaqueta todas las secuencias del proyecto |
| `--map "src=dst"` | Reemplaza prefijo de ruta (Mac→Windows) |
| `--search <ruta>` | Agrega raíz de búsqueda para archivos offline |
| `--include-autosave` | Copia también la carpeta de auto-guardado |

---

## Uso por GUI

```bash
python gui.py
```

Flujo de trabajo:

1. Elegir carpeta de proyectos con el selector
2. Escanear: muestra los `.prproj` encontrados con checkboxes
3. Usar la barra de búsqueda para filtrar (Ctrl+F)
4. Configurar opciones y elegir carpeta destino
5. Empaquetar (Ctrl+Enter) o cancelar (Escape)

### Opciones disponibles en la GUI

- Dry Run
- Limitar cantidad de archivos copiados
- Selección automática de secuencia principal
- Omitir proyectos ya empaquetados
- Incluir Auto-Save
- Mappings Mac→Windows (campo de texto, uno por línea)
- Raíces de búsqueda adicionales (campo de texto, una por línea)

Al hacer click en "Empaquetar" se abre un diálogo para elegir la secuencia si el modo automático no está activo.

Atajo adicional: Ctrl+L limpia el log de salida.

---

## Cómo funciona (resumen)

El `.prproj` de Premiere es un XML comprimido con gzip que representa un grafo de objetos. Cada nodo tiene un `ObjectID` y puede referenciar a otros mediante `ObjectRef`, `ObjectUID` u `ObjectURef`.

El motor:

1. Descomprime y parsea el XML
2. Construye el grafo de objetos
3. Localiza la secuencia seleccionada
4. Hace traversal recursivo del grafo desde esa secuencia
5. Recolecta todos los `ActualMediaFilePath` alcanzables
6. Resuelve rutas offline usando `FileIndex`: indexa el sistema de archivos por sufijo y aplica score de desambiguación
7. Copia los medios al destino manteniendo estructura de carpetas
8. Genera un `.prproj` limpio con solo los objetos del subgrafo alcanzable

---

## Tests

```bash
python test_suite.py        # Suite completa: 139 tests
python test_file_index.py   # Solo FileIndex: 23 tests
```

`test_suite.py` cubre todas las capas del sistema:

| Area | Tests | Que verifica |
|------|-------|--------------|
| Utilidades de rutas | 25 | translate, normalize, is_absolute, path mappings |
| PrprojGraph | 22 | Parseo XML, navegacion del grafo, medios, anidamiento, trim |
| Ranking de secuencias | 5 | Topologia, promote/demote, densidad |
| FileIndex | 14 | Resolucion offline, extra roots, disambiguation, Unicode |
| After Effects | 9 | Escaneo AE binario/XML, gzip, dependencias |
| Read/Write prproj | 4 | Compresion gzip, roundtrip, XML plano |
| Seleccion de secuencia | 5 | Auto, patron, vacio |
| package_project (E2E) | 55 | Dry-run, copia real, offline, NAS, Mac, corrupto, fallbacks |

---

## Licencia

MIT
