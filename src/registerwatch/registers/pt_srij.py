"""Portugal — Serviço de Regulação e Inspeção de Jogos (SRIJ), licensed entities.

One page, one block per brand (`div.block-wysiwyg` with an id), each giving the
brand ("Marca"), the website and the operating entity ("Entidade exploradora").
"""

from __future__ import annotations

import re

from registerwatch.registers.base import Bundle, Column, ParseError, Part, Register, Table
from registerwatch.registers.extract import clean, host, html_doc, text

URL = "https://www.srij.turismodeportugal.pt/pt/jogo-online/entidades-licenciadas/"


def _field(block, label: str) -> str | None:
    for p in block.xpath(".//p"):
        t = text(p) or ""
        m = re.match(rf"{label}\s*:\s*(.*)$", t, re.I)
        if m:
            return clean(m.group(1))
    return None


def _brands(b: Bundle):
    doc = html_doc(b["entities"])
    blocks = [blk for blk in doc.xpath("//div[contains(@class,'block-wysiwyg') and @id]")
              if _field(blk, "Entidade exploradora")]
    if not blocks:
        raise ParseError("NO_BLOCKS", "div.block-wysiwyg[@id] with 'Entidade exploradora'")
    rows = []
    for blk in blocks:
        title = blk.xpath(".//div[contains(@class,'block-wysiwyg-title')]//h2")
        site = _field(blk, "Website")
        rows.append({"block_id": clean(blk.get("id")), "title": text(title[0]) if title else None,
                     "brand": _field(blk, "Marca"), "website": site, "host": host(site),
                     "operator": _field(blk, "Entidade exploradora")})
    return rows


REGISTER = Register(
    slug="pt_srij",
    name="Licensed online gambling entities",
    regulator="Serviço de Regulação e Inspeção de Jogos (SRIJ)",
    country="PT",
    kind="licensees",
    homepage=URL,
    parts=(Part("entities", URL, "html"),),
    tables=(
        Table("brands", (Column("block_id"), Column("title"), Column("brand"), Column("website", required=True),
                         Column("host"), Column("operator", required=True)), _brands, min_rows=5),
    ),
)
