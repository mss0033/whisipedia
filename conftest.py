"""Repository-wide test guard: tests never see a machine's ``config/local.toml``.

``config/local.toml`` points at the real vault (NAS) and index. Without this, any test that loads the
repository's config directory would read or write live data.
"""

import os

os.environ["WSP_NO_LOCAL_CONFIG"] = "1"
