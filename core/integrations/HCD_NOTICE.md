# HCD source attribution

`hcd_topology.py` contains adapted source from
https://github.com/wanghaoyu6661/Hand-Drawn-Circuit-Diagram-Recognition
at commit `5e5a957f41bdb60f4835fb7c887452a6dc469520`.
The original snapshot remains in `third_party/upstream/HCD`.
Its top-level license was not identified in the acquired repository; no
permission for public redistribution is asserted. This integration is for
the user's existing local/private product workspace.

`FUNCTION_MANIFEST` records each adapted upstream function, original path,
original source-file SHA-256 and changes. The six retained algorithms are
adaptive point spacing, DBSCAN/median point merging, pixel line sampling,
eight-connected pixel indexing, nearby pixel snapping and noise filtering.
File I/O, global configuration, automatic gap bridging, guessed component
values, guessed pin roles, ground assignment and SPICE export are excluded.

Hough line extraction, pairwise intersection reporting and the JSON geometry
contract are product integration code, not functions attributed to HCD.
Pixel regions and intersection candidates carry no electrical meaning.
All geometry results require manual correction; the full HCD neural-model
pipeline is not executed by this module.
