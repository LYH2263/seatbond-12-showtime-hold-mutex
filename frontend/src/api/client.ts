export class ApiError extends Error {
  status: number;
  code?: string;
  requestCode?: string;
  showtimeId?: number;
  partySize?: number;
  requested?: { row: number; start_col: number; end_col: number } | null;
  blockingOrderCode?: string | null;
  isConflict: boolean;

  constructor(status: number, message: string, detail: any) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.isConflict = status === 409;
    if (detail && typeof detail === "object") {
      this.code = detail.code;
      this.requestCode = detail.request_code;
      this.showtimeId = detail.showtime_id;
      this.partySize = detail.party_size;
      this.requested = detail.requested;
      this.blockingOrderCode = detail.blocking_order_code;
    }
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      ...init,
    });
  } catch {
    // 网络层失败（断网/超时/服务不可达），与后端返回的 409 冲突明确区分
    throw new ApiError(0, "网络异常：无法连接到服务器，请检查网络后重试（不是座位冲突）", null);
  }

  if (!res.ok) {
    let message = res.statusText;
    let detail: any = null;
    try {
      const data = await res.json();
      detail = data?.detail ?? null;
      if (detail && typeof detail === "object" && detail.message) {
        message = detail.message;
      } else if (typeof data?.detail === "string") {
        message = data.detail;
      }
    } catch {
      // 非 JSON 错误体，回退到文本/状态码
      try {
        const text = await res.text();
        if (text) message = text;
      } catch {
        /* ignore */
      }
    }
    throw new ApiError(res.status, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
