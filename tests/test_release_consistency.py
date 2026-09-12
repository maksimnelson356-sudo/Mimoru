import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_release_versions_match():
    version=(ROOT/'VERSION').read_text(encoding="utf-8").strip()
    text=(ROOT/'pyproject.toml').read_text(encoding="utf-8")
    m=re.search(r'^version\s*=\s*"([^"]+)"',text,re.M)
    assert m
    assert m.group(1)==version.replace('-rc','rc')
