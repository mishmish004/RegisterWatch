"""The intelligence layer: what the 21 registers say, as one model.

The register schemas keep each regulator's rows as published. This package
turns them into the five things people actually ask about, the same way for
every jurisdiction, with the history and the evidence carried along:

  party     a legal entity as one register names it
  licence   a permission a register says a party holds
  brand     a trading name
  domain    a website a licensee register lists (an authorised listing)
  block     a domain on a regulator's blocklist

and derives from their history the changes (events) and, for a domain, a
per-jurisdiction answer that is not a bare yes/no (verdict).

Everything here is pure, like the register parsers: rows in, facts out, no
network and no database. db/repos/model.py reads the register tables and writes
the result; the same functions run against fixtures in the tests.

  names        legal-entity names -> keys that compare across registers
  domains      hostnames -> registrable domain and label (public suffix list)
  facts        the five fact kinds, statuses, products, channels
  projections  one function per register: its rows -> facts
  build        facts -> model rows (versions collapsed) + change events
  verdict      a domain + what the registers say about it -> verdicts
  catalogue    what each register covers, and what is not covered at all
"""

# Bump when a projection or the build changes what it derives from the same
# rows: model.builds records it, so two builds are only compared like for like.
MODEL_VERSION = 1
