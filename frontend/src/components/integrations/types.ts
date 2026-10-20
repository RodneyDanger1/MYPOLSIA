/**
 * Wire types for `/api/v1/integrations` (V26/L2).
 *
 * These mirror `backend/app/api/v1/integrations.py` exactly.  There is deliberately NO field
 * for a credential VALUE in any of them — the server cannot send one and this panel cannot
 * render one.
 */

/** The per-secret source vocabulary of `app/services/secret_store.py`. */
export type SecretSource = "stored" | "env" | "none";

/** A provider's source adds one word for multi-variable providers: both sources in play. */
export type ProviderSource = SecretSource | "mixed";

export type SecretStatus = {
  name: string;
  configured: boolean;
  source: SecretSource;
  /** Non-reversible display hint from `mask_secret` — never the value. */
  masked: string;
};

export type ProviderStatus = {
  provider: string;
  label: string;
  /** True when every REQUIRED variable is present (companions may still be missing). */
  configured: boolean;
  source: ProviderSource;
  /** `true` for stripe / meta_ads / google_ads: status only, credentials live in the env. */
  read_only: boolean;
  /** `true` when this build wires a read-only probe (github, render, sendgrid, stripe). */
  testable: boolean;
  secrets: SecretStatus[];
};

export type ProviderList = { providers: ProviderStatus[] };

/** `POST /integrations/{provider}/test` — only a real 200 reaches this shape. */
export type ProviderTestResult = {
  ok: boolean;
  provider: string;
  detail: string;
};
