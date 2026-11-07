"""One rule for a horse's name across the placer, the duplicate guard and the resulter.

v6.271 (Wilson 8 Nov 2026: "nz counts as same horse as some bookies display it, some dont")."""
import re

# A trailing NZ written as its own word: "Im Boo Nz", "Im Boo (NZ)", "Im Boo-NZ". A name ending in those letters ("Benz")
# keeps them. Other country tags are not touched.
_NZ_TAG_RE = re.compile(r"[\s\-]*[(\[]?\bnz\b[)\]]?\.?\s*$", re.I)


def drop_nz_tag(name) -> str:
    """The name without a trailing NZ tag. A name that is only the tag stays as it is."""
    s = str(name or "")
    out = _NZ_TAG_RE.sub("", s)
    return out if re.search(r"[^\W_]", out) else s
