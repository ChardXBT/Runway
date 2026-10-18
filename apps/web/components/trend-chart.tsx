"use client";

import { useId, useMemo, useState } from "react";

import { formatCompactNumber } from "@/lib/format";

export type TrendPoint = { label: string; value: number };

const WIDTH = 560;
const HEIGHT = 160;
const COMPACT_HEIGHT = 72;
const PAD_Y = 10;

function buildGeometry(points: TrendPoint[], height: number, padX: number) {
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const innerW = WIDTH - padX * 2;
  const innerH = height - PAD_Y * 2;
  const coords = points.map((point, index) => {
    const x = padX + (points.length === 1 ? innerW / 2 : (index / (points.length - 1)) * innerW);
    const y = PAD_Y + (1 - (point.value - min) / span) * innerH;
    return { x, y };
  });
  const line = coords
    .map((coord, index) => `${index === 0 ? "M" : "L"}${coord.x.toFixed(1)},${coord.y.toFixed(1)}`)
    .join(" ");
  const area = `${line} L${coords[coords.length - 1].x.toFixed(1)},${height - PAD_Y} L${coords[0].x.toFixed(1)},${height - PAD_Y} Z`;
  return { coords, line, area, min, max };
}

export function TrendChart({
  points,
  label,
  compact = false,
  valueFormat = formatCompactNumber,
}: {
  points: TrendPoint[];
  label: string;
  compact?: boolean;
  valueFormat?: (value: number) => string;
}) {
  const gradientId = useId();
  const [hover, setHover] = useState<number | null>(null);
  const height = compact ? COMPACT_HEIGHT : HEIGHT;
  const padX = compact ? 8 : 44;
  const geometry = useMemo(
    () => (points.length > 0 ? buildGeometry(points, height, padX) : null),
    [points, height, padX],
  );

  if (points.length === 0 || geometry === null) {
    return (
      <div className="trend-chart-empty" role="img" aria-label={`${label}: no data`}>
        <span>No data in range</span>
      </div>
    );
  }

  const { coords, line, area, min, max } = geometry;
  const ariaLabel = `${label}: ${points.length} points, first ${valueFormat(points[0].value)}, last ${valueFormat(points[points.length - 1].value)}, low ${valueFormat(min)}, high ${valueFormat(max)}`;
  const xLabels = [points[0], points[Math.floor((points.length - 1) / 2)], points[points.length - 1]];

  return (
    <figure className={`trend-chart${compact ? " trend-chart-compact" : ""}`}>
      <svg
        viewBox={`0 0 ${WIDTH} ${height}`}
        role="img"
        aria-label={ariaLabel}
        onPointerLeave={() => setHover(null)}
        onPointerMove={(event) => {
          if (compact) return;
          const rect = (event.currentTarget as SVGSVGElement).getBoundingClientRect();
          const x = ((event.clientX - rect.left) / rect.width) * WIDTH;
          let nearest = 0;
          coords.forEach((coord, index) => {
            if (Math.abs(coord.x - x) < Math.abs(coords[nearest].x - x)) {
              nearest = index;
            }
          });
          setHover(nearest);
        }}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--brand)" stopOpacity="0.16" />
            <stop offset="100%" stopColor="var(--brand)" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 1 / 3, 2 / 3, 1].map((fraction) => {
          const y = PAD_Y + (1 - fraction) * (height - PAD_Y * 2);
          return (
            <g key={fraction}>
              <line
                x1={padX}
                x2={WIDTH - padX}
                y1={y}
                y2={y}
                className="trend-chart-gridline"
              />
              {!compact ? (
                <text
                  x={padX - 6}
                  y={y + 3}
                  textAnchor="end"
                  className="trend-chart-gridlabel"
                >
                  {valueFormat(min + fraction * (max - min || 0))}
                </text>
              ) : null}
            </g>
          );
        })}
        <path d={area} fill={`url(#${gradientId})`} />
        <path d={line} className="trend-chart-line" />
        {hover !== null && !compact ? (
          <g className="trend-chart-hover">
            <line
              x1={coords[hover].x}
              x2={coords[hover].x}
              y1={PAD_Y}
              y2={height - PAD_Y}
            />
            <circle cx={coords[hover].x} cy={coords[hover].y} r="3" />
          </g>
        ) : null}
      </svg>
      {!compact ? (
        <div className="trend-chart-axis">
          {xLabels.map((point, index) => (
            <span key={index}>{point.label}</span>
          ))}
        </div>
      ) : null}
      {hover !== null && !compact ? (
        <div className="trend-chart-tooltip" role="status">
          {points[hover].label}: <strong>{valueFormat(points[hover].value)}</strong>
        </div>
      ) : null}
      <table className="visually-hidden">
        <caption>{label}</caption>
        <thead>
          <tr>
            <th>Point</th>
            <th>Value</th>
          </tr>
        </thead>
        <tbody>
          {points.map((point, index) => (
            <tr key={index}>
              <td>{point.label}</td>
              <td>{point.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
