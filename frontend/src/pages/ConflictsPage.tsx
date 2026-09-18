import { useEffect, useState } from "react";
import { api } from "../api/client";

type Conflict = {
  id: number;
  showtime_id: number;
  party_size: number;
  reason: string;
  request_code: string | null;
  requested_row: number | null;
  requested_start_col: number | null;
  requested_end_col: number | null;
  blocking_order_code: string | null;
  film_title: string | null;
  created_at: string;
};

export default function ConflictsPage() {
  const [rows, setRows] = useState<Conflict[]>([]);
  useEffect(() => {
    api<Conflict[]>("/conflicts").then(setRows);
  }, []);

  return (
    <>
      <h2>冲突</h2>
      <table className="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>场次</th>
            <th>被拒请求</th>
            <th>人数</th>
            <th>申请座位</th>
            <th>占用方订单</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.id}>
              <td className="mono">{new Date(c.created_at).toLocaleString()}</td>
              <td>
                #{c.showtime_id}
                {c.film_title ? ` · ${c.film_title}` : ""}
              </td>
              <td className="mono">{c.request_code ?? "—"}</td>
              <td>{c.party_size}</td>
              <td className="mono">
                {c.requested_row != null
                  ? `R${c.requested_row} C${c.requested_start_col}-${c.requested_end_col}`
                  : "—"}
              </td>
              <td className="mono">{c.blocking_order_code ?? "—"}</td>
              <td>{c.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
