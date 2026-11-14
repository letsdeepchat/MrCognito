import { useMemo, useState } from "react";
import type { MaskedBoard } from "../api/types";

const FILES = "abcdefgh";
const GLYPH: Record<string, string> = { K: "♚", Q: "♛", R: "♜", B: "♝", N: "♞", P: "♟" };
const MASK_LABEL = ["Full board", "Silhouettes", "Coordinates only", "Empty board"];

export interface BoardProps {
  board: MaskedBoard;
  onMove?: (uci: string) => void;
  disabled?: boolean;
  /** Placement editor (Phase 5): controlled map square -> FEN letter */
  edit?: { pieces: Record<string, string>; onChange: (p: Record<string, string>) => void; brush: string | null };
  flipped?: boolean;
  lastMove?: string | null;
}

function squareName(col: number, row: number, white: boolean) {
  const file = white ? col : 7 - col;
  const rank = white ? 7 - row : row;
  return FILES[file] + (rank + 1);
}

export function Board({ board, onMove, disabled, edit, flipped, lastMove }: BoardProps) {
  const [sel, setSel] = useState<string | null>(null);
  const whiteBottom = (board.orientation === "white") !== !!flipped;
  const pieces = edit ? edit.pieces : board.pieces;
  const level = edit ? 0 : board.mask_level;
  const last = useMemo(() => (lastMove ? [lastMove.slice(0, 2), lastMove.slice(2, 4)] : []), [lastMove]);

  function click(sq: string) {
    if (edit) {
      const next = { ...edit.pieces };
      if (edit.brush === null || next[sq] === edit.brush) delete next[sq];
      else next[sq] = edit.brush;
      edit.onChange(next);
      return;
    }
    if (disabled || !onMove) return;
    if (!sel) {
      // On masked boards the user may select any square: they must know where their pieces are.
      setSel(sq);
      return;
    }
    if (sel === sq) return setSel(null);
    const p = pieces[sel];
    const promo = (p === "P" && sel[1] === "7" && sq[1] === "8") || (p === "p" && sel[1] === "2" && sq[1] === "1") ? "q" : "";
    onMove(sel + sq + promo);
    setSel(null);
  }

  const cells = [];
  for (let row = 0; row < 8; row++) {
    for (let col = 0; col < 8; col++) {
      const sq = squareName(col, row, whiteBottom);
      const dark = (col + row) % 2 === 1;
      const p = pieces[sq];
      const x = col * 10, y = row * 10;
      cells.push(
        <g key={sq} onClick={() => click(sq)} className="sq" role="button" aria-label={sq}>
          <rect x={x} y={y} width={10} height={10} className={`cell ${dark ? "dark" : "light"} ${sel === sq ? "sel" : ""} ${last.includes(sq) ? "last" : ""}`} />
          {p && level <= 1 && p !== "*" && (
            <text x={x + 5} y={y + 7.6} textAnchor="middle" className={`piece ${level === 1 ? "silhouette" : p === p.toUpperCase() ? "white" : "black"}`}>
              {GLYPH[p.toUpperCase()]}
            </text>
          )}
          {p && (level === 2 || p === "*") && <circle cx={x + 5} cy={y + 5} r={1.6} className="marker" />}
          {col === 0 && <text x={x + 0.6} y={y + 2.2} className="coord">{sq[1]}</text>}
          {row === 7 && <text x={x + 8.6} y={y + 9.5} className="coord">{sq[0]}</text>}
        </g>,
      );
    }
  }
  return (
    <figure className="board-wrap">
      <svg viewBox="0 0 80 80" className="board" aria-label={`chess board, ${MASK_LABEL[level]}`}>
        {cells}
      </svg>
      <figcaption className="muted small">
        {MASK_LABEL[level]} · {board.side_to_move} to move{sel ? ` · from ${sel}` : ""}
      </figcaption>
    </figure>
  );
}

export function Palette({ brush, setBrush }: { brush: string | null; setBrush: (b: string | null) => void }) {
  const set = ["K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p"];
  return (
    <div className="palette" role="toolbar" aria-label="piece palette">
      {set.map((p) => (
        <button key={p} className={`pal ${brush === p ? "on" : ""}`} onClick={() => setBrush(p)} aria-label={p}>
          <span className={`piece-inline ${p === p.toUpperCase() ? "white" : "black"}`}>{GLYPH[p.toUpperCase()]}</span>
        </button>
      ))}
      <button className={`pal ${brush === null ? "on" : ""}`} onClick={() => setBrush(null)}>Erase</button>
    </div>
  );
}

export function piecesToPlacement(pieces: Record<string, string>): string {
  const rows: string[] = [];
  for (let rank = 8; rank >= 1; rank--) {
    let row = "", empty = 0;
    for (const f of FILES) {
      const p = pieces[f + rank];
      if (p) { if (empty) row += empty; empty = 0; row += p; } else empty++;
    }
    if (empty) row += empty;
    rows.push(row);
  }
  return rows.join("/");
}
