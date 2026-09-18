import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

type Conflict = {
  id: number;
  showtime_id: number;
  film_title?: string | null;
  hall_name?: string | null;
  party_size: number;
  kind: string;
  row?: number | null;
  start_col?: number | null;
  end_col?: number | null;
  reason: string;
  created_at: string;
};

const KIND_LABEL: Record<string, string> = {
  overlap: "座位冲突",
  no_seats: "无连续空座",
};

function spanText(c: Conflict): string {
  if (c.row == null || c.start_col == null || c.end_col == null) return "—";
  return `第${c.row}排 ${c.start_col}-${c.end_col} 座`;
}

export default function ConflictsPage() {
  const [rows, setRows] = useState<Conflict[]>([]);
  const load = useCallback(() => {
    api<Conflict[]>("/conflicts").then(setRows);
  }, []);
  useEffect(load, [load]);
  return (
    <>
      <h2>冲突</h2>
      <div className="toolbar">
        <button onClick={load}>刷新</button>
        <span className="hint">并发锁座中被拒绝的请求都会留痕（含场次、人数与被拒座位）</span>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>类型</th>
            <th>场次</th>
            <th>被拒座位</th>
            <th>人数</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="mono">{new Date(c.created_at).toLocaleString()}</td>
              <td>
                <span className={`kind-badge kind-${c.kind}`}>
                  {KIND_LABEL[c.kind] ?? c.kind}
                </span>
              </td>
              <td>
                {c.film_title ? `${c.film_title}` : `场次 #${c.showtime_id}`}
                {c.hall_name ? ` · ${c.hall_name}` : ""}
                <div className="mono sub">#{c.showtime_id}</div>
              </td>
              <td className="mono">{spanText(c)}</td>
              <td>{c.party_size}</td>
              <td>{c.reason}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="empty-hint">暂无冲突记录</td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
