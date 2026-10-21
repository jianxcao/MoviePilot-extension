"""发布清单必须指向实际源码，避免遮挡已有兼容实现。"""

import json
from pathlib import Path


def test_v3_index_resolves_every_published_source():
    """专用索引中的每个插件都必须能安装，不能误注册仅有 V2 目录的插件。"""
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "package.v3.json").read_text())
    for plugin_id in manifest:
        assert (root / "plugins.v3" / plugin_id.lower() / "__init__.py").is_file(), plugin_id


def test_cdp_plugin_does_not_advertise_missing_legacy_source():
    """CDP 专用实现只由 V3 索引发布，不让旧代索引选择不存在的目录。"""
    root = Path(__file__).resolve().parents[1]
    for name in ("package.json", "package.v2.json"):
        assert "PTCDPSignIn" not in json.loads((root / name).read_text())
