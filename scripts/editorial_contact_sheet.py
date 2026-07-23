from __future__ import annotations

import argparse
import sqlite3
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render Runway editorial QA contact sheets.")
    parser.add_argument("--database", type=Path, default=Path("data/qlob-production/runway.db"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--candidate-run", type=int)
    source.add_argument("--candidate-ids")
    source.add_argument("--proposal-min-id", type=int)
    parser.add_argument("--proposal-max-id", type=int)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_rows(connection: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    if args.candidate_run is not None or args.candidate_ids:
        parameters: list[int]
        where_clause: str
        if args.candidate_run is not None:
            where_clause = "c.search_run_id = ?"
            parameters = [args.candidate_run]
        else:
            try:
                parameters = [
                    int(value.strip())
                    for value in str(args.candidate_ids).split(",")
                    if value.strip()
                ]
            except ValueError as exc:
                raise SystemExit("--candidate-ids must be comma-separated integers") from exc
            if not parameters:
                raise SystemExit("--candidate-ids cannot be empty")
            placeholders = ",".join("?" for _value in parameters)
            where_clause = f"c.id IN ({placeholders})"
        rows = connection.execute(
            f"""
            SELECT c.id, m.local_path, c.search_query, c.source_domain,
                   c.final_rank_score, c.detected_topic_json
            FROM candidate_images AS c
            JOIN media_assets AS m ON m.id = c.media_asset_id
            WHERE {where_clause} AND c.hard_rejection_reason IS NULL
            ORDER BY c.id
            """,
            parameters,
        ).fetchall()
        return [
            {
                "id": row["id"],
                "path": row["local_path"],
                "lines": [
                    f"candidate #{row['id']} | score {row['final_rank_score']:.3f}",
                    str(row["search_query"]),
                    str(row["source_domain"] or "unknown source"),
                ],
            }
            for row in rows
        ]
    parameters: list[int] = [args.proposal_min_id]
    maximum = ""
    if args.proposal_max_id is not None:
        maximum = " AND p.id <= ?"
        parameters.append(args.proposal_max_id)
    rows = connection.execute(
        f"""
        SELECT p.id, p.status, p.recommended_caption, p.alternative_captions_json,
               p.factual_uncertainty_warning, m.local_path
        FROM proposals AS p
        JOIN candidate_images AS c ON c.id = p.candidate_image_id
        JOIN media_assets AS m ON m.id = c.media_asset_id
        WHERE p.id >= ?{maximum}
        ORDER BY p.id
        """,
        parameters,
    ).fetchall()
    return [
        {
            "id": row["id"],
            "path": row["local_path"],
            "lines": [
                f"proposal #{row['id']} | {row['status']}",
                str(row["recommended_caption"]),
                (
                    f"warning: {row['factual_uncertainty_warning']}"
                    if row["factual_uncertainty_warning"]
                    else "warning: none"
                ),
            ],
        }
        for row in rows
    ]


def render_page(
    rows: list[dict[str, Any]],
    *,
    data_root: Path,
    destination: Path,
) -> None:
    columns = 4
    cell_width = 360
    image_height = 250
    text_height = 120
    gutter = 16
    page_rows = (len(rows) + columns - 1) // columns
    width = columns * cell_width + (columns + 1) * gutter
    height = page_rows * (image_height + text_height + gutter) + gutter
    canvas = Image.new("RGB", (width, height), "#f4f1ea")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=16)
    for index, row in enumerate(rows):
        column = index % columns
        line = index // columns
        x = gutter + column * (cell_width + gutter)
        y = gutter + line * (image_height + text_height + gutter)
        source = data_root / str(row["path"])
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((cell_width, image_height), Image.Resampling.LANCZOS)
                frame = Image.new("RGB", (cell_width, image_height), "#17151b")
                frame.paste(
                    image,
                    ((cell_width - image.width) // 2, (image_height - image.height) // 2),
                )
        except (OSError, ValueError):
            frame = Image.new("RGB", (cell_width, image_height), "#7f1d1d")
            ImageDraw.Draw(frame).text((16, 16), "IMAGE LOAD FAILED", fill="white", font=font)
        canvas.paste(frame, (x, y))
        text_y = y + image_height + 8
        wrapped: list[str] = []
        for value in row["lines"]:
            wrapped.extend(textwrap.wrap(value, width=42) or [""])
        draw.multiline_text(
            (x, text_y),
            "\n".join(wrapped[:6]),
            fill="#17151b",
            font=font,
            spacing=4,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, optimize=True)


def main() -> None:
    args = parse_args()
    if args.page_size < 1 or args.page_size > 40:
        raise SystemExit("--page-size must be between 1 and 40")
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"database not found: {database}")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        rows = load_rows(connection, args)
    if not rows:
        raise SystemExit("no matching editorial rows")
    data_root = database.parent
    output = args.output.resolve()
    pages = [rows[index : index + args.page_size] for index in range(0, len(rows), args.page_size)]
    for index, page in enumerate(pages, start=1):
        destination = output.with_name(f"{output.stem}-{index:02d}{output.suffix}")
        render_page(page, data_root=data_root, destination=destination)
        print(destination)


if __name__ == "__main__":
    main()
