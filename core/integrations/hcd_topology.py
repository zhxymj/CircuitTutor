"""HCD-derived image geometry; never an electrical topology or netlist.

The upstream functions listed in FUNCTION_MANIFEST are adapted source, with
I/O, global configuration, automatic bridging, pin guesses and netlist export
removed. OpenCV/NumPy/scikit-learn load only inside the isolated CV worker.
"""
import copy
import math

SOURCE_COMMIT = "5e5a957f41bdb60f4835fb7c887452a6dc469520"
SOURCE_URL = "https://github.com/wanghaoyu6661/Hand-Drawn-Circuit-Diagram-Recognition"
_SOURCES = {
    "merge_points.py": "23bb6408f37abbfc1425e481c313a2903f1e4632a924b586735dad3d13d80ec0",
    "build_connections.py": "0cac2ca8fee8eea5084dc971d6de2030b095c560406380fe4a5aa0205c517148",
    "remove_components.py": "90cfe7aec0029ba9bd924950075d207a7f8e779efc70dd3876742136fe6d6fbe",
}
FUNCTION_MANIFEST = [
    {"local_function": local, "source_function": local,
     "source_path": "src/pipeline/" + filename, "source_sha256": _SOURCES[filename],
     "adaptation": change}
    for local, filename, change in (
        ("estimate_eps", "merge_points.py", "Retain kth-neighbor quantile and clamping; lazy NumPy import."),
        ("merge_points_adaptive", "merge_points.py", "Retain DBSCAN, median and noise retention; deterministic order; remove printing and unused PCA."),
        ("sample_line_connectivity", "build_connections.py", "Retain inclusive straight-line neighborhood sampling; return finite pixel support only."),
        ("build_wire_cc_index", "build_connections.py", "Retain eight-connected stats and area keep table; use explicit minimum area."),
        ("snap_point_to_cc", "build_connections.py", "Retain expanding-window distance/area score; reject out-of-image points and enforce radius."),
        ("filter_noise_wires", "remove_components.py", "Retain area/span stats and vectorized keep; omit resizing, morphology and console output."),
    )
]


def source_manifest():
    return {
        "project": "HCD", "source_url": SOURCE_URL, "source_commit": SOURCE_COMMIT,
        "license": None, "license_status": "not_identified_in_acquired_repository",
        "usage_scope": "local_private_use", "full_hcd_model_pipeline": "not_executed",
        "functions": copy.deepcopy(FUNCTION_MANIFEST),
        "product_functions": ["compute_wire_geometry", "_segment_intersection"],
        "line_extractor": "OpenCV HoughLinesP; product integration, not an HCD source function",
    }


def estimate_eps(points, k=4, q=0.6, min_eps=5.0, max_eps=30.0):
    """Adapted from HCD merge_points.py::estimate_eps."""
    import numpy as np
    pts = np.array(points, dtype=np.float32)
    if len(pts) <= k:
        return float(min_eps)
    knn_dists = []
    for i in range(len(pts)):
        dx = pts[:, 0] - pts[i, 0]
        dy = pts[:, 1] - pts[i, 1]
        dists = np.sqrt(dx * dx + dy * dy)
        knn_dists.append(np.sort(dists)[k])
    return float(np.clip(np.quantile(knn_dists, q), min_eps, max_eps))


def merge_points_adaptive(points, base_eps=None, k_for_eps=4, q_for_eps=0.3,
                          min_eps=3.5, max_eps=15.0, min_samples=1):
    """Adapted DBSCAN/cluster-median/noise retention from HCD merge_points.py."""
    if not len(points):
        return []
    import numpy as np
    from sklearn.cluster import DBSCAN
    pts = np.array(points, dtype=np.float32)
    eps = estimate_eps(points, k=k_for_eps, q=q_for_eps,
                       min_eps=min_eps, max_eps=max_eps) if base_eps is None else float(base_eps)
    labels = DBSCAN(eps=eps, min_samples=min_samples).fit(pts).labels_
    final_pts = []
    for label in sorted(set(labels)):
        if label == -1:
            continue
        cluster_pts = pts[labels == label]
        final_pts.append((float(np.median(cluster_pts[:, 0])),
                          float(np.median(cluster_pts[:, 1]))))
    for i, label in enumerate(labels):
        if label == -1:
            final_pts.append((float(pts[i, 0]), float(pts[i, 1])))
    return final_pts


def sample_line_connectivity(wire_img, x0, y0, x1, y1, radius=1):
    """HCD white-pixel support sampler; this ratio is not confidence in a net."""
    height, width = wire_img.shape[:2]
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    if length < 1e-3:
        sx, sy = int(round(x0)), int(round(y0))
        return float(0 <= sx < width and 0 <= sy < height and wire_img[sy, sx] > 0)
    num_samples = max(int(length), 2)
    hits = 0
    for i in range(num_samples + 1):
        t = i / num_samples
        sx, sy = int(round(x0 + dx * t)), int(round(y0 + dy * t))
        found = False
        for yy in range(sy - radius, sy + radius + 1):
            if found:
                break
            for xx in range(sx - radius, sx + radius + 1):
                if 0 <= xx < width and 0 <= yy < height and wire_img[yy, xx] > 0:
                    hits += 1
                    found = True
                    break
    return min(1.0, hits / (num_samples + 1))


def build_wire_cc_index(wire_img, min_area=3):
    """HCD connected-component keep table; labels denote pixels, never nets."""
    import cv2
    import numpy as np
    binary = (wire_img > 0).astype(np.uint8)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    areas = stats[:, cv2.CC_STAT_AREA].astype(np.int64)
    keep = np.zeros((num_labels,), dtype=np.bool_)
    keep[1:] = areas[1:] >= int(min_area)
    return {"labels": labels, "areas": areas, "keep": keep,
            "min_area": int(min_area), "num_labels": int(num_labels)}


def snap_point_to_cc(cc, x, y, max_r=3, step=2, alpha=0.1):
    """HCD expanding-window snap, with explicit out-of-image/radius rejection."""
    import numpy as np
    height, width = cc["labels"].shape[:2]
    if not (0 <= x < width and 0 <= y < height):
        return float(x), float(y), 0, float("inf")
    x0, y0 = int(round(x)), int(round(y))
    if not (0 <= x0 < width and 0 <= y0 < height):
        return float(x), float(y), 0, float("inf")
    labels, keep, areas = cc["labels"], cc["keep"], cc["areas"]
    lab0 = int(labels[y0, x0])
    if lab0 and keep[lab0]:
        return float(x0), float(y0), lab0, 0.0
    radii = list(range(1, int(max_r) + 1, max(1, int(step))))
    if max_r > 0 and int(max_r) not in radii:
        radii.append(int(max_r))
    for radius in radii:
        x1, x2 = max(0, x0 - radius), min(width, x0 + radius + 1)
        y1, y2 = max(0, y0 - radius), min(height, y0 + radius + 1)
        win = labels[y1:y2, x1:x2]
        mask = win != 0
        if not mask.any():
            continue
        labs = win[mask]
        retained = keep[labs]
        ys, xs = np.where(mask)
        xs, ys, labs = xs[retained] + x1, ys[retained] + y1, labs[retained]
        if not len(xs):
            continue
        distance = np.sqrt((xs.astype(np.float32) - x0) ** 2 + (ys.astype(np.float32) - y0) ** 2)
        within = distance <= max_r
        xs, ys, labs, distance = xs[within], ys[within], labs[within], distance[within]
        if not len(xs):
            continue
        score = distance - float(alpha) * np.log1p(areas[labs].astype(np.float32))
        index = int(np.argmin(score))
        return float(xs[index]), float(ys[index]), int(labs[index]), float(distance[index])
    return float(x0), float(y0), 0, float("inf")


def filter_noise_wires(bin_img, min_area=50, min_length=30):
    """HCD stats-based area/span filter, without upstream bridging or resize."""
    import cv2
    import numpy as np
    filtered = (bin_img > 0).astype(np.uint8) * 255
    count, labels, stats, _ = cv2.connectedComponentsWithStats(filtered, connectivity=8)
    areas = stats[:, cv2.CC_STAT_AREA]
    lengths = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT])
    keep = np.ones((count,), dtype=np.bool_)
    keep[0] = True
    keep[1:] = (areas[1:] >= int(min_area)) & (lengths[1:] >= int(min_length))
    return cv2.bitwise_and(filtered, filtered, mask=keep[labels].astype(np.uint8) * 255)


def _segment_intersection(a, b):
    """Product helper: geometric intersection only; no junction classification."""
    ax, ay, bx, by = a
    cx, cy, dx, dy = b
    rx, ry, sx, sy = bx - ax, by - ay, dx - cx, dy - cy
    denominator = rx * sy - ry * sx
    if abs(denominator) < 1e-9:
        return None
    t = ((cx - ax) * sy - (cy - ay) * sx) / denominator
    u = ((cx - ax) * ry - (cy - ay) * rx) / denominator
    if not (0 <= t <= 1 and 0 <= u <= 1):
        return None
    # Ordinary shared endpoints are already represented by point candidates.
    if (t < 1e-6 or t > 1 - 1e-6) and (u < 1e-6 or u > 1 - 1e-6):
        return None
    return float(ax + t * rx), float(ay + t * ry)


def compute_wire_geometry(mask, *, component_boxes=(), line_segments=None, max_segments=160):
    """Return untrusted JSON geometry from an original-size 2D foreground mask.

    Boxes are pixel {x,y,w,h}; supplied line_segments are [x1,y1,x2,y2].
    Hough extraction is a product helper. HCD supplies clustering, sampling,
    component filtering/indexing and snapping. Foreground may include text,
    outlines, crossings and shorts; no electrical information is inferred.
    """
    import cv2
    import numpy as np
    array = np.asarray(mask)
    if array.ndim != 2 or not array.size or not np.isfinite(array).all():
        raise ValueError("mask must be a nonempty finite two-dimensional image")
    if not isinstance(max_segments, int) or not 1 <= max_segments <= 500:
        raise ValueError("max_segments must be an integer between 1 and 500")
    height, width = array.shape
    foreground = (array > 0).astype(np.uint8) * 255
    for box in component_boxes:
        coordinates = [float(box[key]) for key in ("x", "y", "w", "h")]
        if not all(math.isfinite(v) for v in coordinates) or min(coordinates[2:]) < 0:
            raise ValueError("component box must have finite coordinates and nonnegative size")
        x, y, box_width, box_height = coordinates
        left, top = max(0, math.floor(x)), max(0, math.floor(y))
        right, bottom = min(width, math.ceil(x + box_width)), min(height, math.ceil(y + box_height))
        if right > left and bottom > top:
            foreground[top:bottom, left:right] = 0
    foreground = filter_noise_wires(foreground, min_area=3, min_length=3)
    cc = build_wire_cc_index(foreground, min_area=3)
    if line_segments is None:
        min_length = max(6, int(min(height, width) * 0.015))
        detected = cv2.HoughLinesP(foreground, 1, math.pi / 180,
            threshold=max(5, min_length // 2), minLineLength=min_length, maxLineGap=2)
        candidates = [] if detected is None else detected.reshape(-1, 4).tolist()
    else:
        candidates = list(line_segments)
    valid, seen = [], set()
    for raw in candidates:
        if len(raw) != 4:
            raise ValueError("line segment requires four coordinates")
        coordinates = tuple(float(v) for v in raw)
        if not all(math.isfinite(v) for v in coordinates):
            raise ValueError("line segment coordinates must be finite")
        x1, y1, x2, y2 = coordinates
        if not (0 <= x1 < width and 0 <= x2 < width and 0 <= y1 < height and 0 <= y2 < height):
            continue
        if math.hypot(x2 - x1, y2 - y1) < 2:
            continue
        canonical = min(coordinates, (x2, y2, x1, y1))
        if canonical in seen:
            continue
        seen.add(canonical)
        support = sample_line_connectivity(foreground, *canonical, radius=1)
        if support >= 0.9:
            valid.append((canonical, support))
    valid.sort(key=lambda entry: (-math.hypot(entry[0][2] - entry[0][0], entry[0][3] - entry[0][1]), entry[0]))
    truncated = len(valid) > max_segments
    valid = valid[:max_segments]
    raw_points = [point for coordinates, _ in valid
                  for point in (coordinates[:2], coordinates[2:])]
    merged = merge_points_adaptive(raw_points, min_eps=1.5, max_eps=3.5)
    points = []
    for index, (x, y) in enumerate(merged):
        sx, sy, label, distance = snap_point_to_cc(cc, x, y, max_r=3)
        points.append({"id": "P" + str(index + 1), "x": x, "y": y,
            "snapped_x": sx, "snapped_y": sy, "cc_label": label,
            "snap_dist": distance if math.isfinite(distance) else None})
    segments = []
    for index, (coordinates, support) in enumerate(valid):
        x1, y1, x2, y2 = coordinates
        point_ids = [min(points, key=lambda point: math.hypot(point["x"] - x, point["y"] - y))["id"]
                     for x, y in ((x1, y1), (x2, y2))]
        segments.append({"id": "S" + str(index + 1), "x1": x1, "y1": y1,
                         "x2": x2, "y2": y2, "pixel_support": support,
                         "point_ids": point_ids})
    regions = []
    for label in sorted({point["cc_label"] for point in points if point["cc_label"]}):
        regions.append({"id": label, "area": int(cc["areas"][label]),
            "endpoint_ids": [point["id"] for point in points if point["cc_label"] == label]})
    intersections = []
    for i, (a, _) in enumerate(valid):
        for j in range(i + 1, len(valid)):
            crossing = _segment_intersection(a, valid[j][0])
            if crossing is not None:
                intersections.append({"x": crossing[0], "y": crossing[1],
                    "segment_ids": [segments[i]["id"], segments[j]["id"]],
                    "kind": "unconfirmed_crossing"})
    return {"schema_version": 1, "status": "geometry_candidates", "source": "hcd_native",
        "candidate_only": True, "accepted": False, "electrical_validated": False,
        "width": int(width), "height": int(height), "segments": segments, "points": points,
        "pixel_regions": regions, "ambiguous_intersections": intersections,
        "truncated": truncated, "foreground_pixels_after_exclusion": int(np.count_nonzero(foreground)),
        "topology": {"status": "not_inferred", "pins": None, "connections": [], "nets": []},
        "provenance": source_manifest(),
        "issues": [{"code": "GEOMETRY_ONLY", "message": "像素线段、聚类端点和交叉候选需要人工确认；未推断引脚、地、数值或电气连接。"}]}
