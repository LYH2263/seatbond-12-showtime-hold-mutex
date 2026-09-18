export class ApiError extends Error {
  status: number;
  code: string;
  conflictId?: number;

  constructor(message: string, status: number, code = "error", conflictId?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    if (conflictId !== undefined) this.conflictId = conflictId;
  }
}

// FastAPI error bodies: {detail: "..."} or our structured
// {detail: {code, message, showtime_id, party_size, conflict_id, span}}.
function extractError(status: number, raw: string): ApiError {
  let detail: unknown = raw;
  try {
    detail = JSON.parse(raw).detail;
  } catch {
    /* non-JSON body: keep raw text */
  }
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    const d = detail as Record<string, unknown>;
    const message = typeof d.message === "string" ? d.message : "请求失败";
    const code = typeof d.code === "string" ? d.code : "error";
    return new ApiError(message, status, code, d.conflict_id as number | undefined);
  }
  if (typeof detail === "string" && detail) {
    return new ApiError(detail, status);
  }
  if (Array.isArray(detail)) {
    const msg = detail
      .map((x) => {
        const e = x as { msg?: string; loc?: (string | number)[] };
        return e?.msg ? `${e.loc?.slice(1).join(".")}: ${e.msg}` : String(x);
      })
      .join("; ");
    return new ApiError(msg || "请求参数有误", status, "validation");
  }
  return new ApiError(raw || `请求失败（HTTP ${status}）`, status);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      ...init,
    });
  } catch {
    // fetch only rejects on network/DNS failure — never on HTTP status.
    throw new ApiError("网络异常：无法连接锁座服务，请检查网络后重试", 0, "network");
  }
  if (!res.ok) {
    const text = await res.text();
    throw extractError(res.status, text);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
