/** D5 (audit L5-01): the UI names a refusal in BOTH wire shapes the backend emits.
 *
 *  Envelope: the app's PolsiaError handler -> {ok:false, error:{code, message, how_to_fix}}.
 *  Detail:   FastAPI's default handler for refusal_guard/direct raises -> {detail:{code, ...}}.
 *
 *  RED leg (observed): the `typeof d.code === "string"` guard in `refusalFrom` neutralized with
 *  `false &&` -> the detail-shape test fails (E_HTTP_404 instead of E_NOT_FOUND); restore -> green.
 */
import { ApiError, api } from "@/lib/api";

function mockFetchResponse(status: number, body: string) {
  (global as unknown as { fetch: jest.Mock }).fetch = jest.fn().mockResolvedValue({
    ok: false,
    status,
    statusText: "mock status",
    text: async () => body,
    json: async () => JSON.parse(body),
  });
}

beforeEach(() => {
  jest.restoreAllMocks();
});

describe("refusal shape parsing (D5)", () => {
  it("parses the envelope shape and keeps the fix hint", async () => {
    mockFetchResponse(
      403,
      JSON.stringify({ ok: false, error: { code: "E_AUTH", message: "denied", how_to_fix: "check the key" } }),
    );
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("E_AUTH");
    expect(err.howToFix).toBe("check the key");
  });

  it("parses the HTTPException detail shape (what refusal_guard produces)", async () => {
    mockFetchResponse(
      404,
      JSON.stringify({
        detail: { code: "E_NOT_FOUND", message: "task 7 does not exist", how_to_fix: "use an id from /tasks" },
      }),
    );
    const err = (await api.get("/tasks/7").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("E_NOT_FOUND");
    expect(err.message).toBe("task 7 does not exist");
    expect(err.howToFix).toBe("use an id from /tasks");
  });

  it("leaves a codeless detail payload a generic E_HTTP error", async () => {
    mockFetchResponse(422, JSON.stringify({ detail: [{ loc: ["body"], msg: "field required" }] }));
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err.code).toBe("E_HTTP_422");
  });
});
