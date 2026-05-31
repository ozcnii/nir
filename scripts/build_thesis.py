#!/usr/bin/env python3
"""Собирает thesis.docx из MD по СТО.

Шаги:
  1. Готовит чистый список источников (заголовок прописными, без рабочих
     заметок).
  2. pandoc: intro + chapter1 + источники с эталоном thesis/reference.docx.
  3. Пост-обработка: структурные элементы (ВВЕДЕНИЕ, ЗАКЛЮЧЕНИЕ,
     СОДЕРЖАНИЕ, СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ) — по центру без
     абзацного отступа; нумерованные разделы остаются по ширине.

Перед запуском должен существовать thesis/reference.docx
(scripts/make_reference_docx.py). Запуск: python3 scripts/build_thesis.py
"""
import zipfile, re, os, subprocess, shutil, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THESIS = os.path.join(ROOT, "thesis")
OUT = os.path.join(THESIS, "thesis.docx")
REFDOC = os.path.join(THESIS, "reference.docx")

STRUCTURAL = {"РЕФЕРАТ", "СОДЕРЖАНИЕ", "ВВЕДЕНИЕ", "ЗАКЛЮЧЕНИЕ",
              "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ"}


def prepare_refs():
    src = os.path.join(THESIS, "references.md")
    with open(src, encoding="utf-8") as f:
        lines = f.readlines()
    # берём всё начиная с первой записи (строка, начинающаяся с «N »),
    # СОХРАНЯЯ многострочные записи (URL, дата) и пустые строки-разделители
    start = next((i for i, ln in enumerate(lines)
                  if re.match(r"^\d+\s", ln)), 0)
    body = "".join(lines[start:])
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ\n\n")
        f.write(body)
    return path


def center_structural(doc):
    """Структурным заголовкам Heading1 ставит выравнивание по центру."""
    def repl(m):
        para = m.group(0)
        if 'w:val="Heading1"' not in para:
            return para
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)).strip()
        if text.upper() not in STRUCTURAL:
            return para
        # убрать абзацный отступ и поставить по центру внутри pPr
        para = re.sub(r"<w:ind\b[^/]*/>", "", para)
        para = re.sub(r"<w:jc\b[^/]*/>", "", para)
        para = para.replace(
            "</w:pPr>",
            '<w:ind w:firstLine="0"/><w:jc w:val="center"/></w:pPr>', 1)
        return para
    return re.sub(r"<w:p\b.*?</w:p>", repl, doc, flags=re.S)


def main():
    if not os.path.exists(REFDOC):
        raise SystemExit("Нет thesis/reference.docx — запусти "
                         "scripts/make_reference_docx.py")
    refs = prepare_refs()
    subprocess.run([
        "pandoc",
        os.path.join(THESIS, "front_matter.md"),
        os.path.join(THESIS, "intro.md"),
        os.path.join(THESIS, "chapter1.md"),
        refs,
        "--from", "markdown", "--to", "docx",
        "--resource-path", THESIS,
        "--reference-doc", REFDOC,
        "--output", OUT,
    ], check=True, cwd=THESIS)
    os.unlink(refs)

    # пост-обработка document.xml
    tmp = OUT + ".tmp"
    with zipfile.ZipFile(OUT) as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "word/document.xml":
                data = center_structural(data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    shutil.move(tmp, OUT)
    print("собрано:", OUT)


if __name__ == "__main__":
    main()
