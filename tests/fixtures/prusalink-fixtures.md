# PrusaLink fixtures

`prusalink-status.json`, `prusalink-job.json` and `prusalink-info.json` are
synthetic responses following Prusa's [v1 OpenAPI schemas](https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml)
and dmellok's [PrusaLink 0.1.3 test server](https://github.com/dmellok/tesserae-widget-prusalink/tree/v0.1.3/mock).
They are not recordings from a physical printer. `tests/make_prusalink_fixtures.py`
regenerates them and the three-layer `.gcode` and `.bgcode` cube. The binary
fixture uses Deflate and per-block CRC32; the plain file has a PNG thumbnail.

`prusa-reference.bgcode` is `mini_cube_ps2.8.1.bgcode` from
[Prusa Research's libbgcode](https://github.com/prusa3d/libbgcode/tree/d7353765dfc4b74d2503e4c86f081b5429ecec8b/tests/data),
AGPL-3.0 (the repository's LICENSE applies). It contains real Heatshrink 12/4,
MeatPack-with-comments, Deflate metadata and two QOI thumbnails. The test checks
all decoded printer instructions against the SHA-256 of the paired reference
`mini_cube_ps2.8.1_ref.gcode` after removing blank and comment lines. The
small synthetic tests also cover Heatshrink 11/4, both MeatPack encodings,
checksums, truncated blocks and oversized block declarations.
