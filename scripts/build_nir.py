#!/usr/bin/env python3
"""Собирает тело НИР (Введение → разделы 1-5 → Заключение → Список) по СТО.

Использует эталон thesis/reference.docx (TNR 14, интервал 1.5, поля,
заголовки без полужирного). Структурные элементы (Введение, Заключение,
Список) центрируются. Титульный «Отчёт по практике» и «Задания по
практике» добавляются отдельно (берутся из docx Сидорова со сменой данных).

Запуск: python3 scripts/build_nir.py  →  thesis/НИР_Фокин_Е.А.docx
"""
import zipfile, re, os, subprocess, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THESIS = os.path.join(ROOT, "thesis")
NIR = os.path.join(THESIS, "nir")
OUT = os.path.join(THESIS, "НИР_Фокин_Е.А.docx")
REFDOC = os.path.join(THESIS, "reference.docx")

STRUCTURAL = {"ВВЕДЕНИЕ", "ЗАКЛЮЧЕНИЕ",
              "СПИСОК ИСПОЛЬЗОВАННЫХ ИСТОЧНИКОВ"}


def center_structural(doc):
    def repl(m):
        para = m.group(0)
        if 'w:val="Heading1"' not in para:
            return para
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)).strip()
        if text.upper() not in STRUCTURAL:
            return para
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
    subprocess.run([
        "pandoc",
        os.path.join(NIR, "intro.md"),
        os.path.join(NIR, "body.md"),
        os.path.join(NIR, "conclusion.md"),
        os.path.join(NIR, "references.md"),
        "--from", "markdown", "--to", "docx",
        "--resource-path", THESIS,
        "--reference-doc", REFDOC,
        "--output", OUT,
    ], check=True, cwd=THESIS)

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
