# Changelog

Todos los cambios notables de este proyecto se documentan en este archivo.

El formato sigue [Keep a Changelog](https://keepachangelog.com/es/1.0.0/)
y el proyecto usa [Versionado Semantico](https://semver.org/lang/es/).

---

## [0.5.0] - 2026-04-08

### Agregado
- Indice de archivos (`FileIndex`) para resolver archivos offline con busqueda inteligente por sub-ruta, reemplazando la resolucion directa por mapeo de rutas.
- Soporte para raices de busqueda adicionales al resolver archivos offline que viven fuera del arbol del proyecto (Dropbox, NAS, etc.).
- Seleccion multiple de secuencias para empaquetar en una sola operacion.

### Corregido
- El empaquetado ahora incluye unicamente los archivos de media referenciados por la secuencia seleccionada, eliminando archivos innecesarios del paquete final.

---

## [0.4.0] - 2026-03-30

### Agregado
- Escaneo de proyectos de After Effects (`.aep`) para extraer sus dependencias de footage durante el empaquetado.
- Inclusion de proyectos AE encontrados dentro del arbol del proyecto al empaquetar.
- Filtro que excluye las carpetas de auto-guardado de After Effects (`Auto-Save`) al escanear proyectos AE.

### Corregido
- Deteccion correcta de la raiz del proyecto al incluir proyectos AE.

---

## [0.3.0] - 2026-03-02

### Agregado
- Interfaz grafica (GUI) con Tkinter para seleccionar y empaquetar proyectos sin usar la linea de comandos.
- Lista de proyectos con casillas de verificacion y barra de busqueda/filtro en la GUI.
- Barra de progreso animada y contador en tiempo real durante el escaneo de carpetas.
- Escaneo de carpetas en hilo de fondo para evitar que la interfaz se congele.
- Vista del arbol de carpetas del destino en los logs al terminar el empaquetado.

### Mejorado
- Aceleracion del escaneo de proyectos usando `os.walk` con poda de directorios irrelevantes.
- El boton "seleccionar/deseleccionar todo" actua globalmente y, cuando hay un filtro activo, solo afecta los elementos visibles.
- El contador del boton refleja correctamente la cantidad de elementos visibles y seleccionados cuando el filtro esta activo.

### Corregido
- El evento toggle-all se disparaba dos veces al hacer clic en el encabezado de la lista.
- Reversion del escaneo paralelo a secuencial para mayor estabilidad, manteniendo reporte de progreso cada 5 archivos.

---

## [0.2.0] - 2026-02-25

### Agregado
- Selector manual de secuencia para elegir que secuencia de Premiere empaquetar.
- Filtro de secuencias internas anidadas y auto-generadas para que no aparezcan en la lista de seleccion.
- Recorte del XML del `.prproj` a la secuencia seleccionada, equivalente al Project Manager de Premiere.
- Traduccion de rutas de Mac a Windows (mapeos configurables, con valores predeterminados para `/Volumes/SEGUIMIENTOS`, `NAS-Dropbox`, `Dropbox-Stupendastic`).
- Normalizacion Unicode NFD a NFC para evitar falsos positivos de archivos OFFLINE en rutas de Mac.
- Opcion para omitir proyectos que ya fueron empaquetados previamente.
- Agrupacion de archivos `.prproj` por proyecto, seleccionando automaticamente el mas reciente de cada grupo.
- Rutas relativas en el `.prproj` empaquetado para mayor portabilidad.
- Conservacion de la estructura original de carpetas del proyecto en el destino de empaquetado.
- Los bins de media se renombran a `Otros`, se eliminan prefijos numerados y se muestran los tamanos en el arbol.
- Checkbox de limite de proyectos y estilo visual liviano en la GUI inicial.
- Script de diagnostico para inspeccionar la estructura XML de archivos `.prproj`.

### Corregido
- Deteccion correcta de la raiz del proyecto buscando la carpeta numerada mas alta y retornando su padre.
- Traversal del grafo de objetos basado en la estructura XML real de `.prproj`.
- Los mapeos de ruta predeterminados (Mac-Win) siempre estan presentes aunque la configuracion guardada sea antigua o este vacia.
- Mapeos de NAS adicionales: `NAS-Dropbox/DATA/SEGUIMIENTOS=V:` y `DropboxStupendastic 2023` para referencias cruzadas entre unidades.

---

## [0.1.0] - 2026-02-24

### Agregado
- Empaquetador inicial de proyectos Premiere Pro por linea de comandos.
- Parseo de archivos `.prproj` (XML comprimido con gzip) para extraer referencias de media.
- Copia de archivos de media referenciados al directorio de destino.
- Soporte para entrada de un unico archivo `.prproj`.
- Filtrado de secuencias de Auto-Save al listar secuencias disponibles.
