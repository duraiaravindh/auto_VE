"""Optional Pexels search-only connector.

Results are metadata suggestions.  This module never downloads an asset and
never marks a result approved; the creator must select a file, save it locally,
and add its hash and license evidence to an asset catalogue.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class PexelsError(RuntimeError):
    """Pexels search failed without changing project media."""


def search_videos(
    query: str,
    *,
    api_key: str,
    per_page: int = 10,
    orientation: str = "portrait",
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    if not isinstance(query, str) or not query.strip() or len(query) > 120:
        raise PexelsError("Search query must contain 1-120 characters.")
    if not isinstance(api_key, str) or not api_key.strip():
        raise PexelsError("PEXELS_API_KEY is required for optional stock search.")
    if isinstance(per_page, bool) or not isinstance(per_page, int) or not 1 <= per_page <= 20:
        raise PexelsError("per_page must be from 1 to 20.")
    if orientation not in {"portrait", "landscape", "square"}:
        raise PexelsError("orientation must be portrait, landscape, or square.")
    params = urllib.parse.urlencode(
        {"query": query.strip(), "per_page": per_page, "orientation": orientation}
    )
    request = urllib.request.Request(
        f"https://api.pexels.com/videos/search?{params}",
        headers={"Authorization": api_key.strip(), "User-Agent": "NamaKadhaiLocalEditor/1"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PexelsError("Pexels search request failed; no asset was downloaded.") from exc
    except json.JSONDecodeError as exc:
        raise PexelsError("Pexels returned malformed JSON.") from exc
    videos = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        raise PexelsError("Pexels response did not contain a videos list.")
    results = []
    for raw in videos:
        if not isinstance(raw, dict):
            continue
        files = []
        for item in raw.get("video_files", []):
            if not isinstance(item, dict) or not isinstance(item.get("link"), str):
                continue
            files.append(
                {
                    "id": item.get("id"),
                    "quality": item.get("quality"),
                    "file_type": item.get("file_type"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "link": item["link"],
                }
            )
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        pictures = raw.get("video_pictures") if isinstance(raw.get("video_pictures"), list) else []
        results.append(
            {
                "provider": "Pexels",
                "provider_video_id": raw.get("id"),
                "page_url": raw.get("url"),
                "width": raw.get("width"),
                "height": raw.get("height"),
                "duration": raw.get("duration"),
                "creator": user.get("name"),
                "creator_url": user.get("url"),
                "preview_image": pictures[0].get("picture") if pictures and isinstance(pictures[0], dict) else None,
                "video_files": files,
                "approval": "PENDING",
                "downloaded": False,
                "license_review_required": True,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search Pexels videos without downloading them.")
    parser.add_argument("query")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-page", type=int, default=10)
    parser.add_argument("--orientation", choices=("portrait", "landscape", "square"), default="portrait")
    args = parser.parse_args(argv)
    if args.out.exists():
        print(f"Error: refusing to overwrite {args.out.name}.", file=sys.stderr)
        return 1
    try:
        results = search_videos(
            args.query,
            api_key=os.environ.get("PEXELS_API_KEY", ""),
            per_page=args.per_page,
            orientation=args.orientation,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "version": 1,
                    "query": args.query,
                    "provider": "Pexels",
                    "downloaded": False,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except PexelsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Suggestions: {args.out.resolve()}")
    print("No files were downloaded. Add only selected, approved files to the asset catalogue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

