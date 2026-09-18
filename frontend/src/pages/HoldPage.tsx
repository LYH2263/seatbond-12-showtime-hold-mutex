import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";

type Show = { id: number; film_title: string; hall_name?: string };
type Hold = {
  id: number;
  order_code: string;
  row: number;
  start_col: number;
  end_col: number;
  party_size: number;
};

export default function HoldPage() {
  const [shows, setShows] = useState<Show[]>([]);
  const [sid, setSid] = useState<number | "">("");
  const [party, setParty] = useState(3);
  const [prefRow, setPrefRow] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [last, setLast] = useState<Hold | null>(null);

  useEffect(() => {
    api<Show[]>("/showtimes").then((s) => {
      setShows(s);
      if (s[0]) setSid(s[0].id);
    });
  }, []);

  async function submit() {
    setMsg("");
    setErr(null);
    try {
      const body: Record<string, unknown> = { showtime_id: sid, party_size: party };
      if (prefRow) body.preferred_row = Number(prefRow);
      const hold = await api<Hold>("/holds", { method: "POST", body: JSON.stringify(body) });
      setLast(hold);
      setMsg(`已锁座 ${hold.order_code}：第${hold.row}排 ${hold.start_col}-${hold.end_col}（${hold.party_size} 人）`);
    } catch (e) {
      setErr(e instanceof ApiError ? e : new ApiError(0, String(e), null));
    }
  }

  const reqText = err?.requested
    ? `第${err.requested.row}排 ${err.requested.start_col}-${err.requested.end_col} 座`
    : "";

  return (
    <>
      <h2>锁座</h2>
      <div className="toolbar">
        <select value={sid} onChange={(e) => setSid(Number(e.target.value))}>
          {shows.map((s) => (
            <option key={s.id} value={s.id}>
              {s.film_title} · {s.hall_name}
            </option>
          ))}
        </select>
        <label>
          人数{" "}
          <input
            type="number"
            min={1}
            max={12}
            value={party}
            onChange={(e) => setParty(Number(e.target.value))}
            style={{ width: 72 }}
          />
        </label>
        <label>
          优先排{" "}
          <input
            value={prefRow}
            onChange={(e) => setPrefRow(e.target.value)}
            placeholder="可选"
            style={{ width: 72 }}
          />
        </label>
        <button onClick={submit}>查找并锁连座</button>
      </div>
      {msg && <div className="ok">{msg}</div>}
      {err && (
        <div className={err.isConflict ? "err conflict" : "err network"}>
          {err.isConflict ? (
            <>
              <strong>座位冲突，锁座失败</strong>
              <div>{err.message}</div>
              <div className="mono conflict-meta">
                被拒请求 {err.requestCode} · 场次 #{err.showtimeId} · {err.partySize} 人
                {reqText ? ` · 申请 ${reqText}` : ""}
                {err.blockingOrderCode ? ` · 已被 ${err.blockingOrderCode} 占用` : ""}
              </div>
              <div className="conflict-hint">这不是网络问题，座位仍在：可稍后重试或改选其他座位。</div>
            </>
          ) : (
            <>
              <strong>请求失败</strong>
              <div>{err.message}</div>
            </>
          )}
        </div>
      )}
      {last && (
        <p className="mono">
          订单 {last.order_code} · {last.party_size} 人 · R{last.row} C{last.start_col}-{last.end_col}
        </p>
      )}
    </>
  );
}
