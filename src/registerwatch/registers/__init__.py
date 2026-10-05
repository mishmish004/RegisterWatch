"""Every register this project scrapes. Adding one is a module and one line here.

Regulators that were checked and are NOT here — no public register, a register
behind a browser challenge, search-only lookups — are listed with the reason in
REGULATORS.md, so "not scraped" is a decision on record rather than an omission.
"""

from __future__ import annotations

from registerwatch.registers import (
    au_acma, be_gc, ca_kgc, ca_on_igo, ch_esbk, ch_gespa, cz_mf, de_ggl, ee_emta, es_dgoj, fr_anj,
    gb_ukgc, gr_hgc, ie_revenue, im_gsc, it_adm, pl_mf, pt_srij, se_si, sk_urhh, us_nj_dge,
)
from registerwatch.registers.base import Register

REGISTRY: dict[str, Register] = {
    m.REGISTER.slug: m.REGISTER
    for m in (
        gb_ukgc, be_gc, de_ggl, es_dgoj, fr_anj, pt_srij, se_si, im_gsc, ca_kgc, ca_on_igo,
        pl_mf, it_adm, gr_hgc, ee_emta, cz_mf, ch_gespa, ch_esbk, us_nj_dge, sk_urhh, ie_revenue, au_acma,
    )
}


def get(slug: str) -> Register:
    try:
        return REGISTRY[slug]
    except KeyError:
        raise KeyError(f"unknown register {slug!r}; known: {', '.join(sorted(REGISTRY))}") from None


def all_registers() -> list[Register]:
    return [REGISTRY[s] for s in sorted(REGISTRY)]
