# Vendored ALTO 4.4 schema

Used by `make check-alto-schema` (`scripts/check-alto-schema.py`) to validate the ALTO
that TuzkaOCR produces. Both files are byte-identical to upstream — do not edit them.

| File | Bytes | sha256 | Source |
| --- | --- | --- | --- |
| `alto-4-4.xsd` | 63830 | `2d1ba4b0ce268c4ed763f718cfb9b1ab67ac952caf5bcffa5fc314179cb0866b` | <https://raw.githubusercontent.com/altoxml/schema/master/v4/alto-4-4.xsd> |
| `xlink.xsd` | 3180 | `f1f5bb6003165cdd8f6c1fcc32f8fd1f965e1681010f3b9806d9460bcffa8a3c` | <https://web.archive.org/web/2023id_/http://www.loc.gov/standards/xlink/xlink.xsd> |

## Why vendored, and why from those URLs

The schema is fetched once and committed so the check runs offline and deterministically,
and because the canonical locations are not scriptable:

- **`alto-4-4.xsd`** is normally at `https://www.loc.gov/standards/alto/v4/alto-4-4.xsd`,
  but www.loc.gov answers non-browser clients with a Cloudflare interstitial. The
  `altoxml/schema` GitHub repository is the ALTO Editorial Board's own repository, so the
  mirror is authoritative rather than incidental.
- **`xlink.xsd`** is what `alto-4-4.xsd` line 120 imports for the `xlink` namespace
  (`http://www.loc.gov/standards/xlink/xlink.xsd`, needed for the `xlink:simpleLink`
  attribute group referenced at line 588). Same Cloudflare problem, and it is not in the
  `altoxml/schema` repository, so it came from the Wayback Machine. The `id_` suffix in
  that URL is what returns the original bytes instead of a rewritten page. The file
  identifies itself as "METS XLink Schema, v. 2, Nov. 15, 2004".

**Do not swap in a W3C xlink schema.** Both `https://www.w3.org/1999/xlink.xsd` and
`https://www.w3.org/XML/2008/06/xlink.xsd` target the same namespace but define
`simpleAttrs`, not the `simpleLink` attribute group ALTO references — the import resolves
and the schema then fails to build. For the same reason the checker passes
`use_fallback=False`, which stops xmlschema from silently substituting its own packaged
xlink schema, and promotes import warnings to errors so an unresolved import can't be
mistaken for a successful validation.

## Licence

ALTO is published by the ALTO Editorial Board under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/); the licence notice is in
the header of `alto-4-4.xsd`. The METS XLink schema is a Library of Congress work.
