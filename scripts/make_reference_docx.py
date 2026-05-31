#!/usr/bin/env python3
"""Готовит эталонный reference.docx со стилями под СТО кафедры.

Берёт дефолтный шаблон pandoc и патчит:
  - docDefaults: Times New Roman 14 pt, межстрочный 1,5, интервалы 0 пт;
  - поля страницы A4: левое 3, правое 1, верхнее 2, нижнее 2 см;
  - Normal/FirstParagraph/BodyText: выравнивание по ширине, абзац 1,25 см;
  - Heading1-3: TNR 14, без полужирного, по ширине с абзацным отступом;
  - подписи рисунков (ImageCaption/Caption): по центру, без отступа;
  - подписи таблиц (TableCaption): по левому краю, без отступа.

Запуск: python3 scripts/make_reference_docx.py
Результат: thesis/reference.docx
"""
import zipfile, re, shutil, subprocess, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = "/tmp/ref_default.docx"
OUT = os.path.join(ROOT, "thesis", "reference.docx")

# 1 см = 567 twips; 14 pt = 28 half-points; 1,5 интервала = line 360 auto.
FONT = ('<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
        'w:cs="Times New Roman"/><w:sz w:val="28"/><w:szCs w:val="28"/>')
NOBOLD = '<w:b w:val="0"/><w:bCs w:val="0"/>'
SPACING = '<w:spacing w:before="0" w:after="0" w:line="360" w:lineRule="auto"/>'
IND = '<w:ind w:firstLine="709"/>'           # абзацный отступ 1,25 см

# Параграфные/рунные свойства по стилям (styleId -> (pPr-inner, rPr-inner)).
BODY_P = SPACING + IND + '<w:jc w:val="both"/>'
HEAD_P = ('<w:keepNext/><w:spacing w:before="240" w:after="120" '
          'w:line="360" w:lineRule="auto"/>' + IND + '<w:jc w:val="both"/>')
# Раздел верхнего уровня всегда с новой страницы.
HEAD1_P = '<w:pageBreakBefore/>' + HEAD_P
IMGCAP_P = ('<w:keepLines/><w:spacing w:before="120" w:after="240" '
            'w:line="360" w:lineRule="auto"/><w:jc w:val="center"/>')
TBLCAP_P = ('<w:keepNext/><w:spacing w:before="240" w:after="60" '
            'w:line="360" w:lineRule="auto"/><w:jc w:val="left"/>')

STYLE_MAP = {
    "Normal":         (BODY_P, FONT),
    "FirstParagraph": (BODY_P, FONT),
    "BodyText":       (BODY_P, FONT),
    "Heading1":       (HEAD1_P, FONT + NOBOLD),
    "Heading2":       (HEAD_P, FONT + NOBOLD),
    "Heading3":       (HEAD_P, FONT + NOBOLD),
    "ImageCaption":   (IMGCAP_P, FONT + NOBOLD),
    "Caption":        (IMGCAP_P, FONT + NOBOLD),
    "TableCaption":   (TBLCAP_P, FONT + NOBOLD),
}


def patch_style(styles_xml, style_id, ppr_inner, rpr_inner):
    """В блоке стиля style_id убирает старые pPr/rPr и вставляет свои."""
    pat = re.compile(
        r'(<w:style [^>]*w:styleId="%s"[^>]*>)(.*?)(</w:style>)' % re.escape(style_id),
        re.S)
    m = pat.search(styles_xml)
    if not m:
        print("  ! стиль не найден:", style_id)
        return styles_xml
    head, body, tail = m.group(1), m.group(2), m.group(3)
    body = re.sub(r'<w:pPr>.*?</w:pPr>', '', body, flags=re.S)
    body = re.sub(r'<w:rPr>.*?</w:rPr>', '', body, flags=re.S)
    body += '<w:pPr>%s</w:pPr><w:rPr>%s</w:rPr>' % (ppr_inner, rpr_inner)
    return styles_xml[:m.start()] + head + body + tail + styles_xml[m.end():]


def main():
    # дефолтный шаблон pandoc
    subprocess.run(["pandoc", "-o", SRC, "--print-default-data-file",
                    "reference.docx"], check=True)

    zin = zipfile.ZipFile(SRC)
    styles = zin.read('word/styles.xml').decode('utf-8')
    doc = zin.read('word/document.xml').decode('utf-8')

    # docDefaults: шрифт и интервал на весь документ
    styles = re.sub(
        r'<w:rPrDefault>.*?</w:rPrDefault>',
        '<w:rPrDefault><w:rPr>' + FONT +
        '<w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault>',
        styles, flags=re.S)
    styles = re.sub(
        r'<w:pPrDefault>.*?</w:pPrDefault>',
        '<w:pPrDefault><w:pPr>' + SPACING + '</w:pPr></w:pPrDefault>',
        styles, flags=re.S)

    for sid, (ppr, rpr) in STYLE_MAP.items():
        styles = patch_style(styles, sid, ppr, rpr)

    # доп. стиль «Center» для титульного листа (custom-style в pandoc)
    center = ('<w:style w:type="paragraph" w:styleId="Center">'
              '<w:name w:val="Center"/><w:basedOn w:val="Normal"/>'
              '<w:pPr>' + SPACING + '<w:ind w:firstLine="0"/>'
              '<w:jc w:val="center"/></w:pPr>'
              '<w:rPr>' + FONT + '</w:rPr></w:style>')
    if 'w:styleId="Center"' not in styles:
        styles = styles.replace('</w:styles>', center + '</w:styles>')

    # поля страницы и размер A4 в sectPr
    # Поля по СТО: левое 3, правое 1, верхнее 2, нижнее 2 см. 1 см = 567 twips.
    pg = ('<w:pgSz w:w="11906" w:h="16838"/>'
          '<w:pgMar w:top="1134" w:right="567" w:bottom="1134" '
          'w:left="1701" w:header="720" w:footer="720" w:gutter="0"/>')
    if '<w:pgSz' not in doc:
        doc = doc.replace('</w:sectPr>', pg + '</w:sectPr>')

    # пересобрать docx
    shutil.copy(SRC, OUT)
    # перезаписываем изменённые части
    tmp = OUT + ".tmp"
    with zipfile.ZipFile(SRC) as zsrc, \
         zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zdst:
        for item in zsrc.namelist():
            data = zsrc.read(item)
            if item == 'word/styles.xml':
                data = styles.encode('utf-8')
            elif item == 'word/document.xml':
                data = doc.encode('utf-8')
            zdst.writestr(item, data)
    shutil.move(tmp, OUT)
    print("готово:", OUT)


if __name__ == "__main__":
    main()
