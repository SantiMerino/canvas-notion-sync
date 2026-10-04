"""Convierte el HTML que devuelve Canvas (descripciones, anuncios, páginas) al
"Notion-flavored Markdown" que acepta la API de Notion en /v1/pages/:id/markdown.

Diferencias con Markdown estándar que hay que cuidar:
- Las listas anidadas se indentan con tabs, no con espacios.
- Las tablas van como <table><tr><td>, no con pipes.
- Las ecuaciones inline van como $`latex`$.
- Las imágenes de Canvas requieren sesión, así que Notion no puede mostrarlas:
  se dejan como link en vez de bloque de imagen.
"""

import re

from markdownify import MarkdownConverter

# Caracteres que Notion interpreta como sintaxis y hay que escapar en texto plano.
# `*` y `_` ya los escapa markdownify.
NOTION_SPECIAL_CHARS = re.compile(r"([\\`~$\[\]<>{}|^])")
LIST_BULLET = re.compile(r"^(\S+ )")


class NotionConverter(MarkdownConverter):
    def __init__(self, base_url, **options):
        self.base_url = base_url.rstrip("/")
        options.setdefault("heading_style", "ATX")
        options.setdefault("bullets", "-")
        super().__init__(**options)

    def absolute(self, url):
        if not url or re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            return url
        return f"{self.base_url}/{url.lstrip('/')}"

    def escape(self, text, parent_tags):
        if not text:
            return ""
        if "pre" not in parent_tags and "code" not in parent_tags:
            text = NOTION_SPECIAL_CHARS.sub(r"\\\1", text)
        return super().escape(text, parent_tags)

    def convert_a(self, el, text, parent_tags):
        href = el.get("href")
        if href:
            el["href"] = self.absolute(href)
        return super().convert_a(el, text, parent_tags)

    def convert_img(self, el, text, parent_tags):
        # Canvas renderiza las ecuaciones del editor como <img class="equation_image">
        # con el LaTeX en un atributo; lo pasamos a ecuación nativa de Notion.
        latex = el.get("data-equation-content") or (
            el.get("title") if "equation_image" in (el.get("class") or []) else None
        )
        if latex:
            return f"$`{latex.strip()}`$"
        src = self.absolute(el.get("src"))
        if not src:
            return ""
        label = (el.get("alt") or "").strip() or "imagen"
        return f"[🖼️ {label}]({src})"

    def convert_iframe(self, el, text, parent_tags):
        src = self.absolute(el.get("src"))
        return f"\n\n[▶️ Contenido embebido]({src})\n\n" if src else ""

    def convert_li(self, el, text, parent_tags):
        # markdownify indenta el contenido anidado con espacios del ancho del
        # bullet; Notion necesita tabs para reconocer la jerarquía.
        md = super().convert_li(el, text, parent_tags)
        first, sep, rest = md.partition("\n")
        match = LIST_BULLET.match(first)
        if not match or not rest:
            return md
        pad = " " * len(match.group(1))
        lines = ["\t" + line[len(pad):] if line.startswith(pad) else line for line in rest.split("\n")]
        return first + sep + "\n".join(lines)

    def convert_table(self, el, text, parent_tags):
        rows = []
        for tr in el.find_all("tr"):
            cells = [
                f"\t\t<td>{cell_text(self, td)}</td>"
                for td in tr.find_all(["td", "th"], recursive=False)
            ]
            if cells:
                rows.append("\t<tr>\n" + "\n".join(cells) + "\n\t</tr>")
        if not rows:
            return ""
        has_header = el.find("th") is not None
        header = ' header-row="true"' if has_header else ""
        return f"\n\n<table{header}>\n" + "\n".join(rows) + "\n</table>\n\n"


def cell_text(converter, td):
    # Las celdas de Notion solo admiten rich text: se aplanan los bloques
    # internos y los saltos de línea se vuelven <br>.
    md = "".join(
        converter.process_element(child, parent_tags={"td", "_inline"}) for child in td.children
    ).strip()
    md = re.sub(r"^\s*[-*] ", "• ", md, flags=re.M)
    md = re.sub(r"\s*\n\s*", "<br>", md)
    return re.sub(r" {2,}", " ", md)


def escape_text(text):
    # Para texto plano de Canvas (nombres de criterios, niveles, etc.) que se
    # mete directo en el markdown sin pasar por el conversor de HTML.
    text = NOTION_SPECIAL_CHARS.sub(r"\\\1", str(text or ""))
    return text.replace("*", r"\*").replace("_", r"\_")


def html_to_notion_markdown(html, base_url):
    if not html:
        return ""
    md = NotionConverter(base_url).convert(html)
    # Notion ignora las líneas vacías; se colapsan para que el diff sea estable.
    return re.sub(r"\n{3,}", "\n\n", md).strip()
