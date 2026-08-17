#!/usr/bin/env python3
import os
import sys

# Konfiguration
OUTPUT_FILENAME = "project_export.txt"
EXCLUDE_DIRS = {".git", "__pycache__", "venv", "env"}
EXCLUDE_EXTS = (".db", ".log", ".pyc")
EXCLUDE_SUBSTRINGS = [".log."]
EXCLUDE_FILES = {OUTPUT_FILENAME, "code_exporter.py"}

def is_excluded(filename):
    return (
        filename in EXCLUDE_FILES
        or filename.endswith(EXCLUDE_EXTS)
        or any(sub in filename for sub in EXCLUDE_SUBSTRINGS)
    )

def generate_export():
    root_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(root_dir, OUTPUT_FILENAME)
    
    print(inne_msg := f"Starte Code-Export im Verzeichnis: {root_dir}")
    
    file_count = 0
    
    with open(output_path, "w", encoding="utf-8") as outfile:
        # 1. Verzeichnisbaum als Übersicht an den Anfang schreiben
        outfile.write("================================================================================\n")
        outfile.write("PROJEKT-VERZEICHNISSTRUKTUR\n")
        outfile.write("================================================================================\n")
        
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Ausgeschlossene Ordner herausfiltern
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            
            relative_path = os.path.relpath(dirpath, root_dir)
            if relative_path == ".":
                level = 0
                indent = ""
            else:
                level = relative_path.count(os.sep) + 1
                indent = "    " * level + "├── "
                
            outfile.write(f"{indent}{os.path.basename(dirpath) if relative_path != '.' else root_dir}/\n")
            
            sub_indent = "    " * (level + 1) + "├── "
            for filename in sorted(filenames):
                # HIER: Verwende die is_excluded Funktion
                if is_excluded(filename):
                    continue
                outfile.write(f"{sub_indent}{filename}\n")
                
        outfile.write("\n\n")
        outfile.write("================================================================================\n")
        outfile.write("DATEINHALTE\n")
        outfile.write("================================================================================\n\n")
        
        # 2. Alle Dateiinhalte durchgehen
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            
            for filename in sorted(filenames):
                # HIER: Ebenfalls is_excluded nutzen
                if is_excluded(filename):
                    continue
                
                file_path = os.path.join(dirpath, filename)
                rel_file_path = os.path.relpath(file_path, root_dir)
                
                outfile.write("================================================================================\n")
                outfile.write(f"Datei: {rel_file_path}\n")
                outfile.write("================================================================================\n")
                
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as infile:
                        outfile.write(infile.read())
                except Exception as e:
                    outfile.write(f"[Fehler beim Lesen der Datei: {e}]\n")
                    
                outfile.write("\n\n")
                file_count += 1

    print(f"Export erfolgreich abgeschlossen! {file_count} Dateien wurden in '{OUTPUT_FILENAME}' zusammengefasst.")

if __name__ == "__main__":
    generate_export()